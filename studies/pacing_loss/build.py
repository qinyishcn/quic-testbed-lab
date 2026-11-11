"""Build observation-only copies of the pinned V3 endpoint and loss detector."""

import argparse
import subprocess
from pathlib import Path


PIN = "37bb5d28aaea417649a6d4facc15f54de7d55829"
ROOT = Path(__file__).resolve().parents[2]


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f"unexpected source anchor: {old[:90]}")
    return source.replace(old, new)


def loss_source(source):
    headers = r'''
#include <inttypes.h>
#include <stdio.h>

static void study_loss_event(const char* event, picoquic_cnx_t* cnx,
    picoquic_packet_t* packet, uint64_t now, int probable, int expired,
    uint64_t deadline)
{
    static FILE* stream = NULL;
    picoquic_path_t* path = packet->send_path;
    if (path == NULL) return;
    if (stream == NULL) {
        stream = fopen(cnx->client_mode ? "client-loss.csv" : "server-loss.csv", "w");
        if (stream == NULL) return;
        fputs("event,us,pc,packet_number,send_us,highest_ack,highest_ack_receive_us,"
            "latest_acked_send_us,srtt_us,rttvar_us,base_timer_us,effective_timer_us,"
            "max_ack_delay_us,backoff,probable,timer_expired,deadline_us,ack_eliciting\n", stream);
    }
    picoquic_packet_context_t* context = &cnx->pkt_ctx[packet->pc];
    fprintf(stream, "%s,%" PRIu64 ",%d,%" PRIu64 ",%" PRIu64 ",%" PRIu64
        ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
        ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%d,%d,%" PRIu64 ",%d\n",
        event, now, (int)packet->pc, packet->sequence_number, packet->send_time,
        context->highest_acknowledged, context->highest_acknowledged_time,
        context->latest_time_acknowledged, path->smoothed_rtt, path->rtt_variant,
        path->retransmit_timer, picoquic_current_retransmit_timer(cnx, path),
        (uint64_t)cnx->remote_parameters.max_ack_delay, path->nb_retransmit,
        probable, expired, deadline, picoquic_is_packet_ack_eliciting(packet));
}
'''
    source = replace_once(source, '#include "tls_api.h"', '#include "tls_api.h"\n' + headers)
    source = replace_once(source, "    return is_probably_lost;",
        '    if (is_probably_lost || *is_timer_expired)\n'
        '        study_loss_event("evaluate", cnx, old_p, current_time,\n'
        '            is_probably_lost, *is_timer_expired, retransmit_time);\n'
        '    return is_probably_lost;')
    anchor = "    picoquic_log_packet_lost(cnx, old_p->send_path, old_p->ptype, old_p->sequence_number,"
    source = replace_once(source, anchor,
        '    study_loss_event("declare", cnx, old_p, current_time,\n'
        '        !timer_based_retransmit, timer_based_retransmit, 0);\n' + anchor)
    return '#define _GNU_SOURCE\n#include <pthread.h>\n' + source


def endpoint_source(source):
    source = replace_once(source, '#include "picoquic_qlog.h"',
        '#include "picoquic_qlog.h"\n#include <openssl/sha.h>')
    source = replace_once(source, "static FILE *events, *samples;", "static FILE *events, *samples, *sent;")
    source = replace_once(source, "    if (!quic) return 2;",
        "    if (!quic) return 2;\n    picoquic_set_log_level(quic, 1);")
    source = replace_once(source, '    fprintf(events, "task,event,us,bytes,hash\\n");',
        '    fprintf(events, "task,event,us,bytes,hash\\n");\n'
        '    sent = fopen(client ? "client-sent.csv" : "server-sent.csv", "w");\n'
        '    if (!sent) return 2;\n'
        '    fprintf(sent, "us,udp_bytes,sha256,srtt_us,rttvar_us,base_timer_us,'
        'effective_timer_us,max_ack_delay_us,backoff,cwnd,inflight,pacing_Bps\\n");')
    anchor = "            if (ret || !len) break;"
    source = replace_once(source, anchor, anchor + r'''
            if (last && last->nb_paths) {
                picoquic_path_t* transmit_path = last->path[0];
                unsigned char digest[SHA256_DIGEST_LENGTH];
                char hex[2*SHA256_DIGEST_LENGTH+1];
                SHA256(buf, len, digest);
                for (size_t j = 0; j < SHA256_DIGEST_LENGTH; j++)
                    sprintf(hex+2*j, "%02x", digest[j]);
                fprintf(sent, "%lu,%zu,%s,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu\n",
                    now, len, hex, transmit_path->smoothed_rtt, transmit_path->rtt_variant,
                    transmit_path->retransmit_timer,
                    picoquic_current_retransmit_timer(last, transmit_path),
                    (uint64_t)last->remote_parameters.max_ack_delay, transmit_path->nb_retransmit,
                    transmit_path->cwin, transmit_path->bytes_in_transit,
                    picoquic_get_pacing_rate(last));
            }
''')
    source = replace_once(source, "    fclose(events);", "    fclose(sent);\n    fclose(events);")
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("picoquic", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    source = args.picoquic.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if commit != PIN:
        raise ValueError("picoquic commit differs from V3")
    endpoint = args.out / "endpoint-observe.c"
    losses = args.out / "loss-observe.c"
    endpoint.write_text(endpoint_source((ROOT / "v3/endpoint.c").read_text()))
    losses.write_text(loss_source((source / "picoquic/loss_recovery.c").read_text()))
    build = source / "build"
    ptls = build / "_deps/picotls-build"
    subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-I", str(source / "picoquic"),
        str(endpoint), str(losses), str(build / "libpicoquic-log.a"), str(build / "libpicoquic-core.a"),
        str(ptls / "libpicotls-openssl.a"), str(ptls / "libpicotls-minicrypto.a"),
        str(ptls / "libpicotls-core.a"), "-lssl", "-lcrypto", "-lpthread", "-lm",
        "-o", str(args.out / "endpoint")], check=True)


if __name__ == "__main__":
    main()
