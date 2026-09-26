#!/usr/bin/env python3
"""
plot_metrics.py — Post-processing της 24ωρης καταγραφής του firehose collector.

Παράγει τα διαγράμματα που ζητάει η εκφώνηση:

  jitter.png       Απόκλιση του περιοδικού νήματος από την ιδανική προθεσμία
                   του 1.000 s, σε συνάρτηση με τον χρόνο.
  load_buffer.png  Διπλός άξονας: ρυθμός μηνυμάτων (Hz) και πληρότητα του
                   κυκλικού buffer (%), για την ανάδειξη της εκρηκτικότητας.
  cpu_usage.png    Διπλός άξονας: ρυθμός μηνυμάτων (Hz) και χρήση CPU (%).
  counters.png     Οι τέσσερις μετρητές ανά τύπο μηνύματος.

Χρήση:
    python3 plot_metrics.py --day ../data/metrics_log_2026-09-20.txt \
                            --diag ../data/diag_log.csv --outdir .

Το jitter υπολογίζεται από τη στήλη Wakeup_Jitter_us του diag_log.csv, δηλαδή
από την ΑΜΕΣΗ μέτρηση της απόκλισης με CLOCK_MONOTONIC. Αυτό είναι σκόπιμο:
η διαφόριση των timestamps του metrics_log.txt δίνει ψευδείς κορυφές -1000 ms
όπου ο logger πρόλαβε καθυστερημένες προθεσμίες (catch-up), ενώ η άμεση μέτρηση
δεν επηρεάζεται ούτε από αυτό ούτε από τυχόν διόρθωση του ρολογιού από το NTP.
"""

import argparse
import os

import matplotlib
matplotlib.use("Agg")          # headless: κανένα παράθυρο, μόνο αρχεία
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Μέγεθος σε ίντσες ≈ πλάτος στήλης της αναφοράς (17 cm), ώστε το σχήμα να
# τοποθετείται στην κλίμακα 1:1 και τα γράμματά του να μη σμικρύνονται.
PLT = dict(figsize=(7.0, 2.35), dpi=200)
plt.rcParams.update({
    "savefig.bbox": "tight",
    "font.size": 7.5,
    "axes.titlesize": 8.5,
    "axes.labelsize": 7.5,
    "legend.fontsize": 6.5,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
})


def load(day_path, diag_path):
    m = pd.read_csv(day_path)
    m["Hz"] = (m["Commit_Count"] + m["Identity_Count"]
               + m["Account_Count"] + m["Info_Count"])
    m["Hours"] = (m["Seconds"] - m["Seconds"].iloc[0]) / 3600.0

    d = None
    if diag_path and os.path.exists(diag_path):
        d = pd.read_csv(diag_path)
        keep = d["Seconds"].isin(m["Seconds"])
        d = d[keep].copy()
        if len(d):
            d["Hours"] = (d["Seconds"] - m["Seconds"].iloc[0]) / 3600.0
    return m, d


def jitter_series(d):
    """Δύο ορισμοί jitter, από την ΙΔΙΑ μέτρηση (δεν απαιτείται νέα εκτέλεση).

    j1 = woke - deadline_i          απόκλιση από την ιδανική προθεσμία.
                                    Είναι >= 0 εξ ορισμού, γιατί ένα
                                    clock_nanosleep() με TIMER_ABSTIME δεν
                                    μπορεί να ξυπνήσει νωρίτερα από την
                                    προθεσμία.
    j2 = woke_i - woke_(i-1) - 1s   απόκλιση της πραγματικής περιόδου από το
                                    ιδανικό 1s. Είναι ΘΕΤΙΚΗ Ή ΑΡΝΗΤΙΚΗ.

    Ισχύει ακριβώς j2_i = j1_i - j1_(i-1), γιατί οι διαδοχικές ιδανικές
    προθεσμίες απέχουν ακριβώς 1s.
    """
    j1 = d["Wakeup_Jitter_us"].to_numpy() / 1000.0
    j2 = np.concatenate([[0.0], np.diff(j1)])
    return j1, j2


