"""Build isolated BBRv3 endpoints from the same picoquic commit as the BBRv1 study."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from studies.pacing_loss.build import PIN, endpoint_source, loss_source, replace_once

TARGETS = ("endpoint", "endpoint-trace", "endpoint-loss", "endpoint-online", "endpoint-online-reference")


def select_bbr3(source):
    source = replace_once(source, '"picoquic_bbr1.h"', '"picoquic_bbr.h"')
    for old in ("picoquic_bbr1_algorithm);", "picoquic_bbr1_algorithm->alg_observe"):
        source = replace_once(source, old, old.replace("bbr1", "bbr"))
    return source


def online_source(source):
    source = replace_once(source, "static int client, failed;",
                          'static int client, failed;\n#include "online.inc"')
    source = replace_once(source, "        task->received += length;",
                          "        if (length && online_upload(index, bytes, length)) return -1;\n"
                          "        task->received += length;")
    source = replace_once(source, "    if (event == picoquic_callback_stream_fin) {",
                          "    if (client && length && online_receive(index, bytes, length)) return -1;\n"
                          "    if (event == picoquic_callback_stream_fin) {")
    source = replace_once(source, '            return picoquic_add_to_stream(cnx, stream, (const uint8_t *)"A", 1, 1);',
                          "            return online_submit(cnx, index);")
    return replace_once(source, "        int64_t cap_wake = 2000;",
                        "        if (!client && online_send()) { failed = 1; break; }\n"
                        "        int64_t cap_wake = client ? 2000 : 1000;")


def trace_source(source):
    source = '#define _GNU_SOURCE\n#include <pthread.h>\n#include <inttypes.h>\n#include <stdio.h>\n' + source
    anchor = "    if (bbr_state != NULL) {\n        switch (notification) {"
    source = replace_once(source, anchor, anchor.replace("        switch", r'''
        uint64_t prior_pacing = path_x->pacing.rate;
        uint64_t prior_window = path_x->cwin;
        uint64_t prior_bw = bbr_state->bw;
        double prior_gain = bbr_state->pacing_gain;
        int prior_state = bbr_state->state;
        switch'''))
    anchor = "            path_x->is_cc_data_updated = 1;\n        }\n    }\n}"
    trace = r'''
        if (path_x->cnx->client_mode &&
            (prior_pacing != path_x->pacing.rate || prior_window != path_x->cwin ||
            prior_bw != bbr_state->bw || prior_gain != bbr_state->pacing_gain ||
            prior_state != (int)bbr_state->state)) {
            static FILE* trace = NULL;
            if (trace == NULL) {
                trace = fopen("bbr3-events.csv", "w");
                if (trace != NULL) {
                    fputs("us,notification,state_before,state,pace_before_bps,pace_bps,"
                        "cwnd_before_bytes,cwnd_bytes,bw_before_bps,bw_bps,gain_before,gain,"
                        "max_bw_bps,bw_lo_Bps,inflight_hi_bytes,inflight_lo_bytes,min_rtt_us,"
                        "send_quantum_bytes\n", trace);
                }
            }
            if (trace != NULL) {
                const char* reason = "other";
                switch (notification) {
                    case picoquic_congestion_notification_acknowledgement: reason = "ack"; break;
                    case picoquic_congestion_notification_repeat: reason = "loss"; break;
                    case picoquic_congestion_notification_timeout: reason = "timeout"; break;
                    case picoquic_congestion_notification_restart_from_idle: reason = "idle_restart"; break;
                    case picoquic_congestion_notification_lost_feedback: reason = "lost_feedback"; break;
                    case picoquic_congestion_notification_spurious_repeat: reason = "spurious_loss"; break;
                    default: break;
                }
                fprintf(trace, "%" PRIu64 ",%s,%d,%d,%" PRIu64 ",%" PRIu64
                    ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%.9g,%.9g,"
                    "%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n",
                    current_time, reason, prior_state, (int)bbr_state->state, 8*prior_pacing,
                    8*path_x->pacing.rate, prior_window, path_x->cwin, 8*prior_bw, 8*bbr_state->bw,
                    prior_gain, bbr_state->pacing_gain, 8*bbr_state->max_bw, bbr_state->bw_lo,
                    bbr_state->inflight_hi, bbr_state->inflight_lo, bbr_state->min_rtt,
                    bbr_state->send_quantum);
            }
        }
'''
    return replace_once(source, anchor, anchor[:-7] + trace + "    }\n}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("picoquic", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--only", nargs="+", choices=TARGETS, default=TARGETS)
    args = parser.parse_args()
    source, out = args.picoquic.resolve(), args.output.resolve()
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != PIN or subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"], text=True
    ).strip():
        parser.error(f"requires an unmodified picoquic checkout at {PIN}")
    out.mkdir(parents=True, exist_ok=True)
    build = out / "cmake"
    subprocess.run(["cmake", "-S", str(source), "-B", str(build), "-DPICOQUIC_FETCH_PTLS=ON",
                    "-DBUILD_TESTING=OFF", "-DCMAKE_BUILD_TYPE=Release"], check=True)
    subprocess.run(["cmake", "--build", str(build), "-j", os.getenv("JOBS", "8")], check=True)
    picotls = build / "_deps/picotls-build"
    libraries = [build / f"libpicoquic-{name}.a" for name in ("log", "core")]
    libraries.extend(picotls / f"libpicotls-{name}.a" for name in ("openssl", "minicrypto", "core"))

    reference = (ROOT / "v3/endpoint.c").read_text(encoding="utf-8")
    endpoint = select_bbr3(reference)
    plain = out / "endpoint.c"
    plain.write_text(endpoint, encoding="utf-8")
    trace = out / "bbr3-trace.c"
    trace.write_text(trace_source((source / "picoquic/bbr.c").read_text()), encoding="utf-8")
    observed = out / "endpoint-observe.c"
    observed.write_text(endpoint_source(endpoint), encoding="utf-8")
    loss = out / "loss-observe.c"
    loss.write_text(loss_source((source / "picoquic/loss_recovery.c").read_text()), encoding="utf-8")
    online = out / "endpoint-online.c"
    online.write_text(online_source(endpoint), encoding="utf-8")
    online_reference = out / "endpoint-online-reference.c"
    online_reference.write_text(online_source(reference), encoding="utf-8")
    for name, sources in (
        ("endpoint", [plain]),
        ("endpoint-trace", [plain, trace]),
        ("endpoint-loss", [observed, loss]),
        ("endpoint-online", [online]),
        ("endpoint-online-reference", [online_reference]),
    ):
        if name not in args.only:
            continue
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-I", str(source / "picoquic"),
                        "-I", str(Path(__file__).resolve().parent),
                        *map(str, sources + libraries), "-lssl", "-lcrypto", "-lpthread", "-lm",
                        "-o", str(out / name)], check=True)


if __name__ == "__main__":
    main()
