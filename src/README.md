# Firehose Collector — Raspberry Pi source and deployment

A multithreaded real-time collector for the Bluesky Jetstream Firehose,
written in C for the Raspberry Pi Zero W (ARMv6, 32-bit) and built around the
producer–consumer pattern with POSIX threads.

This directory is the complete set of files that runs on the Pi: the C source,
the Makefile, the systemd units and the operational scripts. On the target all
of these live together in a single directory (here,
`/home/vagelismo/firehose`).

## Files

| File | Role |
|---|---|
| `firehose_collector.c` | **The program.** Producer, consumer and periodic logger threads, bounded circular queue, counters, watchdog. |
| `jsmn.h` | Single-header JSON tokeniser used by the consumer (third-party, unmodified). |
| `Makefile` | Builds the collector. |
| `bluesky-firehose.service` | systemd unit: starts the collector at boot, restarts it on failure, pins the CPU governor. |
| `check_pi.sh` | Pre-flight check: dependencies, clock, network, endpoint, build, 20 s test run, service state. |
| `health_log.sh`, `health-log.service`, `health-log.timer` | Side-car that samples RSS, thread count, temperature and throttling flags once a minute. Independent of the collector — it never restarts or touches it. |

## Requirements

```bash
sudo apt install build-essential pkg-config libwebsockets-dev libssl-dev
```

## Build

```bash
make                # produces ./firehose_collector
make clean          # removes the binary
```

## Deploy as a service

```bash
sudo cp firehose_collector /usr/local/bin/
sudo cp bluesky-firehose.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bluesky-firehose
bash check_pi.sh
```

The service runs with `Restart=always` and `<WorkingDirectory>` set to this
directory, so `metrics_log.txt`, `connection_log.txt`, `diag_log.csv` and
`health_log.csv` are written here.

## Run manually

```bash
./firehose_collector        # runs until SIGINT/SIGTERM; no arguments
```

The endpoint defaults to the assignment URL
(`wss://jetstream1.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post`).
For offline testing the endpoint can be overridden without recompiling, via
`FIREHOSE_HOST`, `FIREHOSE_PORT`, `FIREHOSE_PATH` and `FIREHOSE_SSL`; on the Pi
these are never set, so the behaviour is exactly the assignment endpoint.

## Output files

`metrics_log.txt` — the required telemetry, one line per second, exactly eight
comma-separated fields:

```
Seconds,Nanoseconds,Commit_Count,Identity_Count,Account_Count,Info_Count,Buffer_Occupancy_Pct,CPU_Pct
```

The header is written only when the file is created, so a restart continues the
same dataset instead of injecting a header in the middle of it.

`diag_log.csv` — secondary diagnostic stream, kept separate so that the file
above stays exactly on specification:

```
Seconds,Nanoseconds,Wakeup_Jitter_us,Peak_Occupancy_Pct,Dropped_Frames_Total,Truncated_Frames_Total
```

`connection_log.txt` — timestamped connect/disconnect/reconnect/watchdog events.

`health_log.csv` — one sample per minute (RSS, threads, temperature, throttling
flags), produced by the `health-log.timer` side-car.

## Operational notes

* **Clock.** The Pi has no RTC. The unit is ordered after
  `network-online.target` and `time-sync.target`, and `check_pi.sh` verifies
  that NTP is synchronised, because the `Seconds` column is wall-clock time.
* **Wi-Fi power saving** must be off, otherwise latency spikes and packet loss
  appear. `check_pi.sh` reports the state and prints the one-off commands.
* **CPU governor.** The service pins the governor to `performance` at start-up
  so that frequency transitions do not add wake-up latency. This is a
  deliberate real-time trade-off, not an oversight.
* **Do not run two instances.** `check_pi.sh` skips its 20 s test run while the
  service is active, because two writers appending to the same
  `metrics_log.txt` would corrupt the dataset.
