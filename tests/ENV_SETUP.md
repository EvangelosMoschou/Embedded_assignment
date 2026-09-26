# ENV SETUP — Τοπικό περιβάλλον δοκιμών για το `firehose_collector`

Αυτό το αρχείο εξηγεί **αναλυτικά** πώς στήθηκε το τοπικό περιβάλλον δοκιμών που
χρησιμοποιήθηκε για να επιβεβαιωθεί ότι ο collector λειτουργεί σωστά, δεν έχει
memory leaks/overflow και αντέχει σε αποσυνδέσεις δικτύου — **χωρίς** να χρειάζεται
κάθε φορά το Raspberry Pi.

---

## 1. Γιατί δεν είναι "100% simulation" του Raspberry Pi

Ένα πλήρες Pi environment δεν είναι εφικτό (και δεν θα είχε και νόημα) γιατί κάποια
πράγματα είναι εγγενώς μη-αναπαραγόμενα σε x86:

| Τι | Γιατί δεν αναπαράγεται |
|---|---|
| **Wi-Fi / brcmfmac** | Το ραδιόφωνο του Zero W δεν υπάρχει έξω από το hardware |
| **Jitter / drift** | Ο scheduler και οι timers του x86 δεν έχουν σχέση με τον ARM11 @1GHz |
| **CPU %** | Το `/proc/stat` δείχνει τον host, όχι το Pi |
| **Buffer occupancy σε ρεαλιστικό φόρτο** | Δεν έχουμε το πραγματικό burstiness του firehose |
| **Θερμικά / τροφοδοσία** | — |

Επιπλέον η εκφώνηση **απαγορεύει ρητά** αποτελέσματα από οτιδήποτε άλλο εκτός από το
physical Pi. Άρα:

> **Το τοπικό περιβάλλον χρησιμοποιείται ΜΟΝΟ για correctness & memory safety.**
> Το `metrics_log.txt` του 24ώρου βγαίνει αποκλειστικά από το Raspberry Pi.

Αυτό που **καλύπτεται πλήρως** τοπικά: σωστή κατηγοριοποίηση μηνυμάτων, ακρίβεια
μετρητών, σωστή μορφή CSV, memory leaks, buffer/stack overflows, undefined behavior,
data races, thread leaks, συμπεριφορά σε αποσυνδέσεις και σε hostile input.

---

## 2. Προαπαιτούμενα

Ό,τι χρειάστηκε ήταν **ήδη** ή διαπιστώθηκε με:

```bash
for c in gcc make python3 curl; do command -v $c; done
pkg-config --modversion libwebsockets      # 4.3.3
python3 -c "import websockets; print(websockets.__version__)"   # 10.4
gcc -fsanitize=address -o /dev/null -x c - <<<'int main(void){return 0;}'   # OK
```

| Εργαλείο | Χρήση |
|---|---|
| `gcc` + `libwebsockets-dev` | χτίσιμο του πραγματικού κώδικα (ίδια βιβλιοθήκη με το Pi) |
| `python3` + `websockets` | ο mock firehose server |
| ASan / UBSan / TSan (`-fsanitize=...`) | memory safety & races |
| `setarch` (util-linux) | εκκίνηση TSan με χαμηλό ASLR |

**Δεν** χρειάστηκαν `valgrind`, `docker` ή `qemu` — τα sanitizers του GCC καλύπτουν
τα leaks και τα overflows, και ο πραγματικός κώδικας χτίζεται native με την ίδια
βιβλιοθήκη.

---

## 3. Αρχιτεκτονική

```
   ┌───────────────────────┐          ws://127.0.0.1:8765          ┌──────────────────────┐
   │  mock_firehose.py     │ ─────────────────────────────────────▶│ firehose_collector   │
   │  (Python, websockets) │                                       │ (πραγματικός C code) │
   │                       │   ελεγχόμενο kind mix / rate / burst   │                      │
   │  κρατά ground truth   │   / μέγεθος payload / σκόπιμα drops    │  γράφει:             │
   │  ──▶ expected.json    │                                       │  metrics_log.txt     │
   └───────────────────────┘                                       │  connection_log.txt  │
                                                                   └──────────┬───────────┘
                                                                              │
   ┌───────────────────────┐        exact match?                              │
   │  check_counts.py      │◀─────────────────────────────────────────────────┘
   │  (ground truth vs CSV)│
   └───────────────────────┘
                    ▲
                    │  ορχηστρώνει builds + cases, μαζεύει sanitizer reports + RSS
            ┌───────────────────┐
            │  run_tests.sh     │
            └───────────────────┘
```

