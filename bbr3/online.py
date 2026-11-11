"""Replay real MiniCPM-o requests with visible model output returned over QUIC."""

import argparse
import csv
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "v2")]
from bbr3.experiment import prepare, testbed_components
from run import dump, sha, stop_process, topology
from run_qa import wait_model
from v3.experiment import launch, run_case
from workload import MODEL_PORT, infer, request_body

PROFILES = {"steady20": (20, 20), "short10_30": (30, 10), "high28_30": (28, 30)}


def worker(work, count):
    for index in range(count):
        source = work / f"request-{index}.bin"
        deadline = time.monotonic() + 35
        while not source.is_file():
            if time.monotonic() > deadline:
                raise TimeoutError(f"request {index} did not arrive")
            time.sleep(.001)
        started = False
        with (work / f"reply-{index}.txt").open("wb", buffering=0) as reply:
            def stream(event):
                nonlocal started
                text = event["text"]
                if started or text.strip():
                    started = True
                    reply.write(text.encode())

            result = infer(source.read_bytes(), stream)
        if not started:
            raise RuntimeError(f"request {index} produced no visible answer")
        dump(work / f"inference-{index}.json", result)
        (work / f"reply-{index}.done").touch()


def make_plan(questions, paths, profile):
    tasks = []
    for index, (row, file) in enumerate(zip(questions, paths)):
        tasks.append(dict(t=600_000 + index*3_000_000, size=file.stat().st_size,
                          file=str(file), budget=20_000, rate1=0, rate2=0, step=0,
                          hold=0, window=0, retry=0, question=row["video_id"],
                          question_number=row["question_number"]))
    duration = tasks[-1]["t"] + 4_000_000
    first_rate, second_rate = PROFILES[profile]
    capacity = [(0, first_rate)]
    if first_rate != second_rate:
        rng = random.Random(9911)
        start = 0
        capacity.clear()
        while start < duration:
            dwell = rng.randint(5000, 10000)
            capacity.extend(((start, first_rate), (start+dwell, second_rate)))
            start += 2*dwell
    return dict(tasks=tasks, capacity=capacity, duration=duration, delay_ms=1)


