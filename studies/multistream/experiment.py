"""Run simultaneous 40 kB streams on one QUIC BBRv3 connection over a single radio queue."""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bbr3.experiment import PIN, LabeledLogger, prepare, testbed_components
from run import TESTBED_COMMIT, dump, output, sha, topology
from studies.cross_layer.experiment import correlated_rates
from studies.pacing_loss.experiment import run_case
from studies.pacing_loss.mismatch_experiment import SERVER_CREDIT

CASE = "four-stream"
FRAME_BYTES = 40_000
STREAMS = 4
BATCHES = 8
DURATION_US = 3_850_000


def plan_for(repeat):
    seed = 86_100+repeat
    tasks = [dict(t=230_000, size=160_000, warmup=True)]
    for batch in range(BATCHES):
        tasks.extend(dict(t=1_100_000+300_000*batch, size=FRAME_BYTES, warmup=False, batch=batch+1)
                     for _ in range(STREAMS))
    rates = [(0, 20), *correlated_rates(seed+200_000, 10, 30, 5000, .65, DURATION_US)]
    return dict(case=CASE, controller="bbr", provider="picoquic-bbr3", repeat=repeat, seed=seed,
                duration=DURATION_US, tasks=tasks, capacity=rates, impairments=[], buffer_packets=1024,
                gate={"probability": .1, "wait_us": 5000}, server_flow_control=SERVER_CREDIT,
                reverse_delay={"min_us": 0, "max_us": 0, "seed": seed+1_000_000})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("testbed", "endpoint", "out"):
        parser.add_argument(f"--{flag}", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=24)
    args = parser.parse_args()
    if os.geteuid() or args.repeats < 1:
        parser.error("requires root and positive repeats")
    args.out = args.out.resolve()
    args.endpoint = args.endpoint.resolve(strict=True)
    testbed = args.testbed.resolve()
    hashes = testbed_components(testbed)
    sys.path[:0] = [str(testbed/"netemu/src"), str(testbed/"aitestbed")]
    from analysis.logger import TrafficLogger

    relay = ROOT/"studies/pacing_loss/relay.py"
    with topology() as names:
        import json
        router = json.loads(output("ip", "netns", "exec", names[1], "tc", "-j", "qdisc", "show"))
        qdiscs = {r["dev"]: r["kind"] for r in router if r["dev"] in ("r0", "r1")}
        if qdiscs != {"r0": "noqueue", "r1": "noqueue"}:
            raise RuntimeError(f"Unexpected router queue: {qdiscs}")
        prepare(args.out, "profiles: {}\n")
        logger = LabeledLogger(TrafficLogger(str(args.out/"traffic.sqlite")))
        dump(args.out/"environment.json", dict(testbed=TESTBED_COMMIT, picoquic=PIN,
             testbed_hashes=hashes, kernel=os.uname().release, router_qdiscs=qdiscs,
             endpoint_hash=sha(args.endpoint), runner_hash=sha(Path(__file__)), relay_hash=sha(relay),
             cases=[CASE], repeats=args.repeats, measured_tasks_per_run=STREAMS*BATCHES,
             batches_per_run=BATCHES, streams_per_batch=STREAMS, frame_bytes=FRAME_BYTES,
             reverse={"min_us": 0, "max_us": 0}, forward_propagation_us=0, forward_queues=1))
        args.relay = relay
        for repeat in range(args.repeats):
            run_case(args, names, CASE, repeat, None, logger, plan_for(repeat))


if __name__ == "__main__":
    main()
