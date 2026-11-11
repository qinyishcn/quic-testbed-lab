"""Replay the original Testbed plans with isolated picoquic BBRv3 binaries."""

import argparse
import hashlib
import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from run import TESTBED_COMMIT, cmd, dump, sha, topology
from studies.pacing_loss import experiment as loss
from studies.pacing_loss.build import PIN
from v3 import experiment as burst
from v3.report_data import get


class LabeledLogger:
    def __init__(self, logger):
        self.logger = logger

    def log(self, record):
        record.provider = "picoquic-bbr3"
        self.logger.log(record)


def testbed_components(testbed):
    files = ("netemu/src/netemu/emulator.py", "netemu/src/netemu/profile.py",
             "aitestbed/analysis/logger.py")
    hashes = {}
    for name in files:
        expected = subprocess.check_output(["git", "-c", f"safe.directory={testbed}",
                                            "-C", str(testbed), "show", f"{TESTBED_COMMIT}:{name}"])
        contents = (testbed / name).read_bytes().replace(b"\r\n", b"\n")
        if contents != expected:
            raise ValueError(f"Testbed component differs from the pinned commit: {name}")
        hashes[name] = hashlib.sha256(contents).hexdigest()
    return hashes


def fixtures_at(path):
    fixtures = get(path)
    reference = get(ROOT / "results/v3/fixtures.json")
    if set(fixtures) != set(reference):
        raise ValueError("JPEG fixture question set differs from the BBRv1 study")
    for question, tiers in fixtures.items():
        for tier in ("low", "high"):
            item = tiers[tier]
            file = Path(item["path"])
            if file.stat().st_size != reference[question][tier]["bytes"] or (
                sha(file) != reference[question][tier]["sha256"]
            ):
                raise ValueError(f"JPEG fixture differs from the BBRv1 study: {question} {tier}")
    return fixtures


def prepare(out, profiles):
    out.mkdir(parents=True)
    (out / "profiles.yaml").write_text(profiles)
    cmd("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
        "-keyout", str(out / "key.pem"), "-out", str(out / "cert.pem"), capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("bursts", "loss", "trace"))
    parser.add_argument("--testbed", type=Path, required=True)
    parser.add_argument("--endpoint", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, help="Original v3 fixtures.json including local paths")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--only", nargs="+")
    args = parser.parse_args()
    if os.geteuid():
        parser.error("requires root to configure the isolated network namespaces")
    args.out = args.out.resolve()
    args.endpoint = args.endpoint.resolve(strict=True)
    testbed = args.testbed.resolve()
    hashes = testbed_components(testbed)
    sys.path[:0] = [str(testbed / "netemu/src"), str(testbed / "aitestbed")]
    from netemu import NetworkEmulator
    from analysis.logger import TrafficLogger

    available = loss.CASES if args.suite == "loss" else burst.CASES + burst.EXTRA_CASES
    defaults = ("A1load_slow",) if args.suite == "trace" else available
    cases = tuple(args.only or defaults)
    repeats = args.repeats if args.repeats is not None else (1 if args.suite == "trace" else 3)
    if repeats < 1 or len(cases) != len(set(cases)) or not set(cases).issubset(available):
        parser.error("choose distinct cases in the selected suite and a positive repeat count")
    if any(case.startswith("C1") for case in cases) and not args.fixtures:
        parser.error("C1 requires the original JPEG fixtures")
    fixtures = fixtures_at(args.fixtures) if args.fixtures else None
    profiles = ("profiles:\n  propagation: {delay_ms: 1, limit_packets: 4096}\n" if args.suite == "loss"
                else "profiles:\n  propagation: {delay_ms: 1, limit_packets: 1024}\n"
                     "  propagation_long: {delay_ms: 6, limit_packets: 1024}\n")
    prepare(args.out, profiles)
    env = dict(testbed=TESTBED_COMMIT, picoquic=PIN, algorithm="picoquic-bbr3", suite=args.suite,
               kernel=os.uname().release, testbed_hashes=hashes, endpoint_hash=sha(args.endpoint),
               cases=cases, repeats=repeats, clock="CLOCK_MONOTONIC in all three WSL2 namespaces")
    if args.suite == "loss":
        env.update(measured_tasks_per_run=6, duration_us=loss.DURATION)
    else:
        env.update(qlog=True)
    dump(args.out / "environment.json", env)
    logger = LabeledLogger(TrafficLogger(str(args.out / "traffic.sqlite")))
    with topology() as namespaces:
        for repeat in range(repeats):
            order = list(cases)
            random.Random((1990 if args.suite == "loss" else 8100) + repeat).shuffle(order)
            for case in order:
                if args.suite == "loss":
                    loss.run_case(args, namespaces, case, repeat, NetworkEmulator, logger)
                else:
                    plan = burst.scenario(case, 9911 + repeat, fixtures)
                    burst.run_case(args.out, case, repeat, plan, namespaces, args.endpoint,
                                   NetworkEmulator, logger, repeat == 0)


if __name__ == "__main__":
    main()
