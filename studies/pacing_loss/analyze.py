"""Join every QUIC packet, ACK, native loss decision, and relay intervention."""

import argparse
import csv
import gzip
import hashlib
import io
import json
import sqlite3
import sys
import tarfile
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write, get, packets as read_capture, quantile


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def qlog_events(work, role):
    files = list((work / ("qlog-"+role)).glob("*.qlog"))
    if len(files) != 1:
        raise ValueError(f"expected one complete qlog for {work.name}/{role}")
    trace = get(files[0])["traces"][0]
    if trace["configuration"]["time_units"] != "us":
        raise ValueError("unexpected qlog timestamp unit")
    base = int(trace["common_fields"]["reference_time"])
    return [(base+event[0], event[2], event[3]) for event in trace["events"]]


def packet_key(data):
    return data["packet_type"], int(data.get("packet_number", data.get("header", {}).get("packet_number", -1)))


def stream_frames(data):
    return [(int(frame["id"]), int(frame.get("offset", 0)), int(frame.get("length", 0)))
            for frame in data.get("frames", []) if frame["frame_type"] == "stream"]


def overlap_bytes(frames, history):
    repeated = 0
    for stream, offset, length in frames:
        end = offset+length
        intersections = [(max(offset, left), min(end, right)) for left, right in history[stream]
                         if offset < right and left < end]
        boundary = offset
        for left, right in sorted(intersections):
            repeated += max(0, right-max(left, boundary))
            boundary = max(boundary, right)
        history[stream].append((offset, end))
    return repeated


def acknowledgement_times(events, outstanding):
    result = {}
    for when, name, data in events:
        if name != "packet_received":
            continue
        packet_type = data["packet_type"]
        for frame in data.get("frames", []):
            if frame["frame_type"] != "ack":
                continue
            for bounds in frame["acked_ranges"]:
                low, high = min(bounds), max(bounds)
                for key in outstanding:
                    if key[0] == packet_type and low <= key[1] <= high and key not in result:
                        result[key] = (when, frame.get("ack_delay", 0), packet_key(data))
    return result


def loss_decisions(work, role):
    evaluated, declared = {}, {}
    for raw in read_csv(work / (role+"-loss.csv")):
        row = {key:int(value) if key != "event" else value for key,value in raw.items()}
        key = row["pc"], row["packet_number"], row["us"]
        if row["event"] == "evaluate":
            evaluated[key] = row
        else:
            observation = evaluated.get(key)
            if observation is None:
                raise ValueError(f"a declaration lacks its actual decision in {work.name}: {key}")
            if row["timer_expired"]:
                reason = "timer"
            elif row["highest_ack"]-row["packet_number"] >= 3:
                reason = "packet_number"
            elif row["highest_ack"] > row["packet_number"]:
                reason = "rack_time"
            else:
                reason = "other"
            observation["reason"] = reason
            declared[(row["send_us"], row["packet_number"])] = observation
    return declared


def wire_delays(work):
    """Join NIC captures by encrypted UDP prefix across the relay's IP rewrite."""
    client = list(read_capture(work / "client.pcap.gz",0))
    server = list(read_capture(work / "server.pcap.gz",0))
    forward = [row for row in client if row[1] == "forward"]
    forward_keys = {row[3][8:] for row in forward}
    forward_receive = [row for row in server if row[3][8:] in forward_keys]
    reverse = [row for row in server if row[3][8:] not in forward_keys]
    reverse_receive = [row for row in client if row[1] == "reverse"]
    result = {}
    for role,source,target in (("client",forward,forward_receive),("server",reverse,reverse_receive)):
        observation = read_csv(work / (role+"-sent.csv"))
        source_keys = {row[3][8:] for row in source}
        received = {row[3][8:]:row[0] for row in target}
        if (len(source) != len(observation) or len(source_keys) != len(source)
                or len(received) != len(target) or not received.keys() <= source_keys):
            raise ValueError(f"NIC capture coverage differs from endpoint UDP sends: {work.name}/{role}")
        for row, meta in zip(source,observation):
            if row[2] != int(meta["udp_bytes"])+28:
                raise ValueError("capture and endpoint disagree on UDP size")
            receive = received.get(row[3][8:])
            result[meta["sha256"]] = round((receive-row[0])*1000,6) if receive is not None else None
    return result


