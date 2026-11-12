"""Two FIFO service stages and causal grant/queue feedback for local MsQuic."""

import bisect
import csv
import json
import os
import select
import socket
import struct
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from v3.relay import SERVER, open_port

SAMPLE_US = 5000
SOCKET = "ipc/msquic_cellular_ratio.sock"


class Stage:
    def __init__(self, capacity, origin, name, log):
        self.times, self.rates = zip(*[(origin+t, rate) for t, rate in capacity])
        self.name, self.log = name, log
        self.queue = deque()
        self.bytes = 0

    def portions(self, start):
        index = max(0, bisect.bisect_right(self.times, start)-1)
        for i in range(index, len(self.times)):
            end = self.times[i+1] if i+1 < len(self.times) else float("inf")
            yield start, end, self.rates[i]
            start = end

    def budget(self, start, end):
        bits = 0
        for left, right, rate in self.portions(start):
            bits += (min(right, end)-left)*rate
            if right >= end:
                break
        return bits/8

    def put(self, now, packet):
        size = len(packet)+28
        if len(self.queue) >= 256:
            self.log.writerow((self.name, "drop", now, size, self.bytes, 0))
            return
        start = max(now, self.queue[-1][0]) if self.queue else now
        remaining = size*8
        for left, right, rate in self.portions(start):
            service = (right-left)*rate
            if remaining <= service:
                due = left+remaining/rate
                break
            remaining -= service
        self.bytes += size
        self.queue.append((due, packet, size))
        self.log.writerow((self.name, "in", now, size, self.bytes, round(due)))

    def release(self, now):
        while self.queue and self.queue[0][0] <= now:
            due, packet, size = self.queue.popleft()
            self.bytes -= size
            self.log.writerow((self.name, "out", now, size, self.bytes, round(due)))
            yield packet

    @property
    def deadline(self):
        return self.queue[0][0] if self.queue else float("inf")


def replay_ratio(elapsed):
    if elapsed < 1_000_000:
        return 1.0
    if elapsed < 2_000_000:
        return 0.0
    if elapsed < 2_300_000:
        return None
    return 1.0


def main(work):
    os.chdir(work)
    plan = json.loads(Path("plan.json").read_text())
    origin = plan["origin_us"]
    next_sample = origin+300_000
    previous = None
    peer = None
    with (open_port("10.206.0.1") as client, open_port("10.207.0.1") as server,
          socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as signal,
          open("wire.csv", "w", newline="") as wire, open("grants.csv", "w", newline="") as grants):
        signal.setblocking(False)
        packets, telemetry = csv.writer(wire), csv.writer(grants)
        packets.writerow(("stage", "event", "us", "ip_bytes", "queue_bytes", "due_us"))
        telemetry.writerow(("slot_start_us", "slot_end_us", "sent_us", "demand_ip_bytes",
                            "grant_ip_bytes", "ratio", "socket_sent"))
        radio = Stage(plan["radio"], origin, "radio", packets)
        backhaul = Stage(plan["backhaul"], origin, "backhaul", packets)
        Path("relay-ready").touch()
        while True:
            now = time.monotonic_ns()//1000
            for packet in radio.release(now):
                backhaul.put(now, packet)
            for packet in backhaul.release(now):
                server.sendto(packet, SERVER)
            if now >= next_sample:
                if previous is not None:
                    start, demand = previous
                    grant = radio.budget(start, next_sample)
                    ratio = min(2.0, grant/demand) if demand else 1.0
                    if plan["profile"] == "signal_probe":
                        ratio = replay_ratio(next_sample-origin)
                    sent = 0
                    if ratio is not None:
                        try:
                            signal.sendto(struct.pack("<d", ratio), SOCKET)
                            sent = 1
                        except (FileNotFoundError, ConnectionRefusedError, BlockingIOError):
                            pass
                    telemetry.writerow((start, next_sample, now, demand, round(grant, 3), ratio, sent))
                previous = (next_sample, radio.bytes)
                # Do not invent missed BSR samples when the host wakes late.
                next_sample += max(1, (now-next_sample)//SAMPLE_US+1)*SAMPLE_US
            if Path("stop").exists() and not radio.queue and not backhaul.queue:
                break
            deadline = min(next_sample, radio.deadline, backhaul.deadline)
            readable, _, _ = select.select((client, server), (), (), max(0, deadline-now)/1e6)
            for sock in readable:
                for _ in range(32):
                    try:
                        packet, addr = sock.recvfrom(65536)
                    except BlockingIOError:
                        break
                    if sock is client:
                        peer = addr
                        radio.put(time.monotonic_ns()//1000, packet)
                    elif peer is not None:
                        client.sendto(packet, peer)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