Η βασική ιδέα: **ο mock ξέρει ακριβώς πόσα μηνύματα κάθε τύπου έστειλε**, οπότε τα
αθροίσματα του CSV πρέπει να ταιριάζουν *ακριβώς*. Αν ο client χάσει, διπλομετρήσει ή
παρεξηγήσει ένα μήνυμα, το diff το δείχνει αμέσως.

---

## 4. Τα αρχεία

Ο κώδικας του collector (και ό,τι τρέχει στο Pi) βρίσκεται στο `../src/` —
αυτός ο φάκελος περιέχει μόνο το τοπικό περιβάλλον δοκιμών.

```
tests/
├── mock_firehose.py     # mock Jetstream WebSocket server + ground truth
├── check_counts.py      # expected.json vs metrics_log.txt (exit 0 = exact match)
├── run_tests.sh         # builds + cases + assertions + RSS sampling
├── armv6_check.sh       # cross-compile έλεγχος για τον ARMv6 του Pi (χωρίς το Pi)
├── bin/                 # τα 4 builds (παράγονται, δεν είναι source)
└── run/<case>/          # αποτελέσματα ανά case
```

### 4.1 `mock_firehose.py`

Παράγει μηνύματα σε μορφή Jetstream με **γνωστή κατανομή** τύπων:

| `kind` που στέλνει | Πού καταλήγει στον client |
|---|---|
| `commit` | `Commit_Count` |
| `identity` | `Identity_Count` |
| `account` | `Account_Count` |
| `info` | `Info_Count` |
| `unknown` (π.χ. `not-a-real-kind`) | `Info_Count` |
| `garbage` (κομμένο JSON, χωρίς ολοκληρωμένο `kind`) | `Info_Count` |

Επιλογές:

```bash
python3 tests/mock_firehose.py --help
  --host/--port/--path        # πού ακούει (default 127.0.0.1:8765)
  --rate N                    # μηνύματα/δευτερόλεπτο
  --burst                     # στέλνει το quota του δευτερολέπτου όσο πιο γρήγορα γίνεται
  --duration N                # αυτόματο stop
  --drop-every N              # κλείνει τη σύνδεση κάθε N sec (εξαναγκασμένα reconnects)
  --payload-bytes N           # μήκος του text στο commit record (μέγεθος μηνύματος)
  --mix "commit:90,info:5,..."# κατανομή τύπων
  --seed N                    # ντετερμινιστικό
  --expected-file PATH        # γράφει ground truth JSON στο τέλος
```

### 4.2 `check_counts.py`

Διαβάζει το `expected.json` και το `metrics_log.txt`, αθροίζει τις 4 στήλες
μετρητών και τυπώνει `PASS`/`FAIL` με diff ανά τύπο.

### 4.3 `run_tests.sh`

Χτίζει 4 εκδόσεις, τρέχει τα cases, και για κάθε case:

1. ξεκινά τον mock,
2. ξεκινά τον client μέσα σε δικό του working dir (τα logs δεν λερώνουν το project),
3. δειγματοληπτεί το `VmRSS` από `/proc/<pid>/status` κάθε 2s,
4. σταματά **πρώτα τον mock**, περιμένει `DRAIN=3s` ώστε ο logger να προλάβει να
   γράψει και τα μηνύματα του τελευταίου μισού δευτερολέπτου, και μετά τον client,
5. συγκρίνει counts και μαζεύει sanitizer reports.

---

## 5. Η αλλαγή στο `firehose_collector.c`

Για να μπορεί ο mock να οδηγήσει τον client χωρίς να αλλάξει η συμπεριφορά στο Pi,
προστέθηκε **μόνο** endpoint override από environment variables:

