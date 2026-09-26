#!/usr/bin/env python3
"""
Mock Bluesky Jetstream firehose (plain WebSocket) for local testing.

Emits messages whose "kind" mix is known exactly, so the counters written by
firehose_collector to metrics_log.txt can be verified against ground truth.

Client-side classification semantics being mirrored here:
    kind == "commit"   -> Commit_Count
    kind == "identity" -> Identity_Count
    kind == "account"  -> Account_Count
    anything else      -> Info_Count   (kind "info", unknown kinds,
                                        unparseable/truncated frames)

Run with --help for all options.
"""

import argparse
import asyncio
import json
import random
import signal
import sys
import time

import websockets

LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua. "
)

MIX_KEYS = ("commit", "identity", "account", "info", "unknown", "garbage")
CLIENT_BUCKET = {
    "commit": "commit",
    "identity": "identity",
    "account": "account",
    "info": "info",
    "unknown": "info",
    "garbage": "info",
}


def parse_mix(spec):
    mix = {k: 0.0 for k in MIX_KEYS}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        key, _, weight = part.partition(":")
        key = key.strip()
        if key not in mix:
            raise SystemExit(f"unknown mix key {key!r} (valid: {', '.join(MIX_KEYS)})")
        mix[key] = float(weight)
    if sum(mix.values()) <= 0:
        raise SystemExit("mix weights must sum to > 0")
    return mix


def hexs(rng, nbits):
    return "%0*x" % (nbits // 4, rng.getrandbits(nbits))


class Generator:
    def __init__(self, args):
        self.args = args
        self.rng = random.Random(args.seed)
        self.mix = parse_mix(args.mix)
        self.keys = list(self.mix)
        self.weights = [self.mix[k] for k in self.keys]

    def _pick(self):
        return self.rng.choices(self.keys, weights=self.weights, k=1)[0]

    def _text(self):
        n = self.args.payload_bytes
        if n <= 0:
            return "hello world"
        return (LOREM * (n // len(LOREM) + 1))[:n]

    def commit(self):
        rng = self.rng
        record = {
            "$type": "app.bsky.feed.post",
            "createdAt": "2026-09-17T00:00:00.000Z",
            "langs": ["en"],
            "text": self._text(),
        }
        return {
            "did": "did:plc:" + hexs(rng, 96),
            "kind": "commit",
            "time_us": int(time.time() * 1e6),
            "commit": {
                "rev": hexs(rng, 52),
                "operation": "create",
                "collection": "app.bsky.feed.post",
                "rkey": hexs(rng, 52),
                "record": record,
                "cid": "bafyrei" + hexs(rng, 80),
            },
        }

    def identity(self):
        rng = self.rng
        did = "did:plc:" + hexs(rng, 96)
        return {
            "did": did,
            "kind": "identity",
            "time_us": int(time.time() * 1e6),
            "identity": {
                "did": did,
                "handle": hexs(rng, 32) + ".bsky.social",
                "seq": rng.randint(1, 10**9),
                "time": "2026-09-17T00:00:00.000Z",
            },
        }

    def account(self):
        rng = self.rng
        did = "did:plc:" + hexs(rng, 96)
        return {
            "did": did,
            "kind": "account",
            "time_us": int(time.time() * 1e6),
            "account": {
                "active": True,
                "did": did,
                "seq": rng.randint(1, 10**9),
                "status": "active",
                "time": "2026-09-17T00:00:00.000Z",
            },
        }

    def info(self):
        return {
            "kind": "info",
            "name": "OutdatedCursor",
            "message": "Requested cursor exceeded limit. Possibly missing events.",
        }

    def unknown(self):
        return {
            "did": "did:plc:" + hexs(self.rng, 96),
            "kind": "not-a-real-kind",
            "time_us": int(time.time() * 1e6),
        }

    def make(self):
        kind = self._pick()
        if kind == "commit":
            payload = json.dumps(self.commit())
        elif kind == "identity":
            payload = json.dumps(self.identity())
        elif kind == "account":
            payload = json.dumps(self.account())
        elif kind == "info":
            payload = json.dumps(self.info())
        elif kind == "unknown":
            payload = json.dumps(self.unknown())
        else:
            # truncated before "kind" is ever completed -> unparseable
            payload = '{"did":"did:plc:%s", "kin' % hexs(self.rng, 96)
        return kind, payload


ARGS = None
STATS = None
STOP = None


async def feed(ws, gen):
    n = 0
    start = time.monotonic()
    while not STOP.is_set():
        kind, payload = gen.make()
        try:
            await ws.send(payload)
        except Exception:
            return
        STATS["sent"][kind] += 1
        STATS["bytes"] += len(payload)
        n += 1
        if ARGS.burst:
            if n % ARGS.rate == 0:
                delay = start + (n // ARGS.rate) - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
        else:
            delay = start + n / ARGS.rate - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)


async def drop_later(ws):
    await asyncio.sleep(ARGS.drop_every)
    await ws.close(code=1001, reason="mock drop")


async def handler(ws):
    STATS["connections"] += 1
    gen = Generator(ARGS)
    tasks = [asyncio.create_task(feed(ws, gen))]
    if ARGS.drop_every > 0:
        tasks.append(asyncio.create_task(drop_later(ws)))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except Exception:
        pass
    finally:
        for t in tasks:
            t.cancel()


def dump_expected():
    expected = {b: 0 for b in ("commit", "identity", "account", "info")}
    for kind, count in STATS["sent"].items():
        expected[CLIENT_BUCKET[kind]] += count
    out = {
        "sent": STATS["sent"],
        "expected_client": expected,
        "connections": STATS["connections"],
        "bytes": STATS["bytes"],
    }
    if ARGS.expected_file:
        with open(ARGS.expected_file, "w") as fh:
            json.dump(out, fh, indent=2)
    print("expected: " + json.dumps(out), flush=True)


async def amain():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, STOP.set)

    async with websockets.serve(
        handler, ARGS.host, ARGS.port, ping_interval=None, max_size=None
    ):
        print(
            "mock firehose on ws://%s:%d%s rate=%d/s burst=%s mix=%s"
            % (ARGS.host, ARGS.port, ARGS.path, ARGS.rate, ARGS.burst, ARGS.mix),
            flush=True,
        )
        try:
            await asyncio.wait_for(STOP.wait(), timeout=ARGS.duration or None)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            pass
    dump_expected()


def main():
    global ARGS, STATS, STOP
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--path", default="/subscribe?wantedCollections=app.bsky.feed.post")
    p.add_argument("--rate", type=float, default=40.0, help="messages per second")
    p.add_argument("--burst", action="store_true", help="send each second's quota as fast as possible")
    p.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until signal)")
    p.add_argument("--drop-every", type=float, default=0, help="close each connection after N seconds")
    p.add_argument("--payload-bytes", type=int, default=0, help="length of the commit record text field")
    p.add_argument("--mix", default="commit:90,identity:4,account:3,info:2,unknown:1")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--expected-file", default=None)
    ARGS = p.parse_args()
    STATS = {"sent": {k: 0 for k in MIX_KEYS}, "connections": 0, "bytes": 0}
    STOP = asyncio.Event()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
