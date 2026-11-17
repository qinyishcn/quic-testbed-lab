"""Run a two-bottleneck QUIC experiment on the 6G-Testbed namespace topology."""

import argparse
import json
import math
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bbr3.experiment import PIN, LabeledLogger, prepare, testbed_components
from run import TESTBED_COMMIT, dump, output, sha, topology
from studies.cross_layer.config import (
    COUNT, DURATION, FORWARD_STEP, MODES, PROFILES, REVERSE_STEP, REVISION_MODES,
    REVISION_PROFILES, SLOW, START, TAIL_BACKHAUL,
)
from studies.pacing_loss.experiment import run_case
from studies.pacing_loss.mismatch_experiment import SERVER_CREDIT

def correlated_rates(seed, minimum, maximum, period, correlation):
    rng = random.Random(seed)
    z = rng.gauss(0, 1)
    innovation = math.sqrt(1-correlation**2)
    center = (minimum+maximum)/2
    span = (maximum-minimum)/2
    for t in range(900_000, DURATION, period):
        yield t, center+span*math.erf(z/math.sqrt(2))
        z = correlation*z+innovation*rng.gauss(0, 1)


def plan_for(profile, mode, repeat, revision=False):
    seed = 69811 + repeat
    radio = [(0, 20)]
    if profile in ("radio", "mixed", "mixed-clean", "mixed-feasible") or profile in TAIL_BACKHAUL:
        if revision:
            radio.extend(correlated_rates(seed+200_000, 10, 30, 5000, .65))
        else:
            rng = random.Random(seed+200_000)
            t = 900_000
            while t < DURATION:
                hold = rng.randint(5000, 9000)
                radio.extend(((t, 30), (t+hold, 10)))
                t += 2*hold
    backhaul = [(0, 45)]
    if profile in ("backhaul", "mixed", "mixed-clean", "mixed-feasible"):
        backhaul.extend(((SLOW[0], 12 if profile == "mixed-feasible" else 7.5), (SLOW[1], 45)))
    elif profile in TAIL_BACKHAUL:
        backhaul.extend(correlated_rates(seed+300_000, *TAIL_BACKHAUL[profile], .70))
    tasks = [dict(t=230_000, size=300_000, warmup=True)]
    tasks.extend(dict(t=START + i*33_000, size=40_000, warmup=False) for i in range(COUNT))
    plan = dict(case=profile, controller=mode, repeat=repeat, seed=seed, duration=DURATION, tasks=tasks,
                capacity=radio, radio=radio, backhaul=backhaul, impairments=[],
                gate=None if profile == "mixed-clean" else {"probability": .1, "wait_us": 5000},
                reverse_delay={"min_us": 10000, "max_us": 20000, "seed": seed+1_000_000},
                reverse_step=REVERSE_STEP if profile == "ack" else None,
                server_flow_control=SERVER_CREDIT)
    if revision:
        plan["forward_step"] = FORWARD_STEP if profile == "forward-delay" else None
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("testbed", "endpoint", "out"):
        parser.add_argument(f"--{flag}", type=Path, required=True)
    parser.add_argument("--revision", action="store_true", help="BBRv3 outer loop with correlated radio")
    parser.add_argument("--profiles", nargs="+", choices=set((*PROFILES, *REVISION_PROFILES, *TAIL_BACKHAUL)))
    parser.add_argument("--modes", nargs="+", choices=(*MODES, "bbr_radio"))
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    profiles = REVISION_PROFILES if args.revision else PROFILES
    available = (*profiles, *TAIL_BACKHAUL) if args.revision else profiles
    modes = REVISION_MODES if args.revision else MODES
    args.profiles = args.profiles or profiles
    args.modes = args.modes or modes
    if (os.geteuid() or args.repeats < 1 or len(set(args.profiles)) != len(args.profiles)
            or len(set(args.modes)) != len(args.modes) or not set(args.profiles) <= set(available)
            or not set(args.modes) <= set(modes)):
        parser.error("requires root, positive repeats, and distinct profiles and modes")
    testbed = args.testbed.resolve()
    hashes = testbed_components(testbed)
    sys.path[:0] = [str(testbed / "netemu/src"), str(testbed / "aitestbed")]
    from analysis.logger import TrafficLogger

    target = args.out.resolve()
    endpoint = args.endpoint.resolve(strict=True)
    relay = Path(__file__).with_name("relay.py")
    loggers = {}
    with topology() as names:
        rows = json.loads(output("ip", "netns", "exec", names[1], "tc", "-j", "qdisc", "show"))
        qdiscs = {r["dev"]: r["kind"] for r in rows if r["dev"] in ("r0", "r1")}
        if qdiscs != {"r0": "noqueue", "r1": "noqueue"}:
            raise RuntimeError(f"Unexpected router queue: {qdiscs}")
        for mode in args.modes:
            out = target / mode
            prepare(out, "profiles: {}\n")
            loggers[mode] = LabeledLogger(TrafficLogger(str(out / "traffic.sqlite")))
            dump(out / "environment.json", dict(testbed=TESTBED_COMMIT, picoquic=PIN,
                 testbed_hashes=hashes, kernel=os.uname().release, router_qdiscs=qdiscs,
                 endpoint_hash=sha(endpoint), runner_hash=sha(Path(__file__)), relay_hash=sha(relay),
                 cases=args.profiles, repeats=args.repeats, measured_tasks_per_run=COUNT,
                 mode=mode, reverse={"min_us": 10000, "max_us": 20000}, forward_propagation_us=4000))
        for repeat in range(args.repeats):
            for index, case in enumerate(args.profiles):
                offset = (index+repeat) % len(args.modes)
                for mode in args.modes[offset:]+args.modes[:offset]:
                    run = argparse.Namespace(out=target / mode, endpoint=endpoint, relay=relay)
                    print(f"{mode} {case} {repeat}", flush=True)
                    run_case(run, names, case, repeat, None, loggers[mode],
                             plan_for(case, mode, repeat, args.revision))


if __name__ == "__main__":
    main()