def stats(m, d):
    print(f"  διάρκεια              : {m['Hours'].iloc[-1]:.2f} h "
          f"({len(m)} δείγματα)")
    print(f"  ρυθμός μηνυμάτων      : μέσος {m['Hz'].mean():.1f} Hz | "
          f"μέγιστος {int(m['Hz'].max())} Hz | "
          f"διάμεσος {m['Hz'].median():.0f} Hz")
    print(f"  CPU                   : μέση {m['CPU_Pct'].mean():.2f}% | "
          f"μέγιστη {m['CPU_Pct'].max():.2f}%")
    print(f"  πληρότητα buffer      : στιγμιαία max {m['Buffer_Occupancy_Pct'].max():.2f}%", end="")
    if d is not None and len(d):
        j = d["Wakeup_Jitter_us"].to_numpy()
        print(f" | peak max {d['Peak_Occupancy_Pct'].max():.2f}%")
        print(f"  JITTER (άμεση μέτρηση): median {np.median(j):.0f} µs | "
              f"p95 {np.percentile(j, 95):.0f} µs | "
              f"p99 {np.percentile(j, 99):.0f} µs | max {j.max()} µs")
        n5 = int((j > 5000).sum())
        print(f"  spikes > 5 ms         : {n5} ({100.0 * n5 / len(j):.3f}%)")
        _, j2 = jitter_series(d)
        print(f"  JITTER (περίοδος vs 1s): median {np.median(j2):.0f} ms | "
              f"min {j2.min():.1f} | max {j2.max():.1f} ms | "
              f"αρνητικά {int((j2 < 0).sum())} / θετικά {int((j2 > 0).sum())}")
        print(f"  απώλειες/αποκοπές     : drops={int(d['Dropped_Frames_Total'].iloc[-1])}"
              f" truncated={int(d['Truncated_Frames_Total'].iloc[-1])}")
        worst = np.argsort(j)[-5:][::-1]
        print("  5 χειρότερα jitter    : " + ", ".join(
            f"{int(d['Seconds'].iloc[i])}={j[i] / 1000:.1f}ms" for i in worst))
    else:
        print()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--day", default="../data/metrics_log.txt")
    p.add_argument("--diag", default="../data/diag_log_2026-09-20.csv")
    p.add_argument("--outdir", default="figs")
    p.add_argument("--date", default="2026-09-20")
    a = p.parse_args()

    m, d = load(a.day, a.diag)
    print(f"πηγή: {a.day}  ({a.date})")
    stats(m, d)

    os.makedirs(a.outdir, exist_ok=True)
    H, X = m["Hours"], "Χρόνος (ώρες από την έναρξη)"

    # ---- 1. Jitter ----------------------------------------------------
    fig, ax = plt.subplots(**PLT)
    if d is not None and len(d):
        j1, j2 = jitter_series(d)
        ax.plot(d["Hours"], j1, lw=0.4, color="grey", alpha=.55,
                label="από την ιδανική προθεσμία (>=0)")
        ax.plot(d["Hours"], j2, lw=0.4, color="purple",
                label="της περιόδου από το 1s (θετική ή αρνητική)")
        ax.axhline(0, lw=.4, color="black", alpha=.5)
    ax.set_xlabel(X); ax.set_ylabel("Jitter (ms)")
    ax.set_title(f"Διάγραμμα Jitter — {a.date}")
    ax.grid(alpha=.3); ax.legend(loc="upper right")
    fig.tight_layout(); fig.savefig(os.path.join(a.outdir, "jitter.png")); plt.close(fig)

    # ---- 2. Φόρτος & Buffer -------------------------------------------
    fig, ax1 = plt.subplots(**PLT)
    ax2 = ax1.twinx()
    ax1.plot(H, m["Hz"], lw=.5, color="green", alpha=.8,
             label="Ρυθμός μηνυμάτων (Hz)")
    ax2.plot(H, m["Buffer_Occupancy_Pct"], lw=.5, color="steelblue",
             label="Πληρότητα buffer, στιγμιαία (%)")
    if d is not None and len(d):
        ax2.plot(d["Hours"], d["Peak_Occupancy_Pct"], lw=.5, color="red", alpha=.8,
                 label="Πληρότητα buffer, μέγιστη ανά δευτ. (%)")
    ax1.set_xlabel(X)
    ax1.set_ylabel("Ρυθμός Μηνυμάτων (Hz)", color="green")
    ax2.set_ylabel("Πληρότητα Buffer (%)", color="steelblue")
    ax1.set_title(f"Διάγραμμα Φόρτου & Buffer — {a.date}")
    ax1.grid(alpha=.3)
    h1, l1 = ax1.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8)
    fig.tight_layout(); fig.savefig(os.path.join(a.outdir, "load_buffer.png")); plt.close(fig)

    # ---- 3. CPU --------------------------------------------------------
    fig, ax1 = plt.subplots(**PLT)
    ax2 = ax1.twinx()
    ax1.plot(H, m["Hz"], lw=.5, color="green", alpha=.8, label="Ρυθμός μηνυμάτων (Hz)")
    ax2.plot(H, m["CPU_Pct"], lw=.5, color="crimson", alpha=.8, label="Χρήση CPU (%)")
    ax1.set_xlabel(X)
    ax1.set_ylabel("Ρυθμός Μηνυμάτων (Hz)", color="green")
    ax2.set_ylabel("Χρήση CPU (%)", color="crimson")
    ax1.set_title(f"Διάγραμμα CPU (φόρτος vs χρήση) — {a.date}")
    ax1.grid(alpha=.3)
    h1, l1 = ax1.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8)
    fig.tight_layout(); fig.savefig(os.path.join(a.outdir, "cpu_usage.png")); plt.close(fig)

    # ---- 4. Μετρητές ανά τύπο -----------------------------------------
    fig, ax = plt.subplots(**PLT)
    for col, c in (("Commit_Count", "tab:blue"), ("Identity_Count", "tab:orange"),
                   ("Account_Count", "tab:green"), ("Info_Count", "tab:red")):
        ax.plot(H, m[col], lw=.4, color=c, label=col.replace("_Count", ""))
    ax.set_xlabel(X); ax.set_ylabel("Πλήθος ανά δευτερόλεπτο")
    ax.set_title(f"Μηνύματα ανά τύπο — {a.date}")
    ax.grid(alpha=.3); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(os.path.join(a.outdir, "counters.png")); plt.close(fig)

    # ---- 5. Συνοπτικό σχήμα 3 πάνελ (κοινός άξονας χρόνου) για την αναφορά
    fig, axes = plt.subplots(3, 1, figsize=(7.0, 4.6), dpi=200, sharex=True,
                             gridspec_kw={"hspace": 0.24})
    a0, a1, a2 = axes

    if d is not None and len(d):
        j1, j2 = jitter_series(d)
        a0.plot(d["Hours"], j1, lw=.32, color="grey", alpha=.55,
                label="από την ιδανική προθεσμία")
        a0.plot(d["Hours"], j2, lw=.32, color="purple",
                label="της περιόδου από το 1s")
        a0.axhline(0, lw=.4, color="black", alpha=.5)
        a0.legend(loc="upper right", frameon=False)
    a0.set_ylabel("Jitter (ms)")
    a0.set_title("(a) Διάγραμμα Jitter (θετικό ή αρνητικό)")
    a0.grid(alpha=.3)

    b1 = a1.twinx()
    a1.plot(H, m["Hz"], lw=.4, color="green")
    b1.plot(H, m["Buffer_Occupancy_Pct"], lw=.4, color="steelblue")
    if d is not None and len(d):
        b1.plot(d["Hours"], d["Peak_Occupancy_Pct"], lw=.4, color="red", alpha=.8)
    a1.set_ylabel("Hz", color="green")
    b1.set_ylabel("Buffer %", color="steelblue")
    a1.set_title("(b) Διάγραμμα Φόρτου & Buffer")
    a1.grid(alpha=.3)

    c1 = a2.twinx()
    a2.plot(H, m["Hz"], lw=.4, color="green")
    c1.plot(H, m["CPU_Pct"], lw=.4, color="crimson", alpha=.8)
    a2.set_ylabel("Hz", color="green")
    c1.set_ylabel("CPU %", color="crimson")
    a2.set_title("(c) Διάγραμμα CPU")
    a2.grid(alpha=.3)
    a2.set_xlabel(X)

    fig.savefig(os.path.join(a.outdir, "overview.png"))
    plt.close(fig)

    print("  γράφτηκαν: jitter.png, load_buffer.png, cpu_usage.png, "
          "counters.png, overview.png")


if __name__ == "__main__":
    main()
