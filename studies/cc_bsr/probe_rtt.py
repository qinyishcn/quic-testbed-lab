"""Observe native BBRv3 ProbeRTT after a step in the simulated return-path delay."""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bbr3.experiment import PIN, LabeledLogger, prepare, testbed_components
from run import TESTBED_COMMIT, cmd, dump, sha, topology
from v3.experiment import run_case, scenario


CASE = "A1load_probe_rtt"
STEP_US = 1_000_000


def step_return_delay(out, router):
    work = out / f"{CASE}__00"
    plan_path = work / "plan.json"
    deadline = time.monotonic() + 4
    while True:
        try:
            plan = json.loads(plan_path.read_text())
            break
        except (FileNotFoundError, json.JSONDecodeError):
            if time.monotonic() > deadline:
                raise TimeoutError("Testbed did not publish the run plan")
            time.sleep(.01)
    target = plan["origin"] + STEP_US
    time.sleep(max(0, (target - time.monotonic_ns() // 1000) / 1e6))
    start = time.monotonic_ns() // 1000 - plan["origin"]
    cmd("ip", "netns", "exec", router, "tc", "qdisc", "change", "dev", "r0", "root",
        "netem", "delay", "11ms", "limit", "1024")
    dump(work / "delay-step.json", dict(scheduled_us=STEP_US, command_start_us=start,
         command_end_us=time.monotonic_ns() // 1000 - plan["origin"], initial_ms=1, final_ms=11))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testbed", type=Path, required=True)
    parser.add_argument("--endpoint", type=Path, required=True, help="BBRv3 endpoint-trace binary")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid():
        parser.error("requires root to configure the isolated network namespaces")
    testbed, endpoint, out = args.testbed.resolve(), args.endpoint.resolve(strict=True), args.out.resolve()
    hashes = testbed_components(testbed)
    sys.path[:0] = [str(testbed / "netemu/src"), str(testbed / "aitestbed")]
    from netemu import NetworkEmulator
    from analysis.logger import TrafficLogger

    prepare(out, "profiles:\n  propagation: {delay_ms: 1, limit_packets: 1024}\n")
    dump(out / "environment.json", dict(testbed=TESTBED_COMMIT, picoquic=PIN, kernel=os.uname().release,
         testbed_hashes=hashes, endpoint_hash=sha(endpoint), algorithm="picoquic-bbr3"))
    plan = scenario("A1load_slow", 9911, None)
    plan["capacity"] = [(0, 30)]
    logger = LabeledLogger(TrafficLogger(str(out / "traffic.sqlite")))
    with topology() as namespaces, ThreadPoolExecutor(max_workers=1) as executor:
        step = executor.submit(step_return_delay, out, namespaces[1])
        run_case(out, CASE, 0, plan, namespaces, endpoint, NetworkEmulator, logger, qlog=False)
        step.result()


if __name__ == "__main__":
    main()