def analyze_direction(work, plan, role, pcap_delays):
    peer = "server" if role == "client" else "client"
    events, receiver = qlog_events(work, role), qlog_events(work, peer)
    sent = {(packet_key(data)):(when, data) for when,name,data in events if name == "packet_sent"}
    peer_sent = {packet_key(data):when for when,name,data in receiver if name == "packet_sent"}
    received = defaultdict(list)
    for when,name,data in receiver:
        if name == "packet_received":
            received[packet_key(data)].append(when)
    acknowledgements = acknowledgement_times(events, sent)
    metadata = defaultdict(list)
    send_observations = read_csv(work / (role+"-sent.csv"))
    for row in send_observations:
        metadata[int(row["us"])].append(row)
    if Counter(int(row["us"]) for row in send_observations) != Counter(
            when for when,name,_ in events if name == "datagram_sent"):
        raise ValueError(f"qlog does not cover every UDP send in {work.name}/{role}")
    declarations = loss_decisions(work, role)
    wire = read_csv(work / "wire.csv")
    ingress = {row["sha256"]:row for row in wire if row["event"] in ("in", "drop")}
    egress = {row["sha256"]:row for row in wire if row["event"] == "out"}
    qlog_losses = {(packet_key(data)):(when,data) for when,name,data in events if name == "packet_lost"}
    history = defaultdict(list)
    rows, losses = [], []
    for key,(when,data) in sorted(sent.items(), key=lambda item:item[1][0]):
        frames = stream_frames(data)
        meta = metadata[when]
        if len(meta) != 1:
            raise ValueError(f"packet cannot be joined to its send-time timer: {work.name}/{role}/{key}/{when}")
        meta = meta[0]
        relay_in = ingress.get(meta["sha256"])
        relay_out = egress.get(meta["sha256"])
        arrival = min(received[key]) if received[key] else None
        ack = acknowledgements.get(key)
        ack_send = peer_sent[ack[2]] if ack else None
        loss = declarations.get((when,key[1]))
        if bool(loss) != (key in qlog_losses):
            raise ValueError(f"native declaration and qlog disagree: {work.name}/{role}/{key}")
        measured = any(0 < stream//4 < len(plan["tasks"]) for stream,_,_ in frames)
        row = dict(run=work.name, case=plan["case"], repeat=plan["repeat"],
            direction="forward" if role == "client" else "reverse", packet_type=key[0],
            packet_number=key[1], send_ms=(when-plan["origin"])/1000,
            receive_ms=(arrival-plan["origin"])/1000 if arrival is not None else None,
            one_way_ms=(arrival-when)/1000 if arrival is not None else None,
            pcap_one_way_ms=pcap_delays[meta["sha256"]],
            ack_send_ms=(ack_send-plan["origin"])/1000 if ack_send is not None else None,
            ack_receive_ms=(ack[0]-plan["origin"])/1000 if ack else None,
            confirmation_ms=(ack[0]-when)/1000 if ack else None,
            ack_reverse_ms=(ack[0]-ack_send)/1000 if ack else None,
            ack_delay_reported_ms=ack[1]/1000 if ack else None,
            udp_bytes=int(meta["udp_bytes"]), sha256=meta["sha256"],
            stream_ids="|".join(str(stream) for stream,_,_ in frames),
            stream_bytes=sum(length for _,_,length in frames),
            repeated_stream_bytes=overlap_bytes(frames,history), measured=int(measured),
            relay_action=relay_in["action"] if relay_in else "",
            relay_ingress_ms=(int(relay_in["us"])-plan["origin"])/1000 if relay_in else None,
            relay_egress_ms=(int(relay_out["us"])-plan["origin"])/1000 if relay_out else None,
            actual_relay_drop=int(bool(relay_in and relay_in["event"] == "drop")),
            base_timer_at_send_ms=int(meta["base_timer_us"])/1000,
            effective_timer_at_send_ms=int(meta["effective_timer_us"])/1000,
            srtt_at_send_ms=int(meta["srtt_us"])/1000,
            rttvar_at_send_ms=int(meta["rttvar_us"])/1000,
            max_ack_delay_ms=int(meta["max_ack_delay_us"])/1000,
            declared_lost=int(bool(loss)), loss_reason=loss["reason"] if loss else "",
            loss_ms=(loss["us"]-plan["origin"])/1000 if loss else None,
            age_at_loss_ms=(loss["us"]-when)/1000 if loss else None,
            deadline_age_ms=(loss["deadline_us"]-when)/1000 if loss else None,
            base_timer_at_loss_ms=loss["base_timer_us"]/1000 if loss else None,
            effective_timer_at_loss_ms=loss["effective_timer_us"]/1000 if loss else None,
            ack_gap_at_loss=loss["highest_ack"]-key[1] if loss else None,
            original_received_after_loss=int(bool(loss and arrival is not None and arrival > loss["us"])),
            original_received_before_loss=int(bool(loss and arrival is not None and arrival <= loss["us"])),
            declared_but_original_received=int(bool(loss and arrival is not None)))
        if row["one_way_ms"] is not None and row["one_way_ms"] < 0:
            raise ValueError("negative delay across monotonic-clock qlogs")
        if row["actual_relay_drop"] and arrival is not None:
            raise ValueError("relay drop unexpectedly reached receiver")
        if (row["pcap_one_way_ms"] is None) != (arrival is None):
            raise ValueError(f"qlog and NIC capture disagree on packet arrival: {work.name}/{role}/{key}")
        if ack and not (arrival is not None and arrival <= ack_send <= ack[0]):
            raise ValueError("invalid order for receive, ACK send and ACK arrival")
        rows.append(row)
        if loss:
            if qlog_losses[key][0] != loss["us"]:
                raise ValueError("native and qlog loss timestamps differ")
            detail = dict(row, **{("native_"+key):value for key,value in loss.items()})
            repeats = []
            for new_key,(new_when,new_data) in sent.items():
                if new_when <= when or new_key[0] != key[0]:
                    continue
                matched = any(a == b and max(x,y) < min(x+nx,y+ny)
                    for a,x,nx in frames for b,y,ny in stream_frames(new_data))
                if matched:
                    repeats.append((new_when,new_key,min(received[new_key]) if received[new_key] else None))
            repeats.sort()
            detail.update(repeat_packet_numbers="|".join(str(item[1][1]) for item in repeats),
                first_repeat_send_ms=(repeats[0][0]-plan["origin"])/1000 if repeats else None,
                first_repeat_receive_ms=(repeats[0][2]-plan["origin"])/1000
                    if repeats and repeats[0][2] is not None else None)
            losses.append(detail)
    if len(declarations) != len(losses) or len(qlog_losses) != len(losses):
        raise ValueError(f"not every loss declaration matched a send in {work.name}/{role}")
    return rows, losses


def task_rows(work, plan):
    start = {int(row["task"]):int(row["us"]) for row in read_csv(work / "client.csv") if row["event"] == "start"}
    complete = {int(row["task"]):row for row in read_csv(work / "server.csv") if row["event"] == "upload"}
    result = []
    for index,task in enumerate(plan["tasks"]):
        if task["warmup"]:
            continue
        done = complete.get(index)
        if done and (int(done["bytes"]) != task["size"] or int(done["hash"]) != 0):
            raise ValueError("fixed zero-byte request verification failed")
        delay = (int(done["us"])-start[index])/1000 if done else None
        result.append(dict(run=work.name, case=plan["case"], repeat=plan["repeat"], task=index,
            bytes=task["size"], start_ms=(start[index]-plan["origin"])/1000, upload_ms=delay,
            on_time20=int(delay is not None and delay <= 20)))
    return result


def distribution(rows, field, prefix):
    values = [row[field] for row in rows if row[field] is not None]
    return {prefix+suffix:quantile(values,p) for suffix,p in (("p50",.5),("p95",.95),("p99",.99),("max",1))}


def summary(packets, losses, tasks):
    cases = sorted({row["case"] for row in tasks})
    rows = []
    for case in cases:
        data = [row for row in packets if row["case"] == case and row["direction"] == "forward" and row["measured"]]
        lost = [row for row in losses if row["case"] == case and row["direction"] == "forward" and row["measured"]]
        requests = [row for row in tasks if row["case"] == case]
        first_by_task = {}
        for packet in data:
            for stream in packet["stream_ids"].split("|"):
                if stream != "0":
                    first_by_task.setdefault((packet["run"],stream), packet)
        reverse = [row for row in packets if row["case"] == case and row["direction"] == "reverse"
                   and row["packet_type"] == "1RTT" and row["send_ms"] >= 1200]
        row = dict(case=case, runs=len({item["run"] for item in requests}), tasks=len(requests),
            completed=sum(item["upload_ms"] is not None for item in requests),
            on_time20=sum(item["on_time20"] for item in requests), data_packets=len(data),
            data_received=sum(item["one_way_ms"] is not None for item in data),
            actual_drops=sum(item["actual_relay_drop"] for item in data), declared=len(lost),
            unmatched_without_relay_drop=sum(item["one_way_ms"] is None and not item["actual_relay_drop"] for item in data),
            packet_number_losses=sum(item["loss_reason"] == "packet_number" for item in lost),
            rack_time_losses=sum(item["loss_reason"] == "rack_time" for item in lost),
            timer_losses=sum(item["loss_reason"] == "timer" for item in lost),
            original_received=sum(item["declared_but_original_received"] for item in lost),
            original_received_before_loss=sum(item["original_received_before_loss"] for item in lost),
            original_received_after_loss=sum(item["original_received_after_loss"] for item in lost),
            declarations_with_repeated_data=sum(item["first_repeat_send_ms"] is not None for item in lost),
            repeated_bytes=sum(item["repeated_stream_bytes"] for item in data),
            **distribution(data,"one_way_ms","delay_"),
            **distribution(reverse,"one_way_ms","reverse_delay_"),
            **distribution(data,"confirmation_ms","confirm_"),
            **distribution(data,"effective_timer_at_send_ms","send_timer_"),
            **distribution(first_by_task.values(),"effective_timer_at_send_ms","first_timer_"),
            **distribution(lost,"effective_timer_at_loss_ms","loss_timer_"),
            **distribution(lost,"age_at_loss_ms","loss_age_"),
            **distribution(lost,"deadline_age_ms","deadline_age_"),
            **distribution(requests,"upload_ms","upload_"))
        rows.append(row)
    return rows


def archive(raw, output):
    whitelist = {"environment.json", "profiles.yaml", "plan.json", "tasks.tsv", "client.csv", "server.csv",
        "samples.csv", "wire.csv", "client-sent.csv", "server-sent.csv", "client-loss.csv", "server-loss.csv",
        "client.pcap.gz", "server.pcap.gz", "client.capture.txt", "server.capture.txt",
        "client.log", "server.log", "relay.log", "traffic.sqlite"}
    manifest = {}
    with tarfile.open(output, "w:xz") as bundle:
        for path in sorted(raw.rglob("*")):
            if not path.is_file() or (path.name not in whitelist and path.suffix != ".qlog"):
                continue
            name = path.relative_to(raw).as_posix()
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            bundle.addfile(info, io.BytesIO(data))
            manifest[name] = hashlib.sha256(data).hexdigest()
        data = json.dumps(manifest,indent=2).encode()
        info = tarfile.TarInfo("SHA256-MANIFEST.json")
        info.size, info.mode = len(data), 0o644
        bundle.addfile(info, io.BytesIO(data))
    return len(manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    environment = get(args.raw / "environment.json")
    expected = {f"{case}__{repeat:02d}" for case in environment["cases"] for repeat in range(environment["repeats"])}
    runs = {path.parent.name:path.parent for path in args.raw.glob("*/plan.json")}
    if set(runs) != expected:
        raise ValueError("run denominator differs from experiment plan")
    database_uri = (args.raw / "traffic.sqlite").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as database:
        records = database.execute("SELECT session_id,success FROM traffic_logs").fetchall()
    if Counter(records) != Counter((run,1) for run in expected):
        raise ValueError("Testbed TrafficLogger denominator differs from raw runs")
    packets,losses,tasks = [],[],[]
    for name,work in sorted(runs.items()):
        plan = get(work / "plan.json")
        pcap_delays = wire_delays(work)
        for role in ("client","server"):
            if "0 packets dropped by kernel" not in (work / (role+".capture.txt")).read_text():
                raise ValueError("capture loss")
            rows,events = analyze_direction(work,plan,role,pcap_delays)
            packets.extend(rows)
            losses.extend(events)
        tasks.extend(task_rows(work,plan))
    if len(tasks) != len(expected)*environment["measured_tasks_per_run"]:
        raise ValueError("task denominator differs from plan")
    columns = list(packets[0])
    with gzip.open(args.out / "packets.csv.gz","wt",newline="",encoding="utf-8") as handle:
        writer = csv.DictWriter(handle,fieldnames=columns,lineterminator="\n")
        writer.writeheader()
        writer.writerows(packets)
    csv_write(args.out / "loss-events.csv",losses)
    csv_write(args.out / "tasks.csv",tasks)
    csv_write(args.out / "summary.csv",summary(packets,losses,tasks))
    (args.out / "environment.json").write_text(json.dumps(environment,indent=2)+"\n")
    validation = dict(runs=len(runs),tasks=len(tasks),packets=len(packets),loss_declarations=len(losses),
        all_native_loss_events_match_qlog=True,all_packet_send_times_match_timer_observations=True,
        all_qlog_arrivals_match_capture=True,no_negative_one_way_delays=True,no_capture_kernel_drops=True,
        qlog_all_packets_enabled=True,warmup_excluded_from_measured_statistics=True)
    errors = [abs(row["pcap_one_way_ms"]-row["one_way_ms"])
              for row in packets if row["one_way_ms"] is not None]
    validation.update(pcap_qlog_delay_absdiff_p50_ms=quantile(errors,.5),
                      pcap_qlog_delay_absdiff_p99_ms=quantile(errors,.99),
                      pcap_qlog_delay_absdiff_max_ms=max(errors))
    if args.archive:
        validation["archive_members"] = archive(args.raw,args.out / "raw-evidence.tar.xz")
    (args.out / "validation.json").write_text(json.dumps(validation,indent=2)+"\n")
    print(json.dumps(validation))


if __name__ == "__main__":
    main()