def measure(work, questions, paths, algorithm, profile):
    with (work / "client.csv").open() as handle:
        client = {(int(row["task"]), row["event"]): int(row["us"]) for row in csv.DictReader(handle)}
    with (work / "server.csv").open() as handle:
        server = {int(row["task"]): int(row["us"]) for row in csv.DictReader(handle)}
    for index, (question, path) in enumerate(zip(questions, paths)):
        source = work / f"request-{index}.bin"
        if sha(source) != sha(path):
            raise ValueError(f"request bytes differ in {work.name}/{index}")
        result = json.loads((work / f"inference-{index}.json").read_text())
        model_first = next(round(t*1e6) for t, text in result["tokens"] if text.strip())
        model_start = round(result["infer_start"]*1e6)
        start, first, finish = (client[index, name] for name in ("start", "first", "token"))
        if not start <= server[index] <= model_start <= model_first <= first <= finish:
            raise ValueError(f"invalid first-text time ordering in {work.name}/{index}")
        if index + 1 < len(questions) and finish > client[index+1, "start"]:
            raise ValueError(f"overlapping measured requests in {work.name}/{index}")
        answer = (work / f"answer-{index}.txt").read_text(encoding="utf-8")
        if answer.strip() != "".join(text for _, text in result["tokens"]).strip():
            raise ValueError(f"reply bytes differ in {work.name}/{index}")
        match = re.fullmatch(r"([ABCD])[.)。]?", answer.strip(), re.I)
        prediction = match[1].upper() if match else ""
        yield dict(algorithm=algorithm, profile=profile, video=question["video_id"],
                   question=question["question_number"], bytes=path.stat().st_size, sha256=sha(path),
                   upload_ms=(server[index]-start)/1000, model_dispatch_ms=(model_start-server[index])/1000,
                   model_first_text_ms=(model_first-model_start)/1000,
                   reply_first_text_ms=(first-model_first)/1000, first_text_ms=(first-start)/1000,
                   completion_ms=(finish-start)/1000, prediction=prediction,
                   correct=int(prediction == question["answer"]))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        worker(Path(sys.argv[2]), int(sys.argv[3]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("testbed", "assets", "llama", "build", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--lib-path", default="")
    parser.add_argument("--profiles", choices=tuple(PROFILES), nargs="+", default=tuple(PROFILES))
    args = parser.parse_args()
    if os.geteuid():
        parser.error("requires root to configure the isolated network namespaces")
    for name in ("testbed", "assets", "llama", "build", "out"):
        setattr(args, name, getattr(args, name).resolve())
    testbed_hashes = testbed_components(args.testbed)
    sys.path[:0] = [str(args.testbed / "netemu/src"), str(args.testbed / "aitestbed")]
    from netemu import NetworkEmulator
    from analysis.logger import TrafficLogger

    prepare(args.out, "profiles:\n  propagation: {delay_ms: 1, limit_packets: 1024}\n")
    manifest = args.assets / "results/q4_k_m_four_native/questions_manifest.json"
    selected = (1, 4, 7, 10, 13, 16)
    questions = sorted((r for r in json.loads(manifest.read_text()) if r["video_id"] in selected
                        and r["question_number"] in (1, 2)), key=lambda r: (r["video_id"], r["question_number"]))
    if len(questions) != 12:
        raise ValueError("expected the 12 original StreamingBench questions")
    paths = [args.out / f"input-{i}.bin" for i in range(len(questions))]
    with (ROOT / "results/v2/requests.csv").open(encoding="utf-8") as handle:
        reference_hashes = {r["question"]: r["sha256"] for r in csv.DictReader(handle)}
    for row, path in zip(questions, paths):
        path.write_bytes(request_body(args.assets, row))
        if sha(path) != reference_hashes[row["question_id"]]:
            raise ValueError(f"request differs from the second report: {row['question_id']}")
    model = args.assets / "models/Model-7.6B-Q4_K_M.gguf"
    projector = args.assets / "models/mmproj-model-f16.gguf"
    binaries = {"BBRv1": args.build / "endpoint-online-reference", "BBRv3": args.build / "endpoint-online"}
    dump(args.out / "environment.json", dict(
        testbed_hashes=testbed_hashes, model_sha256=sha(model), projector_sha256=sha(projector),
        llama_sha256=sha(args.llama), endpoint_sha256={name: sha(path) for name, path in binaries.items()},
        model_options=["-ngl", "99", "-c", "4096", "-np", "1", "-b", "512", "-ub", "256", "-t", "4", "-tb", "4"],
        profiles=args.profiles, algorithm_order="alternated by profile", measured_tasks_per_case=len(questions)))
    records = []
    db = TrafficLogger(str(args.out / "traffic.sqlite"))

    class OnlineLogger:
        def log(self, record):
            record.provider = f"picoquic-{record.network_profile.rsplit('_', 1)[-1].lower()}"
            db.log(record)

    with topology() as namespaces, (args.out / "model.log").open("w") as log:
        server = namespaces[-1]
        process = subprocess.Popen(["ip", "netns", "exec", server, str(args.llama),
            "-m", str(model), "--mmproj", str(projector), "-ngl", "99", "-c", "4096", "-np", "1",
            "-b", "512", "-ub", "256", "-t", "4", "-tb", "4", "--host", "127.0.0.1",
            "--port", str(MODEL_PORT), "--no-webui"], stdout=log, stderr=subprocess.STDOUT,
            env=dict(os.environ, LD_LIBRARY_PATH=args.lib_path))
        try:
            wait_model(server, process, args.out / "model.log")
            warmup = "import sys;from pathlib import Path;from workload import infer;infer(Path(sys.argv[1]).read_bytes(),lambda _:None)"
            for path in paths[:2]:
                subprocess.run(["ip", "netns", "exec", server, "env", f"PYTHONPATH={ROOT / 'v2'}:{ROOT}",
                                sys.executable, "-c", warmup, str(path)], check=True)
            for index, profile in enumerate(args.profiles):
                for algorithm in tuple(binaries)[::(-1 if index % 2 else 1)]:
                    case = f"{profile}_{algorithm}"
                    work = args.out / f"{case}__00"
                    helper = launch(server, (sys.executable, Path(__file__).resolve(), "worker", work,
                                             len(questions)), args.out / f"{case}-worker.log")
                    try:
                        run_case(args.out, case, 0, make_plan(questions, paths, profile), namespaces,
                                 binaries[algorithm], NetworkEmulator, OnlineLogger(), True)
                        if helper.wait(timeout=3):
                            raise RuntimeError(f"inference worker failed in {case}")
                    finally:
                        stop_process(helper, signal.SIGTERM)
                    records.extend(measure(work, questions, paths, algorithm, profile))
        finally:
            stop_process(process, signal.SIGTERM)
    with (args.out / "online-requests.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=records[0], lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)


if __name__ == "__main__":
    main()
