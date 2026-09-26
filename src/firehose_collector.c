/*
 * firehose_collector.c
 *
 * Final Assignment: Real-Time Embedded Systems 2026
 *
 * Multithreaded real-time system that subscribes to the Bluesky Jetstream
 * Firehose (WebSocket), classifies incoming JSON messages and logs per-second
 * metrics to metrics_log.txt.
 *
 * Threads:
 *   1. Producer   - event-driven libwebsockets client, pushes frames into a
 *                   bounded circular queue, reconnects with exponential backoff.
 *   2. Consumer   - wakes on condition variable, parses JSON (jsmn), bumps
 *                   mutex-protected global counters. No I/O in this thread.
 *   3. Logger     - strictly periodic (clock_nanosleep, absolute deadlines),
 *                   snapshots counters, buffer occupancy and CPU usage,
 *                   appends a CSV line to metrics_log.txt.
 *   4. Watchdog   - monitors heartbeats of the other threads; exits the
 *                   process on stall so systemd (Restart=always) restarts it.
 *
 * Build: make
 */

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <libwebsockets.h>

#include "jsmn.h"

#define JETSTREAM_URL "jetstream1.us-east.bsky.network"
#define JETSTREAM_PORT 443
#define JETSTREAM_PATH "/subscribe?wantedCollections=app.bsky.feed.post"

/* Endpoint resolved once at startup. Defaults are exactly the assignment
 * endpoint; the FIREHOSE_* variables exist only so the local mock server
 * (tests/mock_firehose.py) can drive the client during testing. */
static const char *firehose_host = JETSTREAM_URL;
static const char *firehose_path = JETSTREAM_PATH;
static int firehose_port = JETSTREAM_PORT;
static int firehose_ssl = 1;

static void firehose_config_init(void) {
    const char *v;
    if ((v = getenv("FIREHOSE_HOST")) && *v) firehose_host = v;
    if ((v = getenv("FIREHOSE_PATH")) && *v) firehose_path = v;
    if ((v = getenv("FIREHOSE_PORT")) && *v) firehose_port = atoi(v);
    firehose_ssl = (firehose_port == JETSTREAM_PORT);
    if ((v = getenv("FIREHOSE_SSL")) && *v) firehose_ssl = atoi(v) != 0;
}

#define QUEUE_SLOTS 256          /* bounded circular queue size */
#define MAX_FRAME_SIZE 16384     /* max JSON message bytes */

#define LOG_FILE "metrics_log.txt"
#define CONN_LOG_FILE "connection_log.txt"
/* Δευτερεύον αρχείο με ό,τι δεν χωράει στα 8 υποχρεωτικά πεδία του
 * metrics_log.txt: jitter από το CLOCK_MONOTONIC, μέγιστη πληρότητα ουράς
 * μέσα στο δευτερόλεπτο, και σωρευτικές απώλειες/αποκοπές. */
#define DIAG_FILE "diag_log.csv"

#define WATCHDOG_TIMEOUT_SEC 5   /* thread stall threshold */
#define RECONNECT_MIN_DELAY 1    /* seconds */
#define RECONNECT_MAX_DELAY 60   /* seconds */

/* ---------------- Circular queue of JSON frames ---------------- */

typedef struct {
    char buf[QUEUE_SLOTS][MAX_FRAME_SIZE];
    int len[QUEUE_SLOTS];
    int head, tail, count;
    int peak;                /* μέγιστο count από το τελευταίο reset του logger */
    pthread_mutex_t mut;
    pthread_cond_t notEmpty;
    pthread_cond_t notFull;
} frame_queue;

static frame_queue fq = {
    .head = 0, .tail = 0, .count = 0, .peak = 0,
    .mut = PTHREAD_MUTEX_INITIALIZER,
    .notEmpty = PTHREAD_COND_INITIALIZER,
    .notFull = PTHREAD_COND_INITIALIZER,
};

/* Και τα δύο γράφονται από τον producer και διαβάζονται από τον logger,
 * πάντα μέσα στο κρίσιμο τμήμα του fq.mut. */
static long dropped_frames = 0;   /* frames lost because queue was full */
static long truncated_frames = 0; /* frames cut at MAX_FRAME_SIZE */

/* ---------------- Global counters (protected by counters_mut) ---------------- */

static pthread_mutex_t counters_mut = PTHREAD_MUTEX_INITIALIZER;
static long c_commit = 0, c_identity = 0, c_account = 0, c_info = 0;

/* ---------------- Heartbeats for the watchdog ---------------- */

