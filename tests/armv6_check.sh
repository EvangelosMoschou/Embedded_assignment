#!/usr/bin/env bash
#
# armv6_check.sh — επαλήθευση ότι ο κώδικας είναι σωστός για τον επεξεργαστή
# του Raspberry Pi Zero W (ARM1176 / ARMv6) ΧΩΡΙΣ να χρειάζεται το Pi.
#
#   1. C11 atomics: το pattern των heartbeats (4-byte atomic_int) πρέπει να
#      κάνει LINK χωρίς -latomic. Το control με _Atomic(time_t) (8-byte) στο
#      armv6 ΠΡΕΠΕΙ να αποτύχει — έτσι αποδεικνύεται ότι ο έλεγχος πιάνει.
#      (Γι' αυτό τα heartbeats είναι σκόπιμα int και όχι time_t.)
#   2. Ο πλήρης firehose_collector.c μεταγλωττίζεται για -march=armv6 με
#      -Wall -Wextra -Wformat=2.
#   3. Μεγέθη τύπων στη 32-bit ARM (time_t/long) μέσω static asserts.
#
# Χρειάζεται: sudo apt install gcc-arm-linux-gnueabihf
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../src/firehose_collector.c"
OUT="$HERE/run/armv6"
CC="${CROSS_CC:-arm-linux-gnueabihf-gcc}"
ARCH="${CROSS_ARCH:-armv6}"
# -marm: το Ubuntu cross-gcc βγαίνει σε Thumb-1, που δεν υποστηρίζει hard-float VFP
ARCHFLAGS=(-marm -march="$ARCH" -mfpu=vfp -mfloat-abi=hard)
FAIL=0

rm -rf "$OUT"; mkdir -p "$OUT"

if ! command -v "$CC" >/dev/null 2>&1; then
    echo "λείπει ο $CC — τρέξε: sudo apt install gcc-arm-linux-gnueabihf"
    exit 2
fi
echo "cross compiler: $($CC --version | head -1)"

# --- host shim: το opensslconf.h ζει στο multiarch dir της host, όχι στο ARM tree
HOSTINC="/usr/include/$(gcc -print-multiarch 2>/dev/null)"
SHIM=()
if [ -d "$HOSTINC/openssl" ]; then
    mkdir -p "$OUT/hostfix/openssl"
    ln -sf "$HOSTINC"/openssl/*.h "$OUT/hostfix/openssl/" 2>/dev/null
    SHIM=(-I"$OUT/hostfix")
fi

cat > "$OUT/hb_probe.c" <<'EOF'
/* Ίδιο pattern με τα heartbeats / shutdown flag του collector */
#include <stdatomic.h>
#include <time.h>
static atomic_int hb_producer, hb_consumer, hb_logger;
static atomic_int shutdown_requested = 0;
int main(void) {
    atomic_store_explicit(&hb_producer, (int)time(NULL), memory_order_relaxed);
    atomic_store_explicit(&shutdown_requested, 1, memory_order_relaxed);
    return (int)(time(NULL) - hb_producer) + shutdown_requested
           + hb_consumer + hb_logger;
}
EOF

cat > "$OUT/probe8.c" <<'EOF'
/* Control: η ΠΑΛΙΑ έκδοση (8-byte atomic time_t) — δεν είναι lock-free στο armv6 */
#include <stdatomic.h>
#include <time.h>
static _Atomic(time_t) hb;
int main(void) {
    atomic_store_explicit(&hb, time(NULL), memory_order_relaxed);
    return (int)(time(NULL) - hb);
}
EOF

cat > "$OUT/sizes.c" <<'EOF'
#include <time.h>
_Static_assert(sizeof(time_t) == 8, "time_t δεν ειναι 64-bit (time64)");
_Static_assert(sizeof(long) == 4, "long δεν ειναι 32-bit");
int main(void) { return 0; }
EOF

echo
echo "=== 1. C11 atomics (link ΧΩΡΙΣ -latomic) ==="
probe() { # <label> <file> <arch> <expect ok|fail>
    local label="$1" file="$2" arch="$3" expect="$4" ok
    printf '  %-42s ' "$label"
    if "$CC" -O2 "${ARCHFLAGS[@]/armv6/$arch}" "$OUT/$file" -o "$OUT/bin_${file%.c}_$arch" \
            2> "$OUT/${file%.c}_$arch.err"; then ok=yes; else ok=no; fi
    if [ "$ok" = "$expect" ]; then echo "$ok (αναμενόμενο)"; else echo "$ok — ΑΝΑΠΑΝΤΕΧΟ"; FAIL=1; fi
}

probe "heartbeats: atomic_int (το τρέχον) @ armv6" hb_probe.c armv6 yes
probe "control: _Atomic(time_t) 8-byte @ armv6"   probe8.c   armv6 no
probe "control: _Atomic(time_t) 8-byte @ armv6k"  probe8.c   armv6k yes

echo
echo "=== 2. Πλήρης κώδικας: -march=$ARCH -Wall -Wextra -Wformat=2 ==="
# shellcheck disable=SC2086,SC2046
if "$CC" -O2 "${ARCHFLAGS[@]}" -Wall -Wextra -Wformat=2 -DJSMN_PARENT_LINKS \
        "${SHIM[@]}" $(pkg-config --cflags libwebsockets) \
        -c -o "$OUT/firehose_armv6.o" "$SRC" 2> "$OUT/compile.log"; then
    echo "  COMPILE OK (0 errors, $(wc -l < "$OUT/compile.log") warnings)"
    [ -s "$OUT/compile.log" ] && sed 's/^/      /' "$OUT/compile.log"
    echo "  object: $(file -b "$OUT/firehose_armv6.o")"
else
    echo "  COMPILE FAILED"; sed -n '1,25p' "$OUT/compile.log" | sed 's/^/      /'; FAIL=1
fi

echo
echo "=== 3. Μεγέθη τύπων (32-bit ARM) ==="
if "$CC" "${ARCHFLAGS[@]}" -c -o "$OUT/sizes.o" "$OUT/sizes.c" 2>/dev/null; then
    echo "  time_t = 64-bit, long = 32-bit  (το πεδίο Seconds του CSV ισχύει ως το 2038)"
else
    echo "  τα static asserts απέτυχαν"; FAIL=1
fi

echo
if [ "$FAIL" -eq 0 ]; then
    echo "RESULT: ARMv6 READY ✓  (χτίζει και linkάρει χωρίς -latomic)"
else
    echo "RESULT: ΠΡΟΒΛΗΜΑ ✗"
fi
exit "$FAIL"
