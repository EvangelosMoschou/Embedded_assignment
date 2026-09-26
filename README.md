# Bluesky Jetstream Firehose Collector

A multithreaded, real-time data acquisition system written in **C**, running on a
**Raspberry Pi Zero W** (ARM1176 / ARMv6, single core @ 1 GHz, 512 MB, no
real-time kernel). It subscribes to the public Bluesky **Jetstream** firehose over
a WebSocket, classifies every incoming JSON message by its `kind` field, and
appends one line of telemetry per second to a CSV log.

Final assignment for Real-Time Embedded Systems, September 2026.

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
| Jitter (median / p95 / p99) | **104 µs / 362 µs / 845 µs** |
| Worst-case jitter | 18.1 ms |
| Peak circular-buffer occupancy | **13.7 %** (35 of 256 slots) |
| **Dropped frames** | **0** |
| CPU utilisation | 7.8 % mean (of one core) |
| Resident memory | 12.5 MB (of which 4 MB is the pre-allocated queue) |
| Network disconnections | 42, all auto-recovered, longest outage 3.0 s |
| Process restarts | 1 (watchdog, see below) |

**Time without incoming data: 191 s out of 86 400 (0.221 %).** It breaks down as
two distinct causes, which only the accompanying logs can separate:

| Cause | Evidence | Time |
|---|---|---|
| Process not running | 6 missing rows in `metrics_log.txt` | 6 s |
| Socket disconnected | 42 `DISCONNECTED` events in `connection_log.txt` | 90.5 s |
| Server sent nothing (socket alive) | rows present, all counters zero | ~95 s |

The single restart occurred at 14:43:45 local, when the producer thread stalled
inside the network library with the connection established. The supervisory
thread detected the stale heartbeat after 5 s, terminated the process, and
`systemd` restarted it 5.5 s later. The watchdog distinguishes this from a quiet
network: at 01:28:17 the log shows an *identical* seven-second interval with no
messages, in which **no** restart occurred because the thread was alive. Detail in
[`docs/observations.md`](docs/observations.md).

---

## Architecture

Four threads, decoupled so that a delay in one cannot propagate into the others:

| Thread | Role |
|---|---|
| **Producer** | libwebsockets event loop (`lws_service`, 50 ms). Reassembles fragmented frames, enqueues complete messages, never blocks. Reconnects with exponential backoff (1…60 s, reset after a connection lasting > 60 s). |
| **Consumer** | Sleeps on the queue's condition variable, parses with **jsmn** (zero-allocation tokeniser), increments four counters under their own mutex. Performs no I/O. |
| **Logger** | Strictly periodic 1 Hz via `clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME)`, advanced from the **previous ideal deadline**, so drift is zero by construction. The only thread that writes. |
| **Watchdog** | Observes atomic heartbeats; exits the process if a worker reports no progress for 5 s, letting `systemd` (`Restart=always`) restart it. Takes part in no data path. |

The shared state is a **bounded circular queue of 256 slots × 16 KB** (statically
allocated, 4 MB) protected by one mutex and two condition variables (`notFull`,
`notEmpty`), plus the counter block under its own mutex. No lock is ever held
across a blocking call, and the consumer releases the slot *before* parsing so the
slow path never blocks the producer.

Full description, synchronisation rationale and the race-condition analysis are in
the report: **[`report/architecture.pdf`](report/architecture.pdf)**.

---

## Repository layout

```
src/        The complete program for the Raspberry Pi: C source, Makefile,
            systemd units and operational scripts.  (see src/README.md)
data/       metrics_log.txt        <- the required 24-hour dataset (8 fields)
            diag_log_2026-09-20.csv    jitter, peak occupancy, drop counters
            connection_log.txt         connect / disconnect / watchdog events
            health_log_2026-09-20.csv  RSS, threads, temperature, throttling
analysis/   plot_metrics.py   the three required plots + a combined figure
            verify_day.py     independent completeness check of a 24-hour file
            figs/             generated figures
report/     architecture.tex / .pdf — the report (XeLaTeX)
docs/       observations.md — long-run behaviour and the restart investigation
tests/      Off-target verification harness (see tests/ENV_SETUP.md)
```

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

## Reproducing the analysis

```bash
cd analysis
python3 plot_metrics.py --day ../data/metrics_log.txt        # the three plots
python3 verify_day.py   --day ../data/metrics_log.txt        # completeness check
```

`verify_day.py` rebuilds the set of expected seconds by subtraction rather than
reusing the extraction logic, and additionally checks for the failure modes that
could *hide* a gap: duplicate rows, malformed rows, rows outside the window,
non-monotonic order, and long runs of zero counters (a silently dead socket).

---

## Data provenance

The dataset is genuine output from the physical Raspberry Pi Zero W — no
simulation, no emulation, no other machine, as the assignment requires.

The collector writes continuously and never truncates; the measured day is sliced
out afterwards with `src/extract_day.py`, which reports the coverage percentage,
the missing seconds and the duplicates. The file committed here is that output,
unmodified, with its original header.

---

## Third-party code

`src/jsmn.h` — the jsmn JSON tokeniser (MIT licence), included unmodified.
Everything else in this repository was written for this assignment.