/* Atomics: the watchdog and the owning thread touch these concurrently.
 * Deliberately 4-byte (epoch seconds as int): 8-byte atomics are NOT lock-free
 * on ARMv6, so the Pi Zero W would emit __atomic_load_8/__atomic_store_8 and
 * need -latomic, while 4-byte ones compile to inline LDREX/STREX. Seconds are
 * all the watchdog compares. */
static atomic_int hb_producer, hb_consumer, hb_logger;
static atomic_int shutdown_requested = 0;

static void heartbeat_update(atomic_int *hb) {
    atomic_store_explicit(hb, (int)time(NULL), memory_order_relaxed);
}

/* ---------------- CPU accounting from /proc/stat ---------------- */

typedef struct {
    unsigned long long total, idle;
    int valid;
} cpu_sample;

static void read_cpu(cpu_sample *s) {
    FILE *f = fopen("/proc/stat", "r");
    s->valid = 0;
    if (!f) return;
    unsigned long long u, n, sy, id, iow, irq, sirq, steal;
    if (fscanf(f, "cpu %llu %llu %llu %llu %llu %llu %llu %llu",
               &u, &n, &sy, &id, &iow, &irq, &sirq, &steal) == 8) {
        s->idle = id + iow;
        s->total = u + n + sy + id + iow + irq + sirq + steal;
        s->valid = 1;
    }
    fclose(f);
}

/* ---------------- Connection log ---------------- */

static void conn_log(const char *fmt, ...) {
    FILE *f = fopen(CONN_LOG_FILE, "a");
    if (!f) return;
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    fprintf(f, "%ld.%09ld ", (long)ts.tv_sec, ts.tv_nsec);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fprintf(f, "\n");
    fclose(f);
}

/* ---------------- Producer thread (libwebsockets, event-driven) ---------------- */

static volatile int ws_connected = 0;
static volatile int ws_error = 0;   /* connection attempt failed or closed */

/* libwebsockets hands anything larger than its rx buffer (pt_serv_buf_size)
 * over as several CLIENT_RECEIVE callbacks, so reassemble one whole
 * WebSocket message here before parsing it. Only the producer thread (single
 * connection) touches this. */
static char rx_frame[MAX_FRAME_SIZE];
static size_t rx_len = 0;
static int rx_truncated = 0;   /* το τρέχον μήνυμα δεν χώρεσε στο rx_frame */

static int ws_callback(struct lws *wsi, enum lws_callback_reasons reason,
                       void *user, void *in, size_t len) {
    (void)user;
    switch (reason) {
    case LWS_CALLBACK_CLIENT_ESTABLISHED:
        ws_connected = 1;
        conn_log("CONNECTED to %s:%d", firehose_host, firehose_port);
        lwsl_notice("connected to jetstream\n");
        break;

    case LWS_CALLBACK_CLIENT_RECEIVE: {
        heartbeat_update(&hb_producer);

        if (lws_is_first_fragment(wsi)) { rx_len = 0; rx_truncated = 0; }
        size_t room = sizeof(rx_frame) - 1 - rx_len;
        size_t n = len < room ? len : room;
        if (n < len) rx_truncated = 1;            /* κόπηκε στο MAX_FRAME_SIZE */
        memcpy(rx_frame + rx_len, in, n);
        rx_len += n;
        if (!lws_is_final_fragment(wsi)) break;   /* message continues */
        rx_frame[rx_len] = '\0';

        pthread_mutex_lock(&fq.mut);
        if (rx_truncated) truncated_frames++;
        if (fq.count == QUEUE_SLOTS) {
            /* queue full: drop the frame rather than stalling the network */
            dropped_frames++;
            pthread_mutex_unlock(&fq.mut);
            lwsl_warn("queue full, frame dropped\n");
            rx_len = 0;
            return 0;
        }
        memcpy(fq.buf[fq.tail], rx_frame, rx_len + 1);
        fq.len[fq.tail] = (int)rx_len;
        fq.tail = (fq.tail + 1) % QUEUE_SLOTS;
        fq.count++;
        if (fq.count > fq.peak) fq.peak = fq.count;
        pthread_cond_signal(&fq.notEmpty);
        pthread_mutex_unlock(&fq.mut);
        rx_len = 0;
        break;
    }

    case LWS_CALLBACK_CLIENT_CONNECTION_ERROR:
        ws_connected = 0;
        ws_error = 1;
        conn_log("CONNECTION_ERROR: %s", in ? (const char *)in : "(unknown)");
        break;

    case LWS_CALLBACK_CLIENT_CLOSED:
        ws_connected = 0;
        ws_error = 1;
        conn_log("DISCONNECTED");
        lwsl_notice("connection closed\n");
        break;

    default:
        break;
    }
    return 0;
}