```c
static const char *firehose_host = JETSTREAM_URL;      /* jetstream1.us-east.bsky.network */
static const char *firehose_path = JETSTREAM_PATH;
static int firehose_port = JETSTREAM_PORT;             /* 443 */
static int firehose_ssl  = 1;

static void firehose_config_init(void) {
    const char *v;
    if ((v = getenv("FIREHOSE_HOST")) && *v) firehose_host = v;
    if ((v = getenv("FIREHOSE_PATH")) && *v) firehose_path = v;
    if ((v = getenv("FIREHOSE_PORT")) && *v) firehose_port = atoi(v);
    firehose_ssl = (firehose_port == JETSTREAM_PORT);
    if ((v = getenv("FIREHOSE_SSL")) && *v) firehose_ssl = atoi(v) != 0;
}
```

| Variable | Default | Στο Pi |
|---|---|---|
| `FIREHOSE_HOST` | `jetstream1.us-east.bsky.network` | αχρησιμοποίητο |
| `FIREHOSE_PORT` | `443` | αχρησιμοποίητο |
| `FIREHOSE_PATH` | `/subscribe?wantedCollections=app.bsky.feed.post` | αχρησιμοποίητο |
| `FIREHOSE_SSL` | `1` (αυτόματα 1 όταν port==443) | αχρησιμοποίητο |

**Στο Raspberry Pi δεν αλλάζει απολύτως τίποτα**: χωρίς αυτές τις μεταβλητές
χρησιμοποιούνται τα hardcoded defaults, δηλαδή το ίδιο `wss://` endpoint με πριν.

---

## 6. Οι τέσσερις εκδόσεις (builds)

| Binary | Flags | Τι πιάνει |
|---|---|---|
| `fh_plain` | `-O2 -g -Wall -Wextra` | την πραγματική συμπεριφορά (baseline, χωρίς overhead) |
| `fh_asan` | `-fsanitize=address,undefined` | heap/stack/global **buffer overflow**, use-after-free, **memory leaks** (LSan), signed overflow, invalid shifts, misalignment |
| `fh_tsan` | `-fsanitize=thread` | **data races** και **thread leaks** |
| `fh_hard` | `-D_FORTIFY_SOURCE=2 -fstack-protector-all` | τι θα έβλεπε ένα hardened build (libc-level overflow checks, canaries) |

Γιατί και τα τέσσερα: το ASan είναι ο ισχυρότερος ανιχνευτής αλλά αλλάζει τη
διάταξη μνήμης και είναι πιο αργό· το plain δείχνει την αληθινή συμπεριφορά/RSS·
το TSan βλέπει πράγματα που τα άλλα δεν βλέπουν (races)· το hardened δείχνει ότι ο
κώδικας δεν στηρίζεται σε UB για να δουλέψει.

Τα sanitizer reports γράφονται σε `run/<case>/asan.<pid>` και `run/<case>/tsan.<pid>`.
Η απουσία αρχείου = καθαρό.

---

## 7. Τα test cases

| Case | Τι τρέχει | Τι αποδεικνύει |
|---|---|---|
| `steady_plain` | 40 msg/s, 20s | baseline: ακριβείς μετρητές, σωστό CSV |
| `hardened_plain` | ίδιο με FORTIFY + stack protector | δεν σκάει σε hardened build |
| `steady_asan` | 40 msg/s, ASan | κανένα leak/overflow στο σύνηθες μονοπάτι |
| `realistic_asan` | 3KB payloads | ρεαλιστικά μηνύματα |
| `large_asan` | 8KB payloads | **fragmentation** (lws σπάει >4KB σε πολλά callbacks) |
| `huge_asan` | 20KB payloads | **truncation** στο `MAX_FRAME_SIZE` (16384) |
| `adversarial_asan` | 16KB + 50% garbage + 25% unknown kind | hostile input, κομμένο JSON, μεγάλα frames |
| `burst_plain` | 400 msg/s σε bursts | burstiness, χωρητικότητα queue, drops |
| `tsan_steady` | 40 msg/s, TSan | races / thread leaks |
| `reconnect_plain` | drops κάθε 5s, 100s | **network resilience**: backoff 1→2→4→8→16→32→64s χωρίς να πεθάνει η διεργασία |
| `steady_long` | 150 msg/s, 120s | leak trend σε μεγάλο χρόνο (RSS flat;) |

---

## 8. Πώς τρέχει

