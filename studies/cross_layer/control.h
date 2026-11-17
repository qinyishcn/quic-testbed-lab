/* Causal rate-control ablations. Native packet recovery remains in picoquic. */
#include <sys/un.h>

typedef struct {
    uint64_t source_us;
    uint64_t bytes_per_second;
    uint64_t kind;
} link_feedback_t;

static int feedback_fd = -1;
static int radio_enabled, outer_enabled, hybrid_enabled;
static uint64_t link_rate[2] = { 2500000, 3750000 };
static double bbr_target;
static uint64_t bbr_quantum;
static FILE *applied;

static void study_cc_init(picoquic_path_t *path, const char *options, uint64_t now)
{
    (void)options;
    (void)now;
    path->cwin = 192 * 1024;
}

static void study_cc_notify(picoquic_cnx_t *cnx, picoquic_path_t *path,
    picoquic_congestion_notification_t notification, picoquic_per_ack_state_t *ack, uint64_t now)
{
    (void)cnx;
    (void)path;
    (void)notification;
    (void)ack;
    (void)now;
}

static void study_cc_delete(picoquic_path_t *path)
{
    (void)path;
}

static void study_cc_observe(picoquic_path_t *path, uint64_t *state, uint64_t *parameter)
{
    (void)path;
    *state = *parameter = 0;
}

static const picoquic_congestion_algorithm_t study_cc = {
    "study_link_feedback", 240, 0, study_cc_init, study_cc_notify, study_cc_delete, study_cc_observe
};

static int setup_feedback(void)
{
    char mode[24];
    FILE *config = fopen("controller.txt", "r");
    if (!config) return -1;
    int parsed = fscanf(config, "%23s", mode);
    fclose(config);
    if (parsed != 1) return -1;
    if (!strcmp(mode, "bbr")) return 0;
    hybrid_enabled = !strcmp(mode, "bbr_radio");
    radio_enabled = hybrid_enabled || !strcmp(mode, "radio") || !strcmp(mode, "dual");
    outer_enabled = !strcmp(mode, "outer") || !strcmp(mode, "dual");
    if (!radio_enabled && !outer_enabled) return -1;
    struct sockaddr_un address = {.sun_family = AF_UNIX, .sun_path = "feedback.sock"};
    feedback_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK, 0);
    if (feedback_fd < 0 || bind(feedback_fd, (struct sockaddr *)&address, sizeof(address))) return -1;
    applied = fopen("applied.csv", "w");
    if (!applied) return -1;
    fputs("us,source_us,kind,reported_Bps,effective_Bps\n", applied);
    return 0;
}

static uint64_t study_rate(void)
{
    uint64_t fast = radio_enabled ? link_rate[0] : 2500000;
    if (hybrid_enabled)
        return bbr_target > 0 && bbr_target < fast ? (uint64_t)bbr_target : fast;
    uint64_t slow = outer_enabled ? link_rate[1] : 3750000;
    return fast < slow ? fast : slow;
}

void study_bbr_pacing(picoquic_path_t *path, double rate, uint64_t quantum)
{
    if (hybrid_enabled) {
        bbr_target = rate;
        bbr_quantum = quantum;
        rate = rate < link_rate[0] ? rate : link_rate[0];
    }
    picoquic_update_pacing_rate(path, rate, quantum);
}

void study_bbr_window_pacing(picoquic_path_t *path, int slow_start)
{
    picoquic_update_pacing_data(path, slow_start);
    if (hybrid_enabled)
        study_bbr_pacing(path, path->pacing.rate, path->pacing.quantum_max);
}

static void apply_feedback(picoquic_path_t *path, uint64_t now)
{
    if (feedback_fd < 0) return;
    link_feedback_t sample;
    while (recv(feedback_fd, &sample, sizeof(sample), 0) == sizeof(sample)) {
        if (sample.kind > 1 || (sample.kind == 0 && !radio_enabled) || (sample.kind == 1 && !outer_enabled))
            continue;
        link_rate[sample.kind] = sample.bytes_per_second;
        fprintf(applied, "%lu,%lu,%lu,%lu,%lu\n", now, sample.source_us,
            sample.kind, sample.bytes_per_second, study_rate());
    }
    uint64_t target = study_rate();
    if (target != path->pacing.rate)
        picoquic_update_pacing_rate(path, (double)target,
            hybrid_enabled && bbr_quantum ? bbr_quantum : 2 * path->send_mtu);
}