static const struct lws_protocols protocols[] = {
    { "firehose", ws_callback, 0, 0, 0, NULL, 0 },
    LWS_PROTOCOL_LIST_TERM
};

static void *producer_thread(void *arg) {
    (void)arg;
    int backoff = RECONNECT_MIN_DELAY;

    heartbeat_update(&hb_producer);

    /* sleep in 1s slices so the watchdog sees us alive during backoff */
    void backoff_sleep(int secs) {
        for (int i = 0; i < secs && !shutdown_requested; i++) {
            heartbeat_update(&hb_producer);
            sleep(1);
        }
    }

    while (!shutdown_requested) {
        struct lws_context_creation_info info;
        memset(&info, 0, sizeof(info));
        info.port = CONTEXT_PORT_NO_LISTEN;
        info.protocols = protocols;
        info.options = LWS_SERVER_OPTION_DO_SSL_GLOBAL_INIT;  /* required for wss:// client */
        info.fd_limit_per_thread = 1 + 1;
        info.pt_serv_buf_size = 4096;

        struct lws_context *ctx = lws_create_context(&info);
        if (!ctx) {
            conn_log("lws_create_context failed");
            backoff_sleep(backoff);
            if (backoff < RECONNECT_MAX_DELAY) backoff *= 2;
            continue;
        }

        struct lws_client_connect_info cc;
        memset(&cc, 0, sizeof(cc));
        cc.context = ctx;
        cc.address = firehose_host;
        cc.port = firehose_port;
        cc.path = firehose_path;
        cc.host = cc.address;
        cc.origin = cc.address;
        /* no subprotocol: jetstream does not negotiate one */
        cc.ssl_connection = firehose_ssl ? LCCSCF_USE_SSL : 0;

        struct lws *client = lws_client_connect_via_info(&cc);
        if (!client) {
            conn_log("connect attempt failed");
            lws_context_destroy(ctx);
            backoff_sleep(backoff);
            if (backoff < RECONNECT_MAX_DELAY) backoff *= 2;
            continue;
        }

        /* Service the socket until it errors/closes. The handshake completes
         * inside lws_service, so we must NOT stop once established. */
        ws_error = 0;
        time_t established_at = time(NULL);
        int n = 0;
        do {
            heartbeat_update(&hb_producer);
            n = lws_service(ctx, 50);
        } while (n >= 0 && !shutdown_requested && !ws_error);
        ws_connected = 0;
        if (time(NULL) - established_at > 60) backoff = RECONNECT_MIN_DELAY;

        lws_context_destroy(ctx);
        if (!shutdown_requested) {
            conn_log("RECONNECT in %ds", backoff);
            backoff_sleep(backoff);
            if (backoff < RECONNECT_MAX_DELAY) backoff *= 2;
        }
    }
    return NULL;
}

/* ---------------- Consumer thread (event-driven, no I/O) ---------------- */

static int json_string_eq(const char *json, const jsmntok_t *tok, const char *s) {
    return tok->type == JSMN_STRING &&
           (int)strlen(s) == tok->end - tok->start &&
           strncmp(json + tok->start, s, tok->end - tok->start) == 0;
}