```bash
# ολόκληρο το suite (περίπου 8-9 λεπτά με τα default durations)
bash tests/run_tests.sh

# επιλεγμένα cases
CASES="large_asan reconnect_plain" bash tests/run_tests.sh

# μεγαλύτερες διάρκειες (π.χ. soak)
SECS=120 RECONNECT_SECS=600 LONG_SECS=600 bash tests/run_tests.sh
```

Μεταβλητές: `SECS` (default 20), `RECONNECT_SECS` (150), `LONG_SECS` (120),
`CASES` (`all` ή λίστα με κενά), `PORT` (8765).

Αποτελέσματα: `tests/run/<case>/` → `metrics_log.txt`, `connection_log.txt`,
`expected.json`, `counts.txt`, `rss.txt`, `client.out/err`, `asan.*`/`tsan.*`.
Το exit code του script είναι 0 μόνο αν όλα τα counts είναι `PASS`.

**Προσθήκη νέου case:** μία γραμμή στο τέλος του `run_tests.sh`:

```bash
run_case <name> <bin> <seconds> "<mock args>" <yes|no για count check>
```

---

## 9. Παγίδες που συναντήθηκαν (χρήσιμες αν ξαναστηθεί)

1. **`ASAN_OPTIONS` με κενά σκοτώνει το ASan.**
   Το project path περιέχει κενά (`4th Year/8th Semester/...`) και το
   `log_path=<path με κενά>` προκαλεί
   `AddressSanitizer: ERROR: expected '=' in ASAN_OPTIONS`.
   Λύση: **relative** `log_path` (ο client τρέχει `cd` στο δικό του run dir).

2. **Το TSan δεν ξεκινά με το default ASLR entropy** σε σύγχρονους kernels:
   `FATAL: ThreadSanitizer: unexpected memory mapping`.
   Λύση: `setarch $(uname -m) -R <binary>` (το κάνει αυτόματα ο runner).

3. **Το `pkill -f mock_firehose.py` σκοτώνει και το ίδιο το shell** που το τρέχει,
   γιατί το pattern ταιριάζει με τη γραμμή εντολής του. Πάντα kill by PID (`$!`).

4. **`grep -c CONNECTED` μετράει και το `DISCONNECTED`.** Χρησιμοποιείται
   `grep -c 'CONNECTED to'`.

5. **Ο client πρέπει να σταματήσει *μετά* τον mock + drain.** Ο logger γράφει ανά
   δευτερόλεπτο, άρα τα μηνύματα του τελευταίου κλάσματος χάνονται αν τον
   σκοτώσεις αμέσως. Γι' αυτό `mock → stop → 3s → client stop`.

---

## 10. Τι βρήκε και τι διορθώθηκε

| # | Πρόβλημα | Απόδειξη πριν | Απόδειξη μετά |
|---|---|---|---|
| 1 | **Fragmentation**: μηνύματα > rx buffer (4096B) έρχονταν σε πολλά `CLIENT_RECEIVE` callbacks και το κάθε fragment μετριούνταν ως ξεχωριστό, άκυρο JSON → `info` | `large_asan`: **0/352 commits**, `info` 1071 αντί 15 | συσσώρευση fragments + ταξινόμηση από τα ήδη parsed tokens → **352/352**, `info` 15 |
| 2 | **Watchdog σκότωνε τη διεργασία σε κάθε αποσύνδεση >5s** (ο consumer δεν ενημέρωνε το heartbeat του όταν ήταν idle, άρα φαινόταν "stalled") | `reconnect_plain`: θάνατος στα **34s** με `WATCHDOG: stalled thread ... c=6` | 7 συνδέσεις μέχρι backoff 64s, **0 WATCHDOG**, clean STOPPED |
| 3 | **Data races** στα heartbeats, race στο signal handler, **thread leak** (ο watchdog δεν γινόταν join) | `tsan_steady`: **4 reports** | **0 reports** |
| 4 | **Τα 8-byte atomics δεν είναι lock-free στο ARMv6**: το `_Atomic(time_t)` των heartbeats εκπέμπει `__atomic_load_8/__atomic_store_8` → το `make` στο Pi θα απαιτούσε `-latomic` | `armv6_check.sh`: **LINK FAILED @ armv6** | `atomic_int` heartbeats → **LINK OK, χωρίς `-latomic`** |

