"""Run paired BBR loss measurements with only forward rate shaping."""

import argparse
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bbr3.experiment import PIN, LabeledLogger, prepare, testbed_components
from run import TESTBED_COMMIT, dump, output, sha, topology
from studies.pacing_loss.experiment import DURATION, run_case


PROFILES = {
    "fixed20": (20, 20, 0),
    "small_fast": (18, 22, 5_000),
    "small_slow": (18, 22, 100_000),
    "wide_fast": (10, 30, 5_000),
    "wide_slow": (10, 30, 100_000),
    "deep_fast": (0.5, 39.5, 5_000),
    "deep_slow": (0.5, 39.5, 100_000),
}
PHASE_US = (0, 2_500, 7_500, 5_000, 8_500, 1_000)


def plan_for(case, repeat):
    low, high, dwell = PROFILES[case]
    tasks = [dict(t=200_000, size=300_000, warmup=True)]
    tasks.extend(dict(t=1_200_000 + 350_000 * i + PHASE_US[(i + repeat) % len(PHASE_US)],
                      size=40_000, warmup=False) for i in range(6))
    capacity = [(0, 20)]
    if dwell:
        capacity.extend((t, high if i % 2 == 0 else low)
                        for i, t in enumerate(range(1_000_000, DURATION, dwell)))
    return dict(case=case, repeat=repeat, seed=46000+repeat, tasks=tasks, duration=DURATION,
                capacity=capacity, impairments=[], buffer_packets=256)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testbed", type=Path, required=True)
    parser.add_argument("--bbr1", type=Path, required=True)
    parser.add_argument("--bbr3", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", nargs="+", choices=PROFILES, default=list(PROFILES))
    args = parser.parse_args()
    if os.geteuid() or args.repeats < 1 or len(args.only) != len(set(args.only)):
        parser.error("requires root, positive repeats and distinct profiles")
    testbed = args.testbed.resolve()
    hashes = testbed_components(testbed)
    sys.path[:0] = [str(testbed / "netemu/src"), str(testbed / "aitestbed")]
    from analysis.logger import TrafficLogger

    target = args.out.resolve()
    target.mkdir(parents=True)
    endpoints = {"bbr1": args.bbr1.resolve(strict=True), "bbr3": args.bbr3.resolve(strict=True)}
    loggers = {}
    for version, endpoint in endpoints.items():
        out = target / version
        prepare(out, "profiles: {}\n")
        logger = TrafficLogger(str(out / "traffic.sqlite"))
        loggers[version] = LabeledLogger(logger) if version == "bbr3" else logger

    with topology() as namespaces:
        qdisc = json.loads(output("ip", "netns", "exec", namespaces[1], "tc", "-j", "qdisc", "show"))
        qdiscs = {row["dev"]: row["kind"] for row in qdisc if row["dev"] in ("r0", "r1")}
        if qdiscs != {"r0": "noqueue", "r1": "noqueue"}:
            raise RuntimeError(f"Unexpected router queue discipline: {qdiscs}")
        for version, endpoint in endpoints.items():
            dump(target / version / "environment.json", dict(testbed=TESTBED_COMMIT, picoquic=PIN,
                 algorithm=f"picoquic-{version}", endpoint_hash=sha(endpoint), kernel=os.uname().release,
                 testbed_hashes=hashes, runner_hash=sha(Path(__file__)), router_qdiscs=qdiscs,
                 relay_hash=sha(Path(__file__).with_name("relay.py")), service_hash=sha(ROOT / "v3/relay.py"),
                 measured_tasks_per_run=6, duration_us=DURATION, cases=args.only, repeats=args.repeats,
                 reverse="direct forwarding", added_delay_us=0, buffer_packets=256,
                 clock="CLOCK_MONOTONIC on one WSL2 host"))
        for repeat in range(args.repeats):
            order = args.only.copy()
            random.Random(1990 + repeat).shuffle(order)
            for i, case in enumerate(order):
                for version in (list(endpoints) if (repeat+i) % 2 == 0 else reversed(endpoints)):
                    print(f"{version} {case} {repeat}", flush=True)
                    run = argparse.Namespace(out=target / version, endpoint=endpoints[version])
                    run_case(run, namespaces, case, repeat, None, loggers[version], plan_for(case, repeat))


if __name__ == "__main__":
    main()