static void *consumer_thread(void *arg) {
    (void)arg;
    char frame[MAX_FRAME_SIZE];
    jsmntok_t tokens[256];

    heartbeat_update(&hb_consumer);
    while (!shutdown_requested) {
        pthread_mutex_lock(&fq.mut);
        while (fq.count == 0 && !shutdown_requested) {
            /* Idle is not a stall: wake once a second and refresh the
             * heartbeat, otherwise the watchdog kills us during any network
             * outage (producer backoff goes up to 60s). */
            struct timespec until;
            clock_gettime(CLOCK_REALTIME, &until);
            until.tv_sec += 1;
            pthread_cond_timedwait(&fq.notEmpty, &fq.mut, &until);
            heartbeat_update(&hb_consumer);
        }
        if (fq.count == 0) {  /* shutdown */
            pthread_mutex_unlock(&fq.mut);
            break;
        }
        memcpy(frame, fq.buf[fq.head], (size_t)fq.len[fq.head] + 1);
        fq.head = (fq.head + 1) % QUEUE_SLOTS;
        fq.count--;
        pthread_cond_signal(&fq.notFull);
        pthread_mutex_unlock(&fq.mut);

        heartbeat_update(&hb_consumer);

        /* classify by top-level "kind" (JSMN_PARENT_LINKS: top-level keys
         * are STRING tokens with parent == 0, the root object) */
        memset(tokens, 0, sizeof(tokens));
        jsmn_parser p;
        jsmn_init(&p);
        int r = jsmn_parse(&p, frame, strlen(frame), tokens, 256);
        /* r < 0 means the frame hit MAX_FRAME_SIZE or ran out of token slots.
         * Whatever was parsed is still valid and "kind" sits near the front
         * of a jetstream message, so classify on it instead of discarding. */
        if (r < 0) r = 256;
        if (tokens[0].type != JSMN_OBJECT) {
            pthread_mutex_lock(&counters_mut);
            c_info++;
            pthread_mutex_unlock(&counters_mut);
            continue;
        }
        int kind_found = 0;
        for (int i = 1; i + 1 < r && tokens[i].type != JSMN_UNDEFINED; i++) {
            if (tokens[i].parent == 0 && tokens[i].type == JSMN_STRING &&
                json_string_eq(frame, &tokens[i], "kind")) {
                kind_found = 1;
                const jsmntok_t *val = &tokens[i + 1];
                pthread_mutex_lock(&counters_mut);
                if (json_string_eq(frame, val, "commit"))
                    c_commit++;
                else if (json_string_eq(frame, val, "identity"))
                    c_identity++;
                else if (json_string_eq(frame, val, "account"))
                    c_account++;
                else
                    c_info++;
                pthread_mutex_unlock(&counters_mut);
                break;
            }
        }
        if (!kind_found) {
            pthread_mutex_lock(&counters_mut);
            c_info++;
            pthread_mutex_unlock(&counters_mut);
        }
    }
    return NULL;
}

/* ---------------- systemd watchdog ping (sd_notify, no libsystemd) ---------------- */

static void sd_notify_watchdog(void) {
    const char *sock_path = getenv("NOTIFY_SOCKET");
    if (!sock_path || sock_path[0] == '\0') return;  /* not run by systemd */

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (sock_path[0] == '@') {
        /* abstract socket */
        addr.sun_path[0] = '\0';
        strncpy(addr.sun_path + 1, sock_path + 1, sizeof(addr.sun_path) - 2);
    } else {
        strncpy(addr.sun_path, sock_path, sizeof(addr.sun_path) - 1);
    }

    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return;
    sendto(fd, "WATCHDOG=1", 10, 0,
           (struct sockaddr *)&addr, sizeof(addr));
    close(fd);
}

/* ---------------- Logger thread (strictly periodic, 1 Hz) ---------------- */

static void add_timespec(struct timespec *t, long ns) {
    t->tv_nsec += ns;
    while (t->tv_nsec >= 1000000000L) {
        t->tv_nsec -= 1000000000L;
        t->tv_sec++;
    }
}

