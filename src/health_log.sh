#!/bin/bash
#
# health_log.sh — δείγμα υγείας του firehose_collector.
#
# Το τρέχει το health-log.timer κάθε λεπτό. Διαβάζει τον collector ΑΠΟ ΕΞΩ
# (/proc), οπότε δεν χρειάζεται restart, δεν αγγίζει το metrics_log.txt και
# δεν επηρεάζει τη λειτουργία του. Σκοπός: να αποδειχθεί (με στοιχεία) ότι η
# μνήμη μένει σταθερή για μέρες και ότι δεν έγιναν restarts.
#
# Έξοδος (append): /home/vagelismo/firehose/health_log.csv
#   Timestamp,ISO_Time,PID,Uptime_Sec,RSS_KB,VSZ_KB,Threads,Temp_C,Throttled,Service
#
# Το Throttled είναι το flag του vcgencmd: 0x0 = καμία υποτάση/throttling από
# το boot. Οποιαδήποτε άλλη τιμή σημαίνει ότι κάποια στιγμή υπήρξε πρόβλημα
# τροφοδοσίας ή θερμικό όριο — χρήσιμο στοιχείο για την αναφορά.
#
set -u

DIR="/home/vagelismo/firehose"
OUT="$DIR/health_log.csv"
SERVICE="bluesky-firehose"

# header μόνο όταν το αρχείο δημιουργείται
if [ ! -s "$OUT" ]; then
    echo "Timestamp,ISO_Time,PID,Uptime_Sec,RSS_KB,VSZ_KB,Threads,Temp_C,Throttled,Service" >> "$OUT"
fi

pid=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)
state=$(systemctl is-active "$SERVICE" 2>/dev/null)
now=$(date +%s)
iso=$(date '+%Y-%m-%d %H:%M:%S')

rss=""; vsz=""; thr=""; up=""; temp=""; throttled=""
if [ -n "${pid:-}" ] && [ "$pid" != "0" ] && [ -r "/proc/$pid/status" ]; then
    rss=$(awk '/^VmRSS:/{print $2}'  "/proc/$pid/status" 2>/dev/null)
    vsz=$(awk '/^VmSize:/{print $2}' "/proc/$pid/status" 2>/dev/null)
    thr=$(awk '/^Threads:/{print $2}' "/proc/$pid/status" 2>/dev/null)
    up=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
fi
if command -v vcgencmd >/dev/null 2>&1; then
    temp=$(vcgencmd measure_temp 2>/dev/null | sed -n 's/^temp=\([0-9.]*\).*/\1/p')
    throttled=$(vcgencmd get_throttled 2>/dev/null | sed -n 's/^throttled=//p')
fi

printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$now" "$iso" "${pid:-}" "${up:-}" "${rss:-}" "${vsz:-}" "${thr:-}" \
    "${temp:-}" "${throttled:-}" "$state" >> "$OUT"
