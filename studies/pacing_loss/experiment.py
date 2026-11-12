"""Supplement V3 with paired packet-delay and native loss-decision observations."""

import argparse
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from run import (TESTBED_COMMIT, cmd, dump, gzip_capture, in_namespace, sha,
                 start_capture, stop_process, topology, wait_ready)
from v3.experiment import launch


CASES = ("steady", "one_delay5", "one_delay80", "true_drop", "fifo_pause80",
         "ack_pause80", "jitter5_reorder", "jitter5_fifo", "rack_tail35")
DURATION = 3_700_000


def plan_for(case, repeat):
    tasks = [dict(t=200_000, size=300_000, warmup=True)]
    size = 2400 if case == "rack_tail35" else 40_000
    tasks.extend(dict(t=1_200_000+i*350_000, size=size, warmup=False) for i in range(6))
    impairments = []
    for task in tasks[1:]:
        kind = case
        if case == "steady":
            continue
        if case.startswith("one_delay") or case == "rack_tail35":
            delay = {"one_delay5":5000, "one_delay80":80_000, "rack_tail35":35_000}[case]
            kind, direction, width = "one_delay", "forward", 25_000
        elif case == "true_drop":
            kind, direction, width, delay = "one_drop", "forward", 25_000, 0
        elif case in ("fifo_pause80", "ack_pause80"):
            kind, direction, width, delay = "pause", "reverse" if case.startswith("ack") else "forward", 80_000, 0
        else:
            kind, direction, width, delay = "jitter", "forward", 100_000, 5000
        impairments.append(dict(kind=kind, direction=direction, start_us=task["t"],
            end_us=task["t"]+width, delay_us=delay, fifo=case.endswith("fifo")))
    return dict(case=case, repeat=repeat, seed=46000+repeat, duration=DURATION,
                tasks=tasks, impairments=impairments)


def run_case(args, namespaces, case, repeat, emulator, logger, plan=None):
    from analysis.logger import LogRecord
    work = args.out / f"{case}__{repeat:02d}"
    work.mkdir()
    client, router, server = namespaces
    if plan is None:
        plan = plan_for(case, repeat)
    if "capacity" not in plan:
        with in_namespace(router):
            for interface in ("r0", "r1"):
                shaper = emulator(interface=interface, profiles_path=str(args.out / "profiles.yaml"),
                                  bidirectional=False)
                if not shaper.apply_profile("propagation"):
                    raise RuntimeError("Testbed could not apply the fixed propagation delay")
    # Readiness is checked before either endpoint starts. Handshake has one second before measurements.
    origin = time.monotonic_ns()//1000+600_000
    plan["origin"] = origin
    dump(work / "plan.json", plan)
    with (work / "tasks.tsv").open("w") as handle:
        for task in plan["tasks"]:
            handle.write(f'{task["t"]}\t{task["size"]}\t0\t0\t0\t0\t0\t0\t-\n')
    for role in ("client", "server"):
        (work / ("qlog-"+role)).mkdir()
    processes, captures = [], []
    try:
        relay = launch(router, (sys.executable, Path(__file__).with_name("relay.py"), work,
                               origin, DURATION), work / "relay.log")
        processes.append(relay)
        wait_ready(work / "relay-ready", relay, work / "relay.log")
        for role, namespace, iface in (("client", client, "c0"), ("server", server, "s0")):
            capture = start_capture(namespace, iface, work / (role+".pcap"), work / (role+".capture.txt"), port=4433)
            captures.append(capture)
            deadline = time.monotonic()+3
            while "listening on" not in (work / (role+".capture.txt")).read_text():
                if capture.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("packet capture failed")
                time.sleep(.005)
        for role, namespace in (("server", server), ("client", client)):
            processes.append(launch(namespace, (args.endpoint, role, work, origin, DURATION, "qlog"),
                                    work / (role+".log")))
        for process in processes:
            if process.wait(timeout=8):
                raise RuntimeError(f"{work.name}: child process failed")
    finally:
        for process in processes:
            stop_process(process, signal.SIGTERM)
        for capture in captures:
            stop_process(capture, signal.SIGINT)
    for role in ("client", "server"):
        capture = work / (role+".pcap")
        if capture.stat().st_size <= 24:
            raise RuntimeError("packet capture is empty")
        gzip_capture(capture)
        if "0 packets dropped by kernel" not in (work / (role+".capture.txt")).read_text():
            raise RuntimeError("capture lost packets")
    logger.log(LogRecord(timestamp=time.time(), scenario_id="packet_delay_loss_study",
        session_id=work.name, turn_index=0, network_profile=case, provider="picoquic-bbr1",
        model="fixed-bytes", request_bytes=sum(task["size"] for task in plan["tasks"] if not task["warmup"]),
        success=True))
    print(work.name, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testbed", type=Path, required=True)
    parser.add_argument("--endpoint", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", nargs="+", choices=CASES, default=CASES)
    args = parser.parse_args()
    if os.geteuid():
        parser.error("root is required for isolated network namespaces")
    args.out = args.out.resolve()
    args.endpoint = args.endpoint.resolve()
    args.out.mkdir(parents=True)
    testbed = args.testbed.resolve()
    sys.path[:0] = [str(testbed / "netemu/src"), str(testbed / "aitestbed")]
    from netemu import NetworkEmulator
    from analysis.logger import TrafficLogger
    files = ("netemu/src/netemu/emulator.py", "netemu/src/netemu/profile.py", "aitestbed/analysis/logger.py")
    for name in files:
        expected = subprocess.check_output(["git", "-c", "safe.directory="+str(testbed),
            "-C", str(testbed), "show", f"{TESTBED_COMMIT}:{name}"])
        if (testbed / name).read_bytes().replace(b"\r\n", b"\n") != expected:
            raise RuntimeError(f"Testbed component differs: {name}")
    (args.out / "profiles.yaml").write_text("profiles:\n  propagation: {delay_ms: 1, limit_packets: 4096}\n")
    cmd("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
        "-keyout", str(args.out / "key.pem"), "-out", str(args.out / "cert.pem"), capture_output=True)
    dump(args.out / "environment.json", dict(testbed=TESTBED_COMMIT,
        picoquic="37bb5d28aaea417649a6d4facc15f54de7d55829", kernel=os.uname().release,
        testbed_hashes={name:sha(testbed/name) for name in files}, endpoint_hash=sha(args.endpoint),
        scripts={name:sha(Path(__file__).with_name(name)) for name in ("build.py","experiment.py","relay.py")},
        cases=args.only, repeats=args.repeats, measured_tasks_per_run=6, duration_us=DURATION,
        clock="CLOCK_MONOTONIC in all three namespaces on one WSL2 host"))
    logger = TrafficLogger(str(args.out / "traffic.sqlite"))
    with topology() as namespaces:
        for repeat in range(args.repeats):
            cases = list(args.only)
            random.Random(1990+repeat).shuffle(cases)
            for case in cases:
                run_case(args, namespaces, case, repeat, NetworkEmulator, logger)


if __name__ == "__main__":
    main()
