"""Replay explicitly logged delay, reordering, and loss on an encrypted QUIC path."""

import argparse
import csv
import hashlib
import heapq
import json
import random
import select
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v3.relay import service_finish


CLIENT = ("10.206.0.2", 4433)
SERVER = ("10.207.0.2", 4433)


def port(address):
    result = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    result.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4*1024*1024)
    result.bind((address, 4433))
    result.setblocking(False)
    return result


def main(work, origin, duration):
    plan = json.loads((work / "plan.json").read_text())
    rate_only = "capacity" in plan
    if rate_only:
        offsets, rates = zip(*plan["capacity"])
        bounds = [origin + t for t in offsets]
        if plan["impairments"] or min(rates) <= 0:
            raise ValueError("Rate-only plans require positive rates and no delay or loss interventions")
    rng = random.Random(plan["seed"])
    gate = plan.get("gate")
    client, server = port("10.206.0.1"), port("10.207.0.1")
    queue, sequence = [], 0
    held = []
    service_end = {"forward": 0, "reverse": 0}
    fifo_end = dict(service_end)
    targeted = set()
    end = origin + duration + 100_000
    (work / "relay-ready").write_text("ready")
    with (work / "wire.csv").open("w", newline="") as handle:
        log = csv.writer(handle)
        log.writerow(("event", "us", "direction", "id", "udp_bytes", "sha256", "due_us", "action"))

        def admit(now, ident, payload, digest):
            if len(queue) >= plan["buffer_packets"]:
                log.writerow(("drop", now, "forward", ident, len(payload), digest, 0, "buffer_full"))
                return
            due = service_finish(bounds, rates, max(now, service_end["forward"]), (len(payload)+28)*8)
            service_end["forward"] = due
            heapq.heappush(queue, (due, ident, "forward", payload, digest, "rate_fifo"))
            log.writerow(("admit", now, "forward", ident, len(payload), digest, round(due), "rate_fifo"))

        def check_gate(now, ident, payload, digest):
            if gate and rng.random() < gate["probability"]:
                retry = now + gate["wait_us"]
                heapq.heappush(held, (retry, ident, payload, digest))
                log.writerow(("wait", now, "forward", ident, len(payload), digest, retry, "gate"))
            else:
                admit(now, ident, payload, digest)

        while (now := time.monotonic_ns()//1000) < end:
            while queue and queue[0][0] <= now:
                due, ident, direction, payload, digest, action = heapq.heappop(queue)
                target, address = (server, SERVER) if direction == "forward" else (client, CLIENT)
                target.sendto(payload, address)
                log.writerow(("out", now, direction, ident, len(payload), digest, round(due), action))
                now = time.monotonic_ns()//1000
            wake = min(end, queue[0][0]) if queue else end
            while held and held[0][0] <= now:
                _, ident, payload, digest = heapq.heappop(held)
                check_gate(now, ident, payload, digest)
                now = time.monotonic_ns()//1000
            if queue:
                wake = min(wake, queue[0][0])
            if held:
                wake = min(wake, held[0][0])
            readable, _, _ = select.select((client, server), (), (), max(0, wake-now)/1e6)
            for sock in readable:
                for _ in range(32):
                    try:
                        payload, _ = sock.recvfrom(65536)
                    except BlockingIOError:
                        break
                    now = time.monotonic_ns()//1000
                    direction = "forward" if sock is client else "reverse"
                    sequence += 1
                    digest = hashlib.sha256(payload).hexdigest()
                    if rate_only:
                        if direction == "reverse":
                            client.sendto(payload, CLIENT)
                            sent = time.monotonic_ns()//1000
                            log.writerow(("in", now, direction, sequence, len(payload), digest, now, "direct"))
                            log.writerow(("out", sent, direction, sequence, len(payload), digest, now, "direct"))
                            continue
                        if "gate" in plan:
                            log.writerow(("in", now, direction, sequence, len(payload), digest, now, "rate_input"))
                            check_gate(now, sequence, payload, digest)
                        else:
                            # Preserve the format consumed by the earlier rate-only study.
                            if len(queue) >= plan["buffer_packets"]:
                                log.writerow(("drop", now, direction, sequence, len(payload), digest, 0, "buffer_full"))
                                continue
                            due = service_finish(bounds, rates, max(now, service_end[direction]), (len(payload)+28)*8)
                            service_end[direction] = due
                            heapq.heappush(queue, (due, sequence, direction, payload, digest, "rate_fifo"))
                            log.writerow(("in", now, direction, sequence, len(payload), digest, round(due), "rate_fifo"))
                        continue
                    # Rate is an IP service budget. 1 bit/us equals 1 Mbps.
                    rate = 20 if direction == "forward" else 1000
                    due = max(now, service_end[direction]) + (len(payload)+28)*8/rate
                    service_end[direction] = due
                    action, extra, preserve = "normal", 0, False
                    for index, impairment in enumerate(plan["impairments"]):
                        start, stop = origin+impairment["start_us"], origin+impairment["end_us"]
                        if direction != impairment["direction"] or not start <= now < stop:
                            continue
                        kind = impairment["kind"]
                        if kind in ("one_delay", "one_drop"):
                            if index in targeted or len(payload) < 1000:
                                continue
                            targeted.add(index)
                        action = kind
                        if kind == "one_drop":
                            break
                        if kind == "one_delay":
                            extra = impairment["delay_us"]
                        elif kind == "jitter":
                            extra = rng.randint(0, impairment["delay_us"])
                            preserve = impairment.get("fifo", False)
                        elif kind == "pause":
                            due = max(due, stop)
                            preserve = True
                        break
                    if action == "one_drop":
                        log.writerow(("drop", now, direction, sequence, len(payload), digest, 0, action))
                        continue
                    due += extra
                    if preserve:
                        due = max(due, fifo_end[direction])
                    fifo_end[direction] = due
                    heapq.heappush(queue, (due, sequence, direction, payload, digest, action))
                    log.writerow(("in", now, direction, sequence, len(payload), digest, round(due), action))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("origin", type=int)
    parser.add_argument("duration", type=int)
    args = parser.parse_args()
    main(args.work, args.origin, args.duration)
