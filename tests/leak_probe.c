/*
 * leak_probe.c — τοπικός έλεγχος διαρροής για τις ΧΡΟΝΟΕΞΑΡΤΩΜΕΝΕΣ λειτουργίες
 * του firehose_collector.
 *
 * Γιατί ξεχωριστό πρόγραμμα: οι ύποπτοι (άνοιγμα/κλείσιμο αρχείων κάθε
 * δευτερόλεπτο, socket κάθε δευτερόλεπτο για το sd_notify, lws context ανά
 * επανασύνδεση) εκτελούνται ΜΙΑ φορά το δευτερόλεπτο. Δεν επιταχύνονται με
 * περισσότερα μηνύματα, οπότε σε πραγματικό χρόνο θέλεις ώρες. Εδώ απλώς
 * επαναλαμβάνουμε την ίδια πράξη όσες φορές θα γινόταν σε X ώρες:
 *
 *   86400 iterations == 24 ώρες λειτουργίας του collector (1 Hz tick)
 *
 * Χρήση:  ./leak_probe <a|b|c|all> <iterations>
 *   a = logger file churn : fopen/fprintf/fclose σε metrics_log.txt + diag_log.csv
 *   b = sd_notify churn   : socket/sendto/close προς unix datagram
 *   c = lws reconnect     : lws_create_context + lws_context_destroy (SSL init)
 *
 * Τυπώνει το VmRSS ανά 10% των επαναλήψεων, ώστε να φαίνεται η ΚΛΙΣΗ και όχι
 * μόνο το τελικό νούμερο (μια διαρροή που ισιώνει είναι high-water mark,
 * μια γραμμική αύξηση είναι διαρροή).
 *
 * Σημείωση: οι λειτουργίες αντιγράφουν πιστά αυτές του firehose_collector.c
 * (ίδια flags, ίδια σειρά, ίδιο μέγεθος γραμμής). Ο consumer δεν κάνει καμία
 * δέσμευση μνήμης ανά μήνυμα (static buffers + jsmn), οπότε δεν υπάρχει
 * message-based ύποπτος να ελεγχθεί εδώ.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dirent.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <libwebsockets.h>

#define METRICS "probe_metrics.txt"
#define DIAG    "probe_diag.csv"

static int fd_base = 0;   /* αριθμός fds στην αρχή της τρέχουσας φάσης */

static long rss_kb(void) {
    FILE *f = fopen("/proc/self/status", "r");
    if (!f) return -1;
    char line[256];
    long kb = -1;
    while (fgets(line, sizeof(line), f))
        if (sscanf(line, "VmRSS: %ld kB", &kb) == 1) break;
    fclose(f);
    return kb;
}

/* Αριθμός ανοιχτών file descriptors. Απαραίτητο γιατί μια διαρροή fd (π.χ.
 * eventfd ή socket που δεν κλείνει) ΔΕΝ φαίνεται ούτε στο RSS ούτε στο
 * mallinfo: είναι αντικείμενο του πυρήνα, όχι του heap της διεργασίας. */
static int fd_count(void) {
    DIR *d = opendir("/proc/self/fd");
    if (!d) return -1;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(d))) if (e->d_name[0] != '.') n++;
    closedir(d);
    return n - 1;   /* αφαιρούμε το fd του ίδιου του opendir */
}

static void report(const char *phase, long i, long n, long base, long prev) {
    long now = rss_kb();
    (void)prev;   /* ιστορικό: τώρα μετράμε fds αντί για βήμα RSS */
    printf("  [%s] %7ld/%-7ld  RSS=%ld KB  fds=%-4d  (RSS%+ld, fds%+d)\n",
           phase, i, n, now, fd_count(), now - base, fd_count() - fd_base);
    fflush(stdout);
}

/* ---- a: logger file churn (metrics_log.txt + diag_log.csv κάθε δευτερόλεπτο) */
static void phase_a(long n) {
    long base = rss_kb(), prev = base;
    fd_base = fd_count();
    for (long i = 0; i < n; i++) {
        FILE *f = fopen(METRICS, "a");
        if (f) {
            fprintf(f, "%ld,%ld,%d,%d,%d,%d,%.2f,%.2f\n",
                    (long)time(NULL), 123456789L, 10, 2, 1, 4, 0.39, 12.34);
            fclose(f);
        }
        f = fopen(DIAG, "a");
        if (f) {
            fprintf(f, "%ld,%ld,%d,%.2f,%d,%d\n",
                    (long)time(NULL), 123456789L, 103, 0.78, 0, 0);
            fclose(f);
        }
        if (n >= 10 && i % (n / 10) == 0 && i) { report("a", i, n, base, prev); prev = rss_kb(); }
    }
    report("a", n, n, base, prev);
}

/* ---- b: sd_notify (socket + sendto + close κάθε δευτερόλεπτο) */
static void phase_b(long n) {
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strcpy(addr.sun_path, "/tmp/probe_notify.sock");

    long base = rss_kb(), prev = base;
    fd_base = fd_count();
    for (long i = 0; i < n; i++) {
        int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
        if (fd >= 0) {
            sendto(fd, "WATCHDOG=1", 10, 0,
                   (struct sockaddr *)&addr, sizeof(addr));
            close(fd);
        }
        if (n >= 10 && i % (n / 10) == 0 && i) { report("b", i, n, base, prev); prev = rss_kb(); }
    }
    report("b", n, n, base, prev);
}

/* ---- c: lws create/destroy (η διαδρομή κάθε επανασύνδεσης) */
static int probe_cb(struct lws *wsi, enum lws_callback_reasons reason,
                    void *user, void *in, size_t len) {
    (void)wsi; (void)user; (void)in; (void)len; (void)reason;
    return 0;
}
static const struct lws_protocols probe_protocols[] = {
    { "probe", probe_cb, 0, 0, 0, NULL, 0 },
    LWS_PROTOCOL_LIST_TERM
};

static void phase_c(long n) {
    long base = rss_kb(), prev = base;
    fd_base = fd_count();
    for (long i = 0; i < n; i++) {
        struct lws_context_creation_info info;
        memset(&info, 0, sizeof(info));
        info.port = CONTEXT_PORT_NO_LISTEN;
        info.protocols = probe_protocols;
        info.options = LWS_SERVER_OPTION_DO_SSL_GLOBAL_INIT;
        info.fd_limit_per_thread = 1 + 1;
        info.pt_serv_buf_size = 4096;

        struct lws_context *ctx = lws_create_context(&info);
        if (ctx) lws_context_destroy(ctx);
        if (n >= 10 && i % (n / 10) == 0 && i) { report("c", i, n, base, prev); prev = rss_kb(); }
    }
    report("c", n, n, base, prev);
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s <a|b|c|all> <iterations>\n", argv[0]);
        return 2;
    }
    const char *phase = argv[1];
    long n = atol(argv[2]);
    lws_set_log_level(LLL_ERR, NULL);

    printf("αρχικό RSS = %ld KB | phase=%s iterations=%ld\n", rss_kb(), phase, n);
    if (!strcmp(phase, "a") || !strcmp(phase, "all")) phase_a(n);
    if (!strcmp(phase, "b") || !strcmp(phase, "all")) phase_b(n);
    if (!strcmp(phase, "c") || !strcmp(phase, "all")) phase_c(n);
    printf("τελικό RSS  = %ld KB\n", rss_kb());
    return 0;
}
