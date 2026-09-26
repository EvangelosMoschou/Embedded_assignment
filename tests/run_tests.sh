#!/usr/bin/env bash
#
# Local test harness for firehose_collector.
#
# Builds the collector against the local mock firehose in three flavours
# (plain / ASan+UBSan / TSan), runs a set of traffic profiles and checks that
# the counters logged in metrics_log.txt match exactly what the mock sent.
#
#   bash tests/run_tests.sh              # default durations
#   SECS=60 RECONNECT_SECS=300 bash tests/run_tests.sh
#
# Nothing here touches the Pi build: binaries go to tests/bin, logs to tests/run.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../src/firehose_collector.c"
BIN="$HERE/bin"
RUN="$HERE/run"
MOCK="$HERE/mock_firehose.py"
PORT="${PORT:-8765}"
SECS="${SECS:-20}"
RECONNECT_SECS="${RECONNECT_SECS:-150}"
LONG_SECS="${LONG_SECS:-120}"
CASES="${CASES:-all}"
DRAIN=3

INCS="$(pkg-config --cflags libwebsockets)"
LIBS="$(pkg-config --libs libwebsockets)"
COMMON=(-Wall -Wextra -DJSMN_PARENT_LINKS $INCS)

FAILED=0
SUMMARY=()

build() {
    local out="$1"; shift
    printf '  gcc %-12s ' "$out"
    if gcc "$@" -o "$BIN/$out" "$SRC" -pthread -lm $LIBS 2>"$BIN/$out.build.log"; then
        echo "OK"
    else
        echo "FAILED"; sed -n '1,20p' "$BIN/$out.build.log"; return 1
    fi
}

want_case() {
    [ "$CASES" = "all" ] && return 0
    case " $CASES " in *" $1 "*) return 0 ;; esac
    return 1
}

run_case() {
    local name="$1" bin="$2" secs="$3" mock_args="$4" do_counts="$5"
    local dir="$RUN/$name" client_pid mock_pid sampler pid
    echo
    if ! want_case "$name"; then
        echo "===== $name (skipped) ====="
        return 0
    fi
    echo "===== $name  (${secs}s) ====="
    rm -rf "$dir"; mkdir -p "$dir"

    # TSan cannot start with the default ASLR entropy on modern kernels.
    local cmd=("$BIN/$bin")
    case "$bin" in *tsan*) cmd=(setarch "$(uname -m)" -R "$BIN/$bin") ;; esac

    # shellcheck disable=SC2086
    python3 "$MOCK" --port "$PORT" --expected-file "$dir/expected.json" $mock_args \
        > "$dir/mock.log" 2>&1 &
    mock_pid=$!
    sleep 1

    (
        cd "$dir" || exit 1
        # NB: sanitizer log_path must not contain spaces (the project path has
        # spaces), so they are relative to the client's cwd.
        exec env \
            FIREHOSE_HOST=127.0.0.1 FIREHOSE_PORT="$PORT" FIREHOSE_SSL=0 \
            ASAN_OPTIONS=detect_leaks=1:log_path=asan \
            UBSAN_OPTIONS=print_stacktrace=1 \
            TSAN_OPTIONS=halt_on_error=0:log_path=tsan \
            "${cmd[@]}" > client.out 2> client.err
    ) &
    client_pid=$!

    (
        while kill -0 "$client_pid" 2>/dev/null; do
            rss=$(awk '/VmRSS/{print $2}' "/proc/$client_pid/status" 2>/dev/null)
            printf '%s %s\n' "$(date +%s)" "${rss:-0}"
            sleep 2
        done
    ) > "$dir/rss.txt" 2>/dev/null &
    sampler=$!

    sleep "$secs"

    kill -TERM "$mock_pid" 2>/dev/null
    wait "$mock_pid" 2>/dev/null
    sleep "$DRAIN"
    kill -TERM "$client_pid" 2>/dev/null
    wait "$client_pid" 2>/dev/null
    kill "$sampler" 2>/dev/null
    wait "$sampler" 2>/dev/null

    local conns=0
    [ -f "$dir/connection_log.txt" ] && conns=$(grep -c 'CONNECTED to' "$dir/connection_log.txt")
    echo "  connections: $conns"

    if [ -f "$dir/rss.txt" ]; then
        echo "  rss (KB): first=$(head -1 "$dir/rss.txt" | awk '{print $2}') max=$(awk 'BEGIN{m=0}{if($2>m)m=$2}END{print m}' "$dir/rss.txt") last=$(tail -1 "$dir/rss.txt" | awk '{print $2}')"
    fi

    local counts_ok="n/a"
    if [ "$do_counts" = "yes" ]; then
        if [ -f "$dir/metrics_log.txt" ] && [ -f "$dir/expected.json" ]; then
            python3 "$HERE/check_counts.py" "$dir/expected.json" "$dir/metrics_log.txt" | tee "$dir/counts.txt"
            if grep -q "RESULT: PASS" "$dir/counts.txt"; then counts_ok="PASS"; else counts_ok="FAIL"; FAILED=1; fi
        else
            echo "  ! metrics_log.txt or expected.json missing"; counts_ok="FAIL"; FAILED=1
        fi
    fi

    local san_hits=0
    for f in "$dir"/asan.* "$dir"/tsan.*; do
        [ -f "$f" ] || continue
        san_hits=$((san_hits + 1))
        echo "  --- sanitizer report: $(basename "$f") ($(wc -l < "$f") lines) ---"
        sed -n '1,25p' "$f"
    done
    [ "$san_hits" -eq 0 ] && echo "  sanitizer: clean"

    SUMMARY+=("$name: counts=$counts_ok connections=$conns san_reports=$san_hits")
}

echo "=== build ==="
mkdir -p "$BIN" "$RUN"
build fh_plain -O2 -g "${COMMON[@]}" || exit 1
build fh_asan  -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer "${COMMON[@]}" || exit 1
build fh_tsan  -O1 -g -fsanitize=thread -fno-omit-frame-pointer "${COMMON[@]}" || exit 1
build fh_hard  -O2 -g -D_FORTIFY_SOURCE=2 -fstack-protector-all -fno-omit-frame-pointer "${COMMON[@]}" || exit 1

echo
echo "=== cases ==="
run_case steady_plain    fh_plain "$SECS"           "--rate 40"                    yes
run_case hardened_plain  fh_hard  "$SECS"           "--rate 40"                    yes
run_case steady_asan     fh_asan  "$SECS"           "--rate 40"                    yes
run_case realistic_asan  fh_asan  "$SECS"           "--rate 20 --payload-bytes 3000" yes
run_case large_asan      fh_asan  "$SECS"           "--rate 20 --payload-bytes 8000" yes
run_case huge_asan       fh_asan  "$SECS"           "--rate 20 --payload-bytes 20000" yes
run_case adversarial_asan fh_asan "$SECS" "--rate 30 --payload-bytes 16000 --mix commit:25,garbage:50,unknown:25" yes
run_case burst_plain     fh_plain "$SECS"           "--rate 400 --burst"           yes
run_case tsan_steady     fh_tsan  "$SECS"           "--rate 40"                    yes
run_case reconnect_plain fh_plain "$RECONNECT_SECS" "--rate 30 --drop-every 5"     no
run_case steady_long     fh_plain "$LONG_SECS"      "--rate 150"                   yes

echo
echo "=============================="
echo "SUMMARY"
for line in "${SUMMARY[@]}"; do echo "  $line"; done
echo "=============================="
exit "$FAILED"