Οι διορθώσεις στο `firehose_collector.c`:

- **Fragmentation**: `ws_callback()` συσσωρεύει σε static `rx_frame`/`rx_len` και
  κάνει parse μόνο όταν `lws_is_final_fragment(wsi)` γίνει true.
- **Robust parsing**: ο consumer μηδενίζει το token array και ταξινομεί από τα
  tokens που διαβάστηκαν ακόμα κι όταν το `jsmn_parse` επιστρέψει `r < 0`
  (καλύπτει frames > `MAX_FRAME_SIZE` που κόβονται — το `kind` είναι στην αρχή).
- **Watchdog**: ο consumer κάνει `pthread_cond_timedwait` με 1s deadline και
  ανανεώνει το heartbeat· το "δεν έρχονται μηνύματα" δεν είναι πλέον stall.
- **Atomicity**: `hb_*` → `atomic_int` (4-byte, lock-free στο ARMv6 — δες §12),
  `shutdown_requested` → `atomic_int`, `on_signal()` κάνει μόνο atomic store (το
  `pthread_cond_broadcast` δεν είναι async-signal-safe — ο consumer το βλέπει
  μέσω του timed wait).
- **Thread leak**: `pthread_join(watchdog, NULL)` πριν το `pthread_mutex_destroy`.

---

## 11. Memory safety — συμπέρασμα

**Δεν βρέθηκε κανένα overflow, leak ή UB.** Στοιχεία:

- **ASan + UBSan: 0 reports** σε όλα τα προφίλ (40 msg/s, 400 msg/s bursts, 3KB,
  8KB, 20KB payloads, 50% garbage input, 100s με συνεχείς αποσυνδέσεις).
- **LeakSanitizer: καθαρό** σε κάθε clean exit (SIGTERM).
- **TSan: 0 reports** μετά τις διορθώσεις.
- **`-Wall -Wextra`: 0 warnings** (και με `-std=gnu11`).
- **hardened build** (`_FORTIFY_SOURCE=2`, `-fstack-protector-all`): τρέχει χωρίς abort.

Ανάλυση ορίων (γιατί δεν βγαίνει overflow):

| Σημείο | Όριο |
|---|---|
| `rx_frame[MAX_FRAME_SIZE]` | `n = min(len, sizeof(rx_frame)-1-rx_len)` πριν κάθε `memcpy` |
| `fq.buf[fq.tail]` | `rx_len ≤ MAX_FRAME_SIZE-1`, αντιγράφονται `rx_len+1` bytes → ακριβώς χωράει |
| `frame[MAX_FRAME_SIZE]` (consumer) | `fq.len[...] ≤ MAX_FRAME_SIZE-1` |
| `tokens[256]` | το jsmn ελέγχει μόνο του το πλήθος· μετά τις αλλαγές σαρώνεται με φρουρό `JSMN_UNDEFINED` |
| `addr.sun_path` στο `sd_notify` | `strncpy` με `sizeof(...)-1` |

Αποτύπωμα μνήμης:

| | Τιμή |
|---|---|
| BSS (στατικό) | **4.212.088 B ≈ 4.02 MB** (κυρίως ο queue: 256 × 16 KB) |
| RSS σε λειτουργία | **~9.2 MB σταθερό** (steady / burst / reconnect) |
| Stack του consumer | ~20 KB (`frame` 16 KB + `tokens` 4 KB) — το default pthread stack είναι 8 MB |

Το μόνο που **δεν** πιάνει το περιβάλλον είναι τα timing μεγέθη (jitter, drift,
CPU%, occupancy) — αυτά θέλουν το Pi και την εκφώνηση.

---

## 12. ARMv6 cross-check (`armv6_check.sh`)

Ο επεξεργαστής του Zero W είναι **ARM1176 (ARMv6)**, όχι x86 — άρα υπάρχουν
ολόκληρες κατηγορίες λαθών που το παραπάνω suite **δεν** μπορεί να δει: μεγέθη
τύπων, alignment, print formats και οι εγγυήσεις lock-free των atomics. Το
`armv6_check.sh` τα καλύπτει **χωρίς το Pi**, με cross-compiler:

```bash
sudo apt install gcc-arm-linux-gnueabihf qemu-user-static
bash tests/armv6_check.sh
```