static void *logger_thread(void *arg) {
    (void)arg;
    /* Header μόνο όταν το αρχείο δημιουργείται: ένα restart δεν πρέπει να
     * πετάξει γραμμή header στη μέση ενός 24ωρου dataset. */
    FILE *log = fopen(LOG_FILE, "a");
    if (log) {
        if (fseek(log, 0, SEEK_END) == 0 && ftell(log) == 0)
            fprintf(log, "Seconds,Nanoseconds,Commit_Count,Identity_Count,"
                         "Account_Count,Info_Count,Buffer_Occupancy_Pct,"
                         "CPU_Pct\n");
        fclose(log);
    }
    log = fopen(DIAG_FILE, "a");
    if (log) {
        if (fseek(log, 0, SEEK_END) == 0 && ftell(log) == 0)
            fprintf(log, "Seconds,Nanoseconds,Wakeup_Jitter_us,"
                         "Peak_Occupancy_Pct,Dropped_Frames_Total,"
                         "Truncated_Frames_Total\n");
        fclose(log);
    }

    cpu_sample prev = {0, 0, 0}, cur;
    read_cpu(&prev);

    struct timespec next;
    clock_gettime(CLOCK_MONOTONIC, &next);
    heartbeat_update(&hb_logger);

    while (!shutdown_requested) {
        add_timespec(&next, 1000000000L);  /* ideal next deadline */
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);

        /* Jitter: πόσο αργότερα ξυπνήσαμε από την ιδανική προθεσμία.
         * Μετριέται σε CLOCK_MONOTONIC, οπότε δεν το παραμορφώνει NTP slew
         * όπως θα συνέβαινε αν διαφορίζαμε τα CLOCK_REALTIME timestamps. */
        struct timespec woke;
        clock_gettime(CLOCK_MONOTONIC, &woke);
        long jitter_us = (long)(((woke.tv_sec - next.tv_sec) * 1000000000L +
                                 (woke.tv_nsec - next.tv_nsec)) / 1000);

        heartbeat_update(&hb_logger);

        sd_notify_watchdog();

        /* snapshot and reset counters */
        long s_commit, s_identity, s_account, s_info;
        double occupancy, peak_occ;
        long s_dropped, s_truncated;
        pthread_mutex_lock(&counters_mut);
        s_commit = c_commit; c_commit = 0;
        s_identity = c_identity; c_identity = 0;
        s_account = c_account; c_account = 0;
        s_info = c_info; c_info = 0;
        pthread_mutex_unlock(&counters_mut);

        pthread_mutex_lock(&fq.mut);
        /* floating point: με 256 slots το 1 μήνυμα είναι 0.39% και με ακέραια
         * διαίρεση στρογγυλοποιούνταν σε 0, κρύβοντας την πραγματική εικόνα */
        occupancy = 100.0 * (double)fq.count / (double)QUEUE_SLOTS;
        peak_occ = 100.0 * (double)fq.peak / (double)QUEUE_SLOTS;
        fq.peak = fq.count;              /* νέο παράθυρο για το επόμενο δευτ. */
        s_dropped = dropped_frames;
        s_truncated = truncated_frames;
        pthread_mutex_unlock(&fq.mut);

        /* CPU usage delta */
        double cpu_pct = -1.0;
        read_cpu(&cur);
        if (cur.valid && prev.valid && cur.total > prev.total) {
            unsigned long long dt = cur.total - prev.total;
            unsigned long long didle = cur.idle - prev.idle;
            cpu_pct = 100.0 * (double)(dt - didle) / (double)dt;
        }
        prev = cur;

        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);

        log = fopen(LOG_FILE, "a");
        if (log) {
            fprintf(log, "%ld,%ld,%ld,%ld,%ld,%ld,%.2f,%.2f\n",
                    (long)ts.tv_sec, ts.tv_nsec,
                    s_commit, s_identity, s_account, s_info,
                    occupancy, cpu_pct);
            fclose(log);
        }

        log = fopen(DIAG_FILE, "a");
        if (log) {
            fprintf(log, "%ld,%ld,%ld,%.2f,%ld,%ld\n",
                    (long)ts.tv_sec, ts.tv_nsec,
                    jitter_us, peak_occ, s_dropped, s_truncated);
            fclose(log);
        }
    }
    return NULL;
}

/* ---------------- Watchdog thread ---------------- */

static void *watchdog_thread(void *arg) {
    (void)arg;
    while (!shutdown_requested) {
        sleep(2);
        time_t now = time(NULL);
        if (now - hb_producer > WATCHDOG_TIMEOUT_SEC ||
            now - hb_consumer > WATCHDOG_TIMEOUT_SEC ||
            now - hb_logger > WATCHDOG_TIMEOUT_SEC) {
            conn_log("WATCHDOG: stalled thread detected, exiting for restart "
                     "(p=%ld c=%ld l=%ld now=%ld)",
                     (long)(now - hb_producer), (long)(now - hb_consumer),
                     (long)(now - hb_logger), (long)now);
            lwsl_err("watchdog: stalled thread, restarting process\n");
            /* non-zero exit -> systemd Restart=always kicks in */
            _exit(2);
        }
    }
    return NULL;
}

/* ---------------- main ---------------- */

static void on_signal(int sig) {
    (void)sig;
    /* Only an atomic store here: pthread_cond_broadcast() is not
     * async-signal-safe. The consumer's 1s timed wait notices the flag. */
    atomic_store_explicit(&shutdown_requested, 1, memory_order_relaxed);
}

int main(void) {
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);

    firehose_config_init();

    conn_log("STARTED pid=%d", (int)getpid());
    lws_set_log_level(LLL_ERR | LLL_WARN, NULL);

    pthread_t producer, consumer, logger, watchdog;
    pthread_create(&producer, NULL, producer_thread, NULL);
    pthread_create(&consumer, NULL, consumer_thread, NULL);
    pthread_create(&logger, NULL, logger_thread, NULL);
    pthread_create(&watchdog, NULL, watchdog_thread, NULL);

    pthread_join(producer, NULL);
    pthread_join(consumer, NULL);
    pthread_join(logger, NULL);
    /* watchdog re-checks shutdown_requested every 2s, then exits */
    pthread_join(watchdog, NULL);

    pthread_mutex_destroy(&fq.mut);
    pthread_cond_destroy(&fq.notEmpty);
    pthread_cond_destroy(&fq.notFull);
    pthread_mutex_destroy(&counters_mut);

    conn_log("STOPPED pid=%d dropped_frames=%ld", (int)getpid(), dropped_frames);
    return 0;
}
