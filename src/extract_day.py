#!/usr/bin/env python3
"""
extract_day.py — κόβει από το συνεχές metrics_log.txt το τοπικό 24ωρο που θέλεις.

Ο collector τρέχει συνεχώς (ο service είναι Restart=always και το header
γράφεται μόνο μία φορά, όταν το αρχείο δημιουργείται), οπότε μετά τη λήψη
κρατάμε μόνο το παράθυρο της ημέρας που μας ενδιαφέρει.

Το πεδίο `Seconds` είναι Unix epoch σε UTC, ενώ η "ημέρα" που θέλουμε είναι
τοπική ώρα — γι' αυτό ο υπολογισμός του παραθύρου γίνεται με timezone.

usage:
  python3 extract_day.py                          # σήμερα, Europe/Athens
  python3 extract_day.py --date 2026-09-20
  python3 extract_day.py --date 2026-09-20 --src metrics_log.txt --out metrics_log_2026-09-20.txt

Έξοδος: το αρχείο με το header + τις γραμμές του 24ώρου, και αναφορά για
πληρότητα (86400 δευτερόλεπτα), κενά, διπλότυπα και μέγιστο κενό.
"""

import argparse
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

EXPECTED = 86400  # δευτερόλεπτα σε ένα 24ωρο


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", default=dt.date.today().isoformat(),
                   help="τοπική ημερομηνία YYYY-MM-DD (default: σήμερα)")
    p.add_argument("--tz", default="Europe/Athens")
    p.add_argument("--src", default="metrics_log.txt")
    p.add_argument("--out", default=None,
                   help="default: metrics_log_<date>.txt")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


def main():
    a = parse_args()
    tz = ZoneInfo(a.tz)
    day = dt.date.fromisoformat(a.date)
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())
    end = start + EXPECTED          # αποκλειστικό όριο
    out_path = a.out or f"metrics_log_{day.isoformat()}.txt"

    if not os.path.exists(a.src):
        print(f"! δεν βρέθηκε το {a.src}", file=sys.stderr)
        return 2

    kept, header, total = [], None, 0
    secs = set()
    duplicates = 0
    with open(a.src, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            if line.startswith("Seconds,"):
                if header is None:
                    header = line
                continue                      # header ποτέ στη μέση του dataset
            total += 1
            field = line.split(",", 1)[0]
            try:
                ts = int(field)
            except ValueError:
                continue                      # σκουπίδι -> αγνοείται
            if start <= ts < end:
                if ts in secs:
                    duplicates += 1
                else:
                    secs.add(ts)
                kept.append(line)

    if header is None:
        header = ("Seconds,Nanoseconds,Commit_Count,Identity_Count,"
                  "Account_Count,Info_Count,Buffer_Occupancy_Pct,CPU_Pct")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n")
        for line in kept:
            fh.write(line + "\n")

    missing = EXPECTED - len(secs)
    biggest = 0
    if secs:
        ordered = sorted(secs)
        for i in range(1, len(ordered)):
            if ordered[i] != ordered[i - 1] + 1:
                biggest = max(biggest, ordered[i] - ordered[i - 1] - 1)

    # Κενά στην άκρη του παραθύρου: το μεγαλύτερο ενδιάμεσο κενό δεν τα πιάνει.
    leading = (min(secs) - start) if secs else EXPECTED
    trailing = ((end - 1) - max(secs)) if secs else EXPECTED

    print(f"πηγή            : {a.src}  ({total} γραμμές δεδομένων συνολικά)")
    print(f"ημέρα           : {a.date}  ({a.tz})")
    print(f"παράθυρο (UTC)  : {start} .. {end - 1}")
    print(f"              = {dt.datetime.fromtimestamp(start, tz):%Y-%m-%d %H:%M:%S} .. "
          f"{dt.datetime.fromtimestamp(end - 1, tz):%H:%M:%S} τοπική")
    print(f"γραμμές που κρατήθηκαν : {len(kept)}  (αναμενόμενο {EXPECTED})")
    print(f"μοναδικά δευτερόλεπτα  : {len(secs)}  -> πληρότητα {100.0 * len(secs) / EXPECTED:.3f}%")
    print(f"λείπουν                : {missing} δευτερόλεπτα")
    print(f"  πριν το 1ο δείγμα    : {leading}s")
    print(f"  μετά το τελευταίο    : {trailing}s")
    print(f"  ενδιάμεσα (μέγιστο)  : {biggest}s")
    print(f"διπλότυπα Seconds      : {duplicates}")
    if kept:
        print(f"πρώτη/τελευταία        : {kept[0].split(',')[0]} / {kept[-1].split(',')[0]}")
    print(f"γράφτηκε               : {out_path}")
    if duplicates:
        print("  ΣΗΜΕΙΩΣΗ: διπλότυπο Seconds σημαίνει ότι ο logger πρόλαβε καθυστερημένες")
        print("           προθεσμίες (catch-up). Για το jitter χρησιμοποίησε το diag_log.csv,")
        print("           που το μετράει απευθείας από το CLOCK_MONOTONIC.")
    if missing:
        print("! το dataset δεν καλύπτει όλο το 24ωρο — δες journalctl -u bluesky-firehose")
    else:
        print("  ΚΑΛΥΨΗ: πλήρες 24ωρο, χωρίς κενά.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
