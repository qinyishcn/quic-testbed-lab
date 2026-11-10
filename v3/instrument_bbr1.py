"""Build an observation-only copy of the pinned BBRv1 source for A1 diagnosis."""

import argparse
from pathlib import Path


def instrument(source):
    start = source.index("static void picoquic_bbr1_notify(")
    end = source.index("/* Observe the state of congestion control */", start)
    function = source[start:end]
    before = "    path_x->is_cc_data_updated = 1;"
    if function.count(before) != 1:
        raise ValueError("unexpected BBRv1 notification handler")
    function = function.replace(before, before + r"""
    uint64_t trace_rate = path_x->pacing.rate;
    uint64_t trace_bw = bbr1_state ? bbr1_state->btl_bw : 0;
    double trace_gain = bbr1_state ? bbr1_state->pacing_gain : 0;
    unsigned int trace_phase = bbr1_state ? bbr1_state->cycle_index : 0;
    int trace_round = bbr1_state ? bbr1_state->round_count : 0;
    unsigned int trace_state = bbr1_state ? bbr1_state->state : 0;
    uint64_t trace_cwnd = path_x->cwin;
    uint64_t trace_quantum = bbr1_state ? bbr1_state->send_quantum : 0;
""")
    end_brace = function.rindex("}")
    function = function[:end_brace] + r"""
    if (cnx->client_mode && bbr1_state && trace_rate != path_x->pacing.rate) {
        static FILE* trace = NULL;
        if (trace == NULL) {
            trace = fopen("bbr-native.csv", "w");
            if (trace != NULL) {
                fputs("us,notification,rate_before_bps,rate_after_bps,bw_before_bps,bw_after_bps,"
                    "gain_before,gain_after,phase_before,phase_after,round_before,round_after,"
                    "state_before,state_after,delivery_sample_bps,cwnd_before,cwnd_after,min_rtt_us,"
                    "quantum_before_bytes,quantum_after_bytes,send_mtu_bytes\n", trace);
            }
        }
        if (trace != NULL) {
            fprintf(trace, "%" PRIu64 ",%s,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
                ",%.9g,%.9g,%u,%u,%d,%d,%u,%u,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
                ",%" PRIu64 ",%" PRIu64 ",%zu\n",
                current_time, notification == picoquic_congestion_notification_acknowledgement ? "ack" : "other",
                trace_rate * 8, path_x->pacing.rate * 8, trace_bw * 8, bbr1_state->btl_bw * 8,
                trace_gain, bbr1_state->pacing_gain, trace_phase, bbr1_state->cycle_index,
                trace_round, bbr1_state->round_count, trace_state, bbr1_state->state,
                path_x->bandwidth_estimate * 8, trace_cwnd, path_x->cwin, bbr1_state->rt_prop,
                trace_quantum, bbr1_state->send_quantum, (size_t)path_x->send_mtu);
        }
    }
""" + function[end_brace:]
    headers = "#include <inttypes.h>\n#include <pthread.h>\n#include <stdio.h>\n"
    return headers + source[:start] + function + source[end:]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(instrument(args.source.read_text(encoding="utf-8")), encoding="utf-8")
