# Bluesky Jetstream Firehose Collector

**https://github.com/EvangelosMoschou/Embedded_assignment**

A multithreaded, real-time data acquisition system written in **C**, running on a
**Raspberry Pi Zero W** (ARM1176 / ARMv6, single core @ 1 GHz, 512 MB, no
real-time kernel). It subscribes to the public Bluesky **Jetstream** firehose over
a WebSocket, classifies every incoming JSON message by its `kind` field, and
appends one line of telemetry per second to a CSV log.

Final assignment for Real-Time Embedded Systems, September 2026.

---

## Repository layout

```
src/        Everything that runs on the Pi: the C source, the Makefile,
            the systemd units and the operational scripts.  (see src/README.md)
data/       The 24-hour dataset and the supporting logs for the same day.
analysis/   The post-processing script and the figures it produces.
report/     The report (XeLaTeX source and PDF).
docs/       Long-run behaviour and the restart investigation.
```

| Path | What it is |
|---|---|
| `src/firehose_collector.c` | The program — producer, consumer, logger and watchdog threads. |
| `src/Makefile` | Builds it. |
| `data/metrics_log.txt` | **The required 24-hour dataset** — 8 fields, one line per second. |
| `data/diag_log_2026-09-20.csv` | Jitter, peak occupancy, drop and truncation counters. |
| `data/connection_log.txt` | Connect / disconnect / reconnect / watchdog events. |
| `data/health_log_2026-09-20.csv` | RSS, threads, temperature and throttling, once a minute. |
| `analysis/plot_metrics.py` | Produces the three required plots. |
| `analysis/figs/` | The generated figures (including a combined three-panel overview). |
| `report/architecture.pdf` | The report. |

---

## The 24-hour dataset

The collector ran continuously and unattended on the physical Raspberry Pi from
19 September to 22 September 2026. The measured day is **Sunday 20 September 2026**,
00:00:00 – 23:59:59 local time (`Europe/Athens`).

| Quantity | Value |
|---|---|
| Samples logged | **86 394 / 86 400** (99.993 %) |
| Messages classified | 3 412 814 |
| Message rate | 39.5 Hz mean, **343 Hz peak** (peak-to-mean ≈ 8.7×) |
| Jitter vs. the ideal deadline (median / p95 / p99) | **104 µs / 362 µs / 845 µs** |
| Jitter vs. the ideal deadline, worst case | 18.1 ms |
| Jitter, signed period deviation | **median 0 ms**, range ±18 ms, symmetric (41 716 negative / 41 536 positive) |
| Peak circular-buffer occupancy | **13.7 %** (35 of 256 slots) |
| **Dropped frames** | **0** |
| CPU utilisation | 7.8 % mean (of one core) |
| Resident memory | 12.5 MB (of which 4 MB is the pre-allocated queue) |
| Network disconnections | 42, all auto-recovered, longest outage 3.0 s |
| Process restarts | 1 (watchdog — see below) |

**Time without incoming data: 191 s out of 86 400 (0.221 %).** It has two
distinct causes, which only the accompanying logs can tell apart:

| Cause | Where it shows | Time |
|---|---|---|
| The process was not running | 6 missing rows in `metrics_log.txt` | 6 s |
| The socket was disconnected | 42 `DISCONNECTED` events in `connection_log.txt` | 90.5 s |
| The server sent nothing (socket alive) | rows present, all counters zero | ~95 s |

A row exists for **every** second in which the process was alive, including the
seconds in which nothing arrived — those are written with all counters zero, so
periods without data are never silently omitted. Since the average rate is
39.5 messages/second, an empty second is not statistical noise: it is a real
interruption of reception.

The single restart happened at 14:43:45 local, when the producer thread stalled
inside the network library while the connection was still established. The
supervisory thread detected the stale heartbeat after 5 s, terminated the
process, and `systemd` restarted it 5.5 s later. The watchdog distinguishes this
from a quiet network: at 01:28:17 the log shows an *identical* seven-second
interval with no messages in which **no** restart occurred, because the thread
was alive. The full evidence chain is in
[`docs/observations.md`](docs/observations.md).

---

## Architecture

Four threads, decoupled so that a delay in one cannot propagate into the others:

| Thread | Role |
|---|---|
| **Producer** | libwebsockets event loop (`lws_service`, 50 ms). Reassembles fragmented frames, enqueues complete messages, never blocks. Reconnects with exponential backoff (1…60 s, reset after a connection that lasted more than 60 s). |
| **Consumer** | Sleeps on the queue's condition variable, parses with **jsmn** (a zero-allocation tokeniser), and increments four counters under their own mutex. Performs no I/O. |
| **Logger** | Strictly periodic at 1 Hz via `clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME)`, advanced from the **previous ideal deadline**, so drift is zero by construction. The only thread that writes. |
| **Watchdog** | Observes atomic heartbeats and exits the process if a worker reports no progress for 5 s, letting `systemd` (`Restart=always`) restart it. Takes part in no data path. |

The shared state is a **bounded circular queue of 256 slots × 16 KB** (statically
allocated, 4 MB) protected by one mutex and two condition variables (`notFull`,
`notEmpty`), plus the counter block under its own mutex. No lock is ever held
across a blocking call, and the consumer releases the slot *before* parsing, so
the slow path never blocks the producer.

Full description, synchronisation rationale and race-condition analysis:
**[`report/architecture.pdf`](report/architecture.pdf)**.

---

## Build and run on the Pi

```bash
sudo apt install build-essential pkg-config libwebsockets-dev libssl-dev
cd src && make                     # produces ./firehose_collector
```

```bash
sudo cp firehose_collector /usr/local/bin/
sudo cp bluesky-firehose.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bluesky-firehose
sudo bash check_pi.sh              # pre-flight and post-flight checks
```

`check_pi.sh` verifies dependencies, clock synchronisation, Wi-Fi power saving,
endpoint reachability, the build and the running service. See
[`src/README.md`](src/README.md) for the output-file formats and operational notes.

---

## Reproducing the figures

```bash
cd analysis
python3 plot_metrics.py            # writes figs/*.png
```

It reads `../data/metrics_log.txt` and
`../data/diag_log_2026-09-20.csv` and regenerates all five figures. It also
prints the summary statistics quoted above (coverage, jitter percentiles, peak
occupancy, drops).

---

## Data provenance

The dataset is genuine output from the physical Raspberry Pi Zero W — no
simulation, no emulation, no other machine, as the assignment requires.

The collector writes continuously and never truncates or rotates. The 24-hour
file committed here is that continuous log restricted to the window
00:00:00–23:59:59 local time on 20 September 2026, unmodified, with its original
header. Its completeness can be checked directly: the file contains 86 394 data
rows plus one header, the first row is `1789851600` (20/09 00:00:00 local) and
the last is `1789937999` (20/09 23:59:59 local), and no second appears twice.

---

## Third-party code

`src/jsmn.h` — the jsmn JSON tokeniser (MIT licence), included unmodified.
Everything else in this repository was written for this assignment.
