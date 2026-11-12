"""Run the published MsQuic endpoints on the existing 6G-Testbed topology."""

import argparse
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bbr3.experiment import testbed_components
from run import TESTBED_COMMIT, dump, in_namespace, sha, stop_process, topology, wait_ready
from v3.experiment import launch

PIN = "53cb31a862ca75bd4ee4a500db4161a9b739348b"
OPENSSL = "3638fd45af31a5982d6c279c37afb58799201277"
PROFILES = ("steady", "radio_step", "radio_fast", "backhaul_step", "signal_probe")
UPLOAD = re.compile(r"Result: Upload (\d+) bytes @ (\d+) kbps \(([0-9.]+) ms\)")


def capacity(profile, repeat):
    radio, backhaul = [(0, 30)], [(0, 100)]
    step = [(0, 30), (2_000_000, 10), (6_000_000, 30)]
    if profile == "steady":
        radio = [(0, 20)]
    elif profile == "radio_step":
        radio = step
    elif profile == "backhaul_step":
        backhaul = step
    elif profile == "radio_fast":
        rng = random.Random(2917 + repeat)
        radio = []
        start = 0
        while start <= 35_000_000:
            dwell = rng.randint(5000, 10000)
            radio.extend(((start, 30), (start + dwell, 10)))
            start += 2*dwell
    return radio, backhaul


def server_ready(process, log):
    deadline = time.monotonic()+4
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        output = log.read_text(errors="replace")
        if "Started!" in output:
            return
        time.sleep(.025)
    raise RuntimeError(f"receiver failed readiness: {log}")


def run_case(args, nets, out, profile, mode, repeat, emulator, logger):
    from analysis.logger import LogRecord

    work = out / f"{profile}--{mode}--{repeat:02d}"
    work.mkdir()
    (work / "ipc").mkdir()
    cnet, rnet, snet = nets
    with in_namespace(rnet):
        for interface in ("r0", "r1"):
            shaper = emulator(interface=interface, profiles_path=str(out / "profiles.yaml"), bidirectional=False)
            if not shaper.apply_profile("propagation"):
                raise RuntimeError("6G-Testbed propagation profile could not be applied")
    sender = args.dual if mode == "dual" else args.sender
    processes = []
    try:
        listener = launch(snet, ("stdbuf", "-oL", args.receiver, "-port:4433", "-cc:bbr",
                                  "-exec:maxtput"), work / "receiver.log")
        processes.append(listener)
        server_ready(listener, work / "receiver.log")
        origin = time.monotonic_ns()//1000 + 350_000
        radio, backhaul = capacity(profile, repeat)
        dump(work / "plan.json", dict(profile=profile, mode=mode, repeat=repeat, origin_us=origin,
                                     radio=radio, backhaul=backhaul, upload_bytes=args.bytes))
        relay = launch(rnet, (sys.executable, Path(__file__).with_name("relay.py"), work), work / "relay.log")
        processes.append(relay)
        wait_ready(work / "relay-ready", relay, work / "relay.log")
        if wait := (origin-time.monotonic_ns()//1000)/1e6:
            time.sleep(max(0, wait))
        with (work / "sender.log").open("w") as stream:
            client = subprocess.Popen(["ip", "netns", "exec", cnet, "unshare", "--mount",
                "--propagation", "private", "sh", "-ec",
                'mount --bind "$1" /tmp; shift; exec "$@"', "gbr-cc", str(work / "ipc"),
                str(sender), "-target:10.206.0.1", "-port:4433", "-ip:4", "-cc:bbr", "-ptput:1", "-pctput:1",
                f"-upload:{args.bytes}", f"-cellular:{int(mode != 'bbr')}"],
                cwd=work, stdout=stream, stderr=subprocess.STDOUT,
                env=dict(os.environ, MSQUIC_BBR_LOG=str(work / "native.log"),
                         GBR_OUTER_LOG=str(work / "outer.csv")))
            processes.append(client)
            if client.wait(timeout=75):
                raise RuntimeError(f"sender failed: {work}")
        (work / "stop").touch()
        if relay.wait(timeout=4):
            raise RuntimeError(f"relay failed: {work}")
    finally:
        for process in reversed(processes):
            stop_process(process, signal.SIGTERM)
    result = (work / "sender.log").read_text(errors="replace")
    measurement = UPLOAD.search(result)
    if not measurement or int(measurement[1]) != args.bytes or "Initialized successfully" not in result:
        raise RuntimeError(f"upload did not complete or telemetry did not initialize: {work}")
    record = dict(uploaded=int(measurement[1]), upload_ms=float(measurement[3]),
                  goodput_kbps=int(measurement[2]))
    dump(work / "result.json", record)
    logger.log(LogRecord(timestamp=time.time(), scenario_id="gbr_cc_reproduction", session_id=work.name,
        turn_index=0, network_profile=profile, provider=mode, model="secnetperf",
        request_bytes=args.bytes, success=True))
    print(work.name, record, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("testbed", "sender", "receiver", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--dual", type=Path)
    parser.add_argument("--profiles", nargs="+", choices=PROFILES, default=PROFILES[:-1])
    parser.add_argument("--modes", nargs="+", choices=("bbr", "gbr", "dual"), default=("bbr", "gbr"))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--bytes", type=int, default=20_000_000)
    args = parser.parse_args()
    if os.geteuid():
        parser.error("requires root for isolated network and mount namespaces")
    if args.repeats < 1 or args.bytes < 8 or ("dual" in args.modes and args.dual is None):
        parser.error("invalid repetitions, payload size or dual-loop executable")
    if len(set(args.profiles)) != len(args.profiles) or len(set(args.modes)) != len(args.modes):
        parser.error("each profile and mode must occur once")
    for name in ("testbed", "sender", "receiver", "dual", "out"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    hashes = testbed_components(args.testbed)
    sys.path[:0] = [str(args.testbed / "netemu/src"), str(args.testbed / "aitestbed")]
    from netemu import NetworkEmulator
    from analysis.logger import TrafficLogger

    args.out.mkdir(parents=True)
    (args.out / "profiles.yaml").write_text("profiles:\n  propagation: {delay_ms: 10, limit_packets: 2048}\n")
    binaries = {"sender": args.sender, "receiver": args.receiver}
    if args.dual is not None:
        binaries["dual"] = args.dual
    dump(args.out / "environment.json", dict(upstream=PIN, openssl=OPENSSL,
        testbed=TESTBED_COMMIT, testbed_hashes=hashes, kernel=os.uname().release,
        binaries={key: {"perf": sha(binary), "lib": sha(binary.parent / "libmsquic.so.2")}
                  for key, binary in binaries.items()},
        repeats=args.repeats, profiles=args.profiles, modes=args.modes,
        upload_bytes=args.bytes, sample_period_us=5000, forward_delay_ms=10, reverse_delay_ms=10))
    logger = TrafficLogger(str(args.out / "traffic.sqlite"))
    with topology() as nets:
        for repeat in range(args.repeats):
            for index, profile in enumerate(args.profiles):
                offset = (repeat+index) % len(args.modes)
                for mode in args.modes[offset:] + args.modes[:offset]:
                    run_case(args, nets, args.out, profile, mode, repeat, NetworkEmulator, logger)


if __name__ == "__main__":
    main()
