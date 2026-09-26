#!/usr/bin/env python3
"""
verify_day.py — ΑΝΕΞΑΡΤΗΤΟΣ έλεγχος πληρότητας ενός εξαγμένου 24ώρου.

Δεν επαναχρησιμοποιεί τη λογική του extract_day.py: ξαναχτίζει το σύνολο των
αναμενόμενων δευτερολέπτων, βρίσκει ΑΚΡΙΒΩΣ ποια λείπουν, και ελέγχει όλες τις
ύπουλες περιπτώσεις που θα μπορούσαν να κρύψουν απώλεια:

  - διπλότυπα (δύο γραμμές στο ίδιο δευτερόλεπτο -> μια απώλεια κρύβεται)
  - κακοσχηματισμένες γραμμές (θα τις έτρωγε σιωπηλά ένας parser)
  - γραμμές εκτός παραθύρου
  - μη αύξουσα σειρά
  - περίοδοι με ΜΗΔΕΝ μηνύματα (δείχνουν νεκρή σύνδεση, όχι απώλεια γραμμής)
  - ο σωρευτικός μετρητής dropped frames

Χρήση:  python3 verify_day.py --date 2026-09-20
"""
import argparse
import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd

EXPECTED = 86400


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-20")
    ap.add_argument("--day", default="../data/metrics_log.txt")
    ap.add_argument("--diag", default="../data/diag_log_2026-09-20.csv")
    ap.add_argument("--tz", default="Europe/Athens")
    a = ap.parse_args()

    day_path = a.day or f"../data/metrics_log_{a.date}.txt"
    tz = ZoneInfo(a.tz)
    d0 = dt.date.fromisoformat(a.date)
    start = int(dt.datetime(d0.year, d0.month, d0.day, tzinfo=tz).timestamp())
    end = start + EXPECTED

    rows, bad_len, bad_num = [], 0, 0
    with open(day_path) as fh:
        for line in fh:
            if line.startswith("Seconds,"):
                continue
            parts = line.rstrip("\n").split(",")
            if len(parts) != 8:
                bad_len += 1
                continue
            try:
                ts = int(parts[0])
            except ValueError:
                bad_num += 1
                continue
            rows.append((ts, parts))

    secs = [r[0] for r in rows]
    present = set(secs)
    inside = {s for s in present if start <= s < end}
    outside = len(present) - len(inside)
    missing = sorted(set(range(start, end)) - inside)

    print(f"αρχείο                  : {day_path}")
    print(f"παράθυρο                : {a.date} 00:00:00 .. 23:59:59  ({a.tz})")
    print(f"γραμμές δεδομένων       : {len(rows)}")
    print(f"μοναδικά δευτερόλεπτα   : {len(present)}  (εντός παραθύρου: {len(inside)})")
    print(f"διπλότυπα               : {len(secs) - len(present)}")
    print(f"λάθος αριθμός πεδίων     : {bad_len}")
    print(f"μη αριθμητικό Seconds    : {bad_num}")
    print(f"εκτός παραθύρου          : {outside}")
    print(f"μη αύξουσα σειρά         : {sum(1 for i in range(1, len(secs)) if secs[i] <= secs[i-1])}")
    print(f"πληρότητα               : {100.0 * len(inside) / EXPECTED:.4f} %")
    print(f"ΛΕΙΠΟΥΝ                 : {len(missing)} δευτερόλεπτα")

    if missing:
        runs, s0, prev = [], missing[0], missing[0]
        for m in missing[1:]:
            if m == prev + 1:
                prev = m
            else:
                runs.append((s0, prev)); s0 = prev = m
        runs.append((s0, prev))
        print(f"  σε {len(runs)} συνεχόμενα διαστήματα:")
        for x, y in runs:
            print(f"    {dt.datetime.fromtimestamp(x, tz):%H:%M:%S} .. "
                  f"{dt.datetime.fromtimestamp(y, tz):%H:%M:%S}   ({y - x + 1}s)")

    # ---- ροή δεδομένων: περίοδοι απόλυτης σιωπής ------------------------
    tot = [sum(int(x) for x in r[1][2:6]) for r in rows]
    zero = sum(1 for v in tot if v == 0)
    best = cur = 0
    best_at = None
    for i, v in enumerate(tot):
        if v == 0:
            cur = cur + 1 if cur else 1
            if cur > best:
                best, best_at = cur, i - cur + 1
        else:
            cur = 0
    print(f"δευτερόλεπτα με 0 μηνύματα: {zero}")
    print(f"μεγαλύτερη συνεχής σιωπή  : {best}s"
          + (f"  (από {dt.datetime.fromtimestamp(rows[best_at][0], tz):%H:%M:%S})" if best_at is not None else ""))
    print(f"σύνολο μηνυμάτων         : {sum(tot):,}")

    # ---- dropped frames -------------------------------------------------
    try:
        d = pd.read_csv(a.diag)
        d = d[(d["Seconds"] >= start) & (d["Seconds"] < end)]
        print(f"drops (μέγιστο στο 24ωρο): {int(d['Dropped_Frames_Total'].max())}")
    except Exception as e:                     # noqa: BLE001
        print(f"drops: δεν διαβάστηκε το diag ({e})")

    ok = (len(missing) <= 600 and len(secs) == len(present) and not bad_len
          and not bad_num and outside == 0)
    print()
    print("ΕΤΟΙΜΟ ΓΙΑ ΠΑΡΑΔΟΣΗ ✓" if ok and len(missing) <= 600
          else "ΠΡΟΣΟΧΗ ✗ — έλεγξε τα παραπάνω")


if __name__ == "__main__":
    main()
