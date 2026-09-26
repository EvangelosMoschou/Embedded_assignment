#!/bin/bash
# check_pi.sh — Pre-flight έλεγχος για το firehose_collector στο Raspberry Pi
# Τρέχεις: bash check_pi.sh
# Κάνει: έλεγχο εξαρτήσεων, compile, συνδεσιμότητα firehose, 20s δοκιμαστικό run,
#        και ελέγχει ότι το systemd service είναι σωστά στημένο.

PASS=0; FAIL=0
ok()   { echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "=== 1. Εξαρτήσεις συστήματος ==="
for cmd in gcc make pkg-config curl; do
    command -v $cmd >/dev/null && ok "$cmd βρέθηκε" || bad "$cmd ΔΕΝ βρέθηκε (sudo apt install build-essential pkg-config curl)"
done
pkg-config --exists libwebsockets && ok "libwebsockets ($(pkg-config --modversion libwebsockets))" \
    || bad "libwebsockets λείπει (sudo apt install libwebsockets-dev libssl-dev)"
LDCONFIG=$(command -v ldconfig || echo /sbin/ldconfig)
$LDCONFIG -p 2>/dev/null | grep -q libssl && ok "OpenSSL βιβλιοθήκη" || bad "OpenSSL λείπει (sudo apt install libssl-dev)"

echo "=== 2. Ρολόι / ημερομηνία ==="
timedatectl 2>/dev/null | grep -q "synchronized: yes" && ok "Ρολόι συγχρονισμένο (NTP)" \
    || bad "Ρολόι ΔΕΝ είναι συγχρονισμένο (sudo timedatectl set-ntp true)"
echo "         Τρέχουσα ώρα: $(date '+%Y-%m-%d %H:%M:%S')"

echo "=== 3. Δίκτυο ==="
ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1 && ok "Internet (ping)" || bad "Δεν υπάρχει internet"
# Ένα απλό GET δεν είναι WebSocket upgrade, οπότε ο server απαντά 4xx
# (400/405/426) — κάθε 4xx σημαίνει ότι το endpoint είναι προσβάσιμο.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "https://jetstream1.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post" || true)
case "$code" in
    4*) ok "Jetstream endpoint προσβάσιμο (HTTP $code, σωστή απόρριψη non-WS GET)" ;;
    *)  bad "Jetstream endpoint ΜΗΝ προσβάσιμο (code=$code)" ;;
esac
IWCONFIG=$(command -v iwconfig || echo /usr/sbin/iwconfig)
if sudo -n iw dev wlan0 get power_save 2>/dev/null | grep -qi "off" \
   || "$IWCONFIG" wlan0 2>/dev/null | grep -q "Power Management:off"; then
    ok "WiFi power management OFF"
else
    echo "  [WARN] WiFi power management είναι ON — να το κλείσεις (δείτε στο τέλος)"
fi

echo "=== 4. Compile ==="
if make >/dev/null 2>&1; then
    ok "make επιτυχές ($DIR/firehose_collector)"
else
    bad "make απέτυχε — τρέξε 'make' χειροκίνητα για το σφάλμα"
    echo; exit 1
fi

echo "=== 5. Δοκιμαστικό run 20 δευτερολέπτων ==="
if systemctl is-active --quiet bluesky-firehose 2>/dev/null; then
    echo "  [SKIP] Ο service bluesky-firehose τρέχει ήδη."
    echo "         Ένα δεύτερο instance θα έγραφε στο ΙΔΙΟ metrics_log.txt"
    echo "         και θα έμπλεκε τα δεδομένα του 24ώρου."
    echo "         Αν θέλεις τον έλεγχο:  sudo systemctl stop bluesky-firehose"
    echo "         (και μετά ξανά:        sudo systemctl start bluesky-firehose)"
else
rm -f /tmp/test_metrics.txt /tmp/test_conn.txt
timeout 20 ./firehose_collector >/dev/null 2>&1
grep CONNECTED connection_log.txt >/dev/null 2>&1 && ok "Συνδέθηκε στο firehose" \
    || bad "ΔΕΝ συνδέθηκε — δες connection_log.txt"
lines=$(tail -n +2 metrics_log.txt 2>/dev/null | wc -l)
[ "$lines" -ge 15 ] && ok "metrics_log.txt: $lines γραμμές σε ~20s (αναμενόμενο ~15-19)" \
    || bad "metrics_log.txt: μόνο $lines γραμμές — κάτι δεν πάει καλά"
# σύννομος έλεγχος: υπάρχουν commits;
if tail -n +2 metrics_log.txt 2>/dev/null | awk -F, '{s+=$3} END {exit !(s>0)}'; then
    ok "Λαμβάνονται commit μηνύματα"
else
    bad "Δεν λαμβάνονται commit μηνύματα"
fi
fi

echo "=== 6. Systemd service ==="
if systemctl list-unit-files 2>/dev/null | grep -q bluesky-firehose; then
    systemctl is-enabled bluesky-firehose >/dev/null 2>&1 && ok "service enabled" || bad "service ΔΕΝ είναι enabled (sudo systemctl enable bluesky-firehose)"
    systemctl is-active bluesky-firehose >/dev/null 2>&1 && ok "service active" || echo "  [WARN] service δεν τρέχει τώρα (sudo systemctl start bluesky-firehose)"
else
    echo "  [INFO] service δεν είναι εγκατεστημένο ακόμα — δείτε βήμα 2 παρακάτω"
fi

echo
echo "=============================="
echo "ΑΠΟΤΕΛΕΣΜΑ: PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "ΟΛΑ ΟΚ — έτοιμο για το 24ωρο run!" || echo "Διόρθωσε τα FAIL πριν το 24ωρο."
echo
echo "Αν είχες WARN για WiFi power management, τρέξε μια φορά:"
echo "  sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf <<EOF"
echo "[connection]"
echo "wifi.powersave = 2"
echo "EOF"
echo "  sudo systemctl restart NetworkManager"
