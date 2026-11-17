"""Replay separate radio, backhaul and return-path impairments on two QUIC endpoints."""

import bisect
import csv
import hashlib
import heapq
import json
import os
import random
import select
import socket
import struct
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from studies.pacing_loss.relay import CLIENT, SERVER, port
from v3.relay import service_finish


class Link:
    def __init__(self, name, schedule, origin, log):
        self.name, self.log = name, log
        offsets, self.rates = zip(*schedule)
        self.times = [origin+t for t in offsets]
        self.pending = deque()
        self.target_bytes = 0

    def rate(self, start, stop):
        index = bisect.bisect_right(self.times, start)-1
        bits = 0
        while start < stop:
            end = min(stop, self.times[index+1]) if index+1 < len(self.times) else stop
            bits += (end-start)*self.rates[index]
            start = end
            index += 1
        return bits

    def put(self, now, packet):
        ident, _, size, _ = packet
        if len(self.pending) >= 1024:
            raise RuntimeError(f"{self.name} queue overflow")
        start = max(now, self.pending[-1][0]) if self.pending else now
        due = service_finish(self.times, self.rates, start, 8*size)
        self.pending.append((due, packet))
        self.target_bytes += size
        self.log.writerow((ident, self.name, "in", now, size, self.target_bytes, round(start), round(due)))

    def release(self, now):
        while self.pending and self.pending[0][0] <= now:
            due, packet = self.pending.popleft()
            self.target_bytes -= packet[2]
            self.log.writerow((packet[0], self.name, "out", now, packet[2], self.target_bytes, "", round(due)))
            yield packet

    @property
    def deadline(self):
        return self.pending[0][0] if self.pending else float("inf")


def feedback_rate(link, kind, sample, interval):
    raw = link.rate(sample-interval, sample) / interval
    if kind == 0:
        cap = raw*.97 - max(0, link.target_bytes-2560)*8/8000
    elif link.target_bytes > 8000:
        cap = min(raw*.95, 30) - (link.target_bytes-8000)*8/60_000
    else:
        cap = raw*.95
    return raw, int(max(1, min(30, cap))*125_000)


def main(work, origin, duration):
    os.chdir(work)
    plan = json.loads(Path("plan.json").read_text())
    rng, reverse_rng = random.Random(plan["seed"]), random.Random(plan["reverse_delay"]["seed"])
    gate = plan["gate"]
    reverse = plan["reverse_delay"]
    reverse_step = plan.get("reverse_step")
    waits, delivery, feedback = [], [], []
    seq = 0
    end = origin + duration + 100_000
    periods, travel = (1000, 20_000), (1000, 20_000)
    next_sample = [origin+200_000, origin+200_000]
    enabled = {"bbr": (), "radio": (0,), "outer": (1,), "dual": (0, 1)}[plan["controller"]]
    with (port("10.206.0.1") as client, port("10.207.0.1") as server,
          socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK) as ipc,
          open("wire.csv", "w", newline="") as wf, open("segments.csv", "w", newline="") as sf,
          open("feedback.csv", "w", newline="") as ff):
        wire, segments, signals = csv.writer(wf), csv.writer(sf), csv.writer(ff)
        wire.writerow(("event", "us", "direction", "id", "udp_bytes", "sha256", "due_us", "action"))
        segments.writerow(("id", "segment", "event", "us", "ip_bytes", "queue_bytes", "start_us", "due_us"))
        signals.writerow(("sample_us", "ready_us", "sent_us", "kind", "observed_mbps",
                          "pending_bytes", "cap_Bps", "delivered"))
        links = (Link("radio", plan["radio"], origin, segments), Link("backhaul", plan["backhaul"], origin, segments))

        def admit(now, packet):
            ident, payload, _, digest = packet
            if gate and rng.random() < gate["probability"]:
                retry = now + gate["wait_us"]
                heapq.heappush(waits, (retry, packet))
                wire.writerow(("wait", now, "forward", ident, len(payload), digest, retry, "gate"))
            else:
                links[0].put(now, packet)

        Path("relay-ready").touch()
        while (now := time.monotonic_ns()//1000) < end:
            for packet in links[0].release(now):
                links[1].put(now, packet)
            for packet in links[1].release(now):
                heapq.heappush(delivery, (now + 4000, packet, False))
            while waits and waits[0][0] <= now:
                _, packet = heapq.heappop(waits)
                admit(now, packet)
            while delivery and delivery[0][0] <= now:
                due, (ident, payload, _, digest), back = heapq.heappop(delivery)
                (client if back else server).sendto(payload, CLIENT if back else SERVER)
                wire.writerow(("out", now, "reverse" if back else "forward", ident,
                               len(payload), digest, due, "return-delay" if back else "two-links"))
            for kind in enabled:
                if next_sample[kind] <= now:
                    rate, cap = feedback_rate(links[kind], kind, now, periods[kind])
                    heapq.heappush(feedback, (now+travel[kind], now, kind, rate, links[kind].target_bytes, cap))
                    next_sample[kind] += max(1, (now-next_sample[kind])//periods[kind]+1)*periods[kind]
            while feedback and feedback[0][0] <= now:
                due, source, kind, rate, queued, cap = heapq.heappop(feedback)
                sent = 0
                try:
                    ipc.sendto(struct.pack("<QQQ", source, cap, kind), "feedback.sock")
                    sent = 1
                except (FileNotFoundError, ConnectionRefusedError, BlockingIOError):
                    pass
                signals.writerow((source, due, now, kind, f"{rate:.4f}", queued, cap, sent))
            wake = min(end, links[0].deadline, links[1].deadline,
                       *(next_sample[i] for i in enabled),
                       *([waits[0][0]] if waits else []), *([delivery[0][0]] if delivery else []),
                       *([feedback[0][0]] if feedback else []))
            readable, _, _ = select.select((client, server), (), (), max(0, wake-now)/1e6)
            for sock in readable:
                for _ in range(32):
                    try:
                        payload, _ = sock.recvfrom(65536)
                    except BlockingIOError:
                        break
                    now = time.monotonic_ns()//1000
                    seq += 1
                    digest = hashlib.sha256(payload).hexdigest()
                    packet = (seq, payload, len(payload)+28, digest)
                    if sock is client:
                        wire.writerow(("in", now, "forward", seq, len(payload), digest, now, "radio-input"))
                        admit(now, packet)
                    else:
                        delay = reverse_rng.randint(reverse["min_us"], reverse["max_us"])
                        if reverse_step and reverse_step[0] <= now-origin < reverse_step[1]:
                            delay += reverse_step[2]
                        due = now + delay
                        wire.writerow(("in", now, "reverse", seq, len(payload), digest, due, "return-delay"))
                        heapq.heappush(delivery, (due, packet, True))
        if waits or delivery or any(link.pending for link in links):
            raise RuntimeError("Undelivered packets at end of observation")


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