Τι ελέγχει:

1. **C11 atomics.** Τα heartbeats είναι σκόπιμα `atomic_int` (4-byte) και όχι
   `_Atomic(time_t)`. Αιτία: με `-march=armv6` ο GCC **δεν** κάνει inline
   LDREXD/STREXD για 8-byte atomics — εκπέμπει `__atomic_store_8`/`__atomic_load_8`
   και το link απαιτεί `-latomic` (με `-march=armv6k` ή `armv7-a` γίνεται inline
   και δουλεύει). Δεδομένου ότι το 32-bit Raspberry Pi OS στοχεύει armv6, αυτό θα
   έσπαγε το `make` στο Pi. Με `atomic_int` χρησιμοποιείται LDREX/STREX και χτίζει
   καθαρά, χωρίς επιπλέον εξάρτηση. Το script περιλαμβάνει **control probes που
   πρέπει να αποτύχουν**, ώστε να αποδεικνύεται ότι ο έλεγχος πιάνει όντως το
   πρόβλημα (αλλιώς ένα «όλα OK» δεν σημαίνει τίποτα).
2. **Πλήρης μεταγλώττιση** του `firehose_collector.c` για `-march=armv6` με
   `-Wall -Wextra -Wformat=2`. Αποτέλεσμα: **0 errors, 0 warnings**, παράγεται
   `ELF 32-bit LSB relocatable, ARM, EABI5`.
3. **Μεγέθη τύπων** με `_Static_assert`: `time_t` = 64-bit (time64), `long` =
   32-bit — άρα το `(long)ts.tv_sec` του CSV είναι σωστό σήμερα, αλλά έχει λανθάνον
   όριο το 2038.

Τρέχει σε δευτερόλεπτα, δεν αγγίζει το `run_tests.sh`, και τα artifacts πάνε στο
`tests/run/armv6/`.

Παγίδες που λύθηκαν μέσα στο script:

- **Χρειάζεται `-marm`**: το Ubuntu cross-gcc βγαίνει σε Thumb-1 mode, που δεν
  υποστηρίζει hard-float VFP ABI →
  `sorry, unimplemented: Thumb-1 'hard-float' VFP ABI`.
- **Δεν μπορείς να προσθέσεις `-I/usr/include/x86_64-linux-gnu`** για να βρεθεί
  το `opensslconf.h` (που στη host ζει στο multiarch dir): «μολύνει» και τα
  `gnu/stubs.h` και σκάει σε `stubs-32.h`. Το script φτιάχνει shim **μόνο** για
  το `openssl/`.

### Γιατί όχι πλήρες ARM execution

Με το `qemu-user-static` και ένα armv6 sysroot θα μπορούσαμε να *τρέξουμε* το
binary. Το πιο πιστό σενάριο: mount του ext4 rootfs της κάρτας SD και chroot σε
αυτό — δηλαδή το **πραγματικό** armv6 userland με το δικό του `libwebsockets`.
Χωρίς την κάρτα, ένα Debian/Ubuntu armhf container θα έδινε **armv7**, όχι armv6,
άρα δεν θα απαντούσε στο ερώτημα που έχει σημασία.

Παρ' όλα αυτά, ακόμα και με πλήρες ARM execution, τα **timing** μεγέθη (jitter,
drift, CPU%) παραμένουν άκυρα — καθορίζονται από τον host scheduler και τα timers
του QEMU, όχι από το Pi. Γι' αυτό τα βήματα 1-3 παραπάνω είναι το χρήσιμο κομμάτι.

---

## 13. Checklist πριν το 24ωρο run στο Pi

```bash
# στο Pi, στο /home/<user>/firehose
make                                    # χτίζει το πραγματικό binary
bash check_pi.sh                        # εξαρτήσεις, NTP, endpoint, 20s δοκιμή
sudo systemctl enable --now bluesky-firehose
systemctl status bluesky-firehose
# μετά από λίγα λεπτά:
tail -5 metrics_log.txt
```

Σημείωση: το `bluesky-firehose.service` έχει `WorkingDirectory=/home/pi/firehose`,
αλλά το cloud-init φτιάχνει χρήστη `vagelismo` — διόρθωσε το path/user πριν το
`systemctl enable`.
