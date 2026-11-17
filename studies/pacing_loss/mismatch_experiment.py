"""Replay capacity steps with optional forward retry waits and reverse delay."""

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
LONG_RISE = "bounded_rise_long"
TRAIN_RISE = "bounded_rise_40kb_train"
SERVER_CREDIT = {"stream_bytes": 512 * 1024, "connection_bytes": 8 * 1024 * 1024}
BOUNDED_CAPACITY = {
    "bounded_fixed": [(0, 20)],
    "bounded_rise": [(0, 20), (CHANGE_US, 30)],
    "bounded_dip": [(0, 20), (CHANGE_US, 10), (1_300_000, 15),
                    (1_320_000, 20), (1_340_000, 30)],
    "bounded_narrow": [(0, 20), (CHANGE_US, 16), (1_300_000, 20), (1_320_000, 24)],
}


def plan_for(case, repeat, gate):
    if case == TRAIN_RISE:
        plan = plan_for("bounded_rise", repeat, gate)
        tasks = plan["tasks"][:1]
        tasks.extend(dict(t=CHANGE_US - 1000 + i * 8000, size=40_000, warmup=False) for i in range(125))
        return dict(plan, case=case, duration=7_000_000, tasks=tasks,
                    server_flow_control=SERVER_CREDIT)
    if case == LONG_RISE:
        plan = plan_for("bounded_rise", repeat, gate)
        plan.update(case=case, duration=4_200_000)
        plan["tasks"][1]["size"] = 3_000_000
        del plan["tasks"][2:]
        return plan
    if case in BOUNDED_CAPACITY:
        capacity = BOUNDED_CAPACITY[case]
        recovery = 1_340_000
    else:
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


def paced_requests(case, repeat, gate):
    plan = plan_for(case, repeat, gate)
    tasks = plan["tasks"][:1]
    tasks.extend(dict(t=CHANGE_US - 1000 + i * 33_000, size=40_000, warmup=False) for i in range(30))
    return dict(plan, tasks=tasks, duration=2_850_000, server_flow_control=SERVER_CREDIT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testbed", type=Path, required=True)
    parser.add_argument("--bbr1", type=Path, help="Optional for the BBRv3-only 40 kB studies")
    parser.add_argument("--bbr3", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", nargs="+", choices=(*CASES, *BOUNDED_CAPACITY, LONG_RISE, TRAIN_RISE),
                        default=list(CASES))
    parser.add_argument("--reverse-delay-ms", type=int, nargs=2, metavar=("MIN", "MAX"),
                        help="Run only the forward-gate groups, with independent uniform reverse delay")
    parser.add_argument("--train-33ms", action="store_true",
                        help="Use 30 consecutive 40 kB requests, submitted every 33 ms")
    args = parser.parse_args()
    if os.geteuid() or args.repeats < 1 or len(args.only) != len(set(args.only)):
        parser.error("requires root, positive repeats and distinct cases")
    reverse_delay = None
    if args.reverse_delay_ms:
        lower, upper = args.reverse_delay_ms
        if not 0 <= lower <= upper:
            parser.error("reverse delay bounds must be nonnegative and in order")
        reverse_delay = dict(min_us=lower * 1000, max_us=upper * 1000)
    if (LONG_RISE in args.only or TRAIN_RISE in args.only) and (len(args.only) != 1 or not reverse_delay):
        parser.error("a sustained rise case requires its own run with reverse delay")
    if args.train_33ms and (not reverse_delay or args.bbr1 or {LONG_RISE, TRAIN_RISE}.intersection(args.only)):
        parser.error("--train-33ms requires reverse delay, BBRv3 only, and standard capacity profiles")
    if args.bbr1 is None and args.only != [TRAIN_RISE] and not args.train_33ms:
        parser.error("--bbr1 is required for this workload")
    make_plan = paced_requests if args.train_33ms else plan_for
    workload = make_plan(args.only[0], 0, False)
    testbed = args.testbed.resolve()
    hashes = testbed_components(testbed)
    sys.path[:0] = [str(testbed / "netemu/src"), str(testbed / "aitestbed")]
    from analysis.logger import TrafficLogger

    target = args.out.resolve()
    target.mkdir(parents=True)
    endpoints = {version: path.resolve(strict=True) for version, path in
                 (("bbr1", args.bbr1), ("bbr3", args.bbr3)) if path is not None}
    loggers = {}
    with topology() as namespaces:
        qdisc = json.loads(output("ip", "netns", "exec", namespaces[1], "tc", "-j", "qdisc", "show"))
        qdiscs = {row["dev"]: row["kind"] for row in qdisc if row["dev"] in ("r0", "r1")}
        if qdiscs != {"r0": "noqueue", "r1": "noqueue"}:
            raise RuntimeError(f"Unexpected router queue discipline: {qdiscs}")
        variants = [(v, gate) for v in endpoints for gate in ((True,) if reverse_delay else (False, True))]
        for version, gate in variants:
            mode = f"{version}-{'gate' if gate else 'rate'}"
            if reverse_delay:
                mode += "-reverse"
            out = target / mode
            prepare(out, "profiles: {}\n")
            logger = TrafficLogger(str(out / "traffic.sqlite"))
            loggers[version, gate] = LabeledLogger(logger) if version == "bbr3" else logger
            dump(out / "environment.json", dict(testbed=TESTBED_COMMIT, picoquic=PIN, testbed_hashes=hashes,
                 kernel=os.uname().release, router_qdiscs=qdiscs, endpoint_hash=sha(endpoints[version]),
                 runner_hash=sha(Path(__file__)), relay_hash=sha(Path(__file__).with_name("relay.py")),
                 cases=args.only, repeats=args.repeats,
                 measured_tasks_per_run=len(workload["tasks"]) - 1, mode=mode,
                 added_delay_us=0, reverse=reverse_delay or "direct", gate=GATE if gate else None))
        for repeat in range(args.repeats):
            for case in args.only:
                for version, gate in (variants if repeat % 2 == 0 else reversed(variants)):
                    mode = f"{version}-{'gate' if gate else 'rate'}"
                    plan = make_plan(case, repeat, gate)
                    if reverse_delay:
                        mode += "-reverse"
                        plan["reverse_delay"] = dict(reverse_delay, seed=plan["seed"] + 1_000_000)
                    print(f"{mode} {case} {repeat}", flush=True)
                    run = argparse.Namespace(out=target / mode, endpoint=endpoints[version])
                    run_case(run, namespaces, case, repeat, None, loggers[version, gate], plan)


if __name__ == "__main__":
    main()
