#!/usr/bin/env python3
"""
Compare the raw kinds the mock firehose sent against the totals the collector
logged in metrics_log.txt.

usage: check_counts.py <expected.json> <metrics_log.txt>
exit code 0 = exact match, 1 = mismatch
"""

import json
import sys


def main():
    expected_path, metrics_path = sys.argv[1], sys.argv[2]
    with open(expected_path) as fh:
        expected = json.load(fh)["expected_client"]

    totals = {"commit": 0, "identity": 0, "account": 0, "info": 0}
    lines = 0
    with open(metrics_path) as fh:
        for lineno, line in enumerate(fh):
            line = line.strip()
            if not line or line.startswith("Seconds,"):
                continue
            fields = line.split(",")
            if len(fields) != 8:
                print(f"  ! malformed CSV row {lineno + 1}: {line!r}")
                continue
            lines += 1
            for i, key in enumerate(("commit", "identity", "account", "info")):
                totals[key] += int(fields[2 + i])

    ok = True
    for key in ("commit", "identity", "account", "info"):
        got, want = totals[key], expected[key]
        flag = "ok" if got == want else "MISMATCH"
        if got != want:
            ok = False
        print(f"  {key:<9} sent={want:<7} logged={got:<7} diff={got - want:+d}  [{flag}]")

    print(f"  csv rows: {lines}")
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
