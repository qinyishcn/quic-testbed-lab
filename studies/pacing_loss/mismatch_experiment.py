"""Replay the same capacity steps with and without per-packet retry waits."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bbr3.experiment import PIN, LabeledLogger, prepare, testbed_components
from run import TESTBED_COMMIT, dump, output, sha, topology
from studies.pacing_loss.experiment import run_case


CASES = {
    "rise": (20, 90, 90),
    "drop": (20, 2, 90),
    "collapse": (20, 0.12, 90),
    "staged": (20, 0.12, 90),
    "rebound": (20, 0.12, 90),
}
CHANGE_US = 1_200_000
RECOVERY_US = 1_330_000
DURATION_US = 2_000_000
GATE = {"probability": .1, "wait_us": 5000}


def plan_for(case, repeat, gate):
    before, low, after = CASES[case]
    recovery = RECOVERY_US + {"staged": 150_000, "rebound": 230_000}.get(case, 0)
    capacity = [(0, before), (CHANGE_US, low)]
    if case == "staged":
        capacity.append((RECOVERY_US, 1))
    if case == "rebound":
        capacity.append((RECOVERY_US, 4))
    capacity.append((recovery, after))
    return dict(case=case, repeat=repeat, seed=61700 + repeat, duration=DURATION_US,
                tasks=[dict(t=200_000, size=300_000, warmup=True),
                       dict(t=CHANGE_US - 1_000, size=200_000, warmup=False),
                       dict(t=recovery + 20_000, size=40_000, warmup=False)],
                capacity=capacity,
                impairments=[], buffer_packets=256, gate=GATE if gate else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testbed", type=Path, required=True)
    parser.add_argument("--bbr1", type=Path, required=True)
    parser.add_argument("--bbr3", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", nargs="+", choices=CASES, default=list(CASES))
    args = parser.parse_args()
    if os.geteuid() or args.repeats < 1 or len(args.only) != len(set(args.only)):
        parser.error("requires root, positive repeats and distinct cases")
    testbed = args.testbed.resolve()
    hashes = testbed_components(testbed)
    sys.path[:0] = [str(testbed / "netemu/src"), str(testbed / "aitestbed")]
    from analysis.logger import TrafficLogger

    target = args.out.resolve()
    target.mkdir(parents=True)
    endpoints = {"bbr1": args.bbr1.resolve(strict=True), "bbr3": args.bbr3.resolve(strict=True)}
    loggers = {}
    with topology() as namespaces:
        qdisc = json.loads(output("ip", "netns", "exec", namespaces[1], "tc", "-j", "qdisc", "show"))
        qdiscs = {row["dev"]: row["kind"] for row in qdisc if row["dev"] in ("r0", "r1")}
        if qdiscs != {"r0": "noqueue", "r1": "noqueue"}:
            raise RuntimeError(f"Unexpected router queue discipline: {qdiscs}")
        variants = [(v, gate) for v in endpoints for gate in (False, True)]
        for version, gate in variants:
            mode = f"{version}-{'gate' if gate else 'rate'}"
            out = target / mode
            prepare(out, "profiles: {}\n")
            logger = TrafficLogger(str(out / "traffic.sqlite"))
            loggers[version, gate] = LabeledLogger(logger) if version == "bbr3" else logger
            dump(out / "environment.json", dict(testbed=TESTBED_COMMIT, picoquic=PIN, testbed_hashes=hashes,
                 kernel=os.uname().release, router_qdiscs=qdiscs, endpoint_hash=sha(endpoints[version]),
                 runner_hash=sha(Path(__file__)), relay_hash=sha(Path(__file__).with_name("relay.py")),
                 cases=args.only, repeats=args.repeats, measured_tasks_per_run=2, mode=mode,
                 added_delay_us=0, reverse="direct", gate=GATE if gate else None))
        for repeat in range(args.repeats):
            for case in args.only:
                for version, gate in (variants if repeat % 2 == 0 else reversed(variants)):
                    mode = f"{version}-{'gate' if gate else 'rate'}"
                    print(f"{mode} {case} {repeat}", flush=True)
                    run = argparse.Namespace(out=target / mode, endpoint=endpoints[version])
                    run_case(run, namespaces, case, repeat, None, loggers[version, gate], plan_for(case, repeat, gate))


if __name__ == "__main__":
    main()
