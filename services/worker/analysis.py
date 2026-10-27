"""Turns raw detonation output into normalised behaviour events.

Inputs are hostile (written by, or describing, the sample), so every parser
tolerates malformed data and caps how much it reads.

An event is a flat dict with an "event" type and string fields, for example
{"event": "file_write", "path": "/tmp/x", "source": "strace"}. Rules match on them.
"""
import ipaddress
import json
import re
import struct
from pathlib import Path

MAX_TRACE_BYTES = 20 * 1024 * 1024
MAX_PCAP_BYTES = 50 * 1024 * 1024

# strace -tt line: "12:00:00.123456 syscall(args) = result"
LINE = re.compile(r"^\S+ (\w+)\((.*)\) += (-?\w+)")
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
WRITE_FLAGS = re.compile(r"O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND")
CONNECT = re.compile(r'sin_port=htons\((\d+)\), sin_addr=inet_addr\("([\d.]+)"\)')
SENSITIVE_READS = re.compile(r"^/etc/(passwd|shadow|group|sudoers)$")


def _unquote(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _strings(args):
    return [_unquote(m) for m in QUOTED.findall(args)]


def parse_strace(trace_dir):
    """Parse strace -ff output (one file per process) into processes and events."""
    processes, events = {}, []
    budget = MAX_TRACE_BYTES
    for path in sorted(Path(trace_dir).glob("strace.*")):
        pid = int(path.suffix[1:])
        processes.setdefault(pid, {"pid": pid, "ppid": None, "exe": None, "argv": []})
        with open(path, errors="replace") as f:
            text = f.read(max(budget, 0))
        budget -= len(text)
        for line in text.splitlines():
            m = LINE.match(line)
            if not m:
                continue
            call, args, result = m.groups()
            ok = not result.startswith("-")
            strings = _strings(args)

            if call in ("clone", "clone3", "fork", "vfork") and result.isdigit():
                child = int(result)
                processes.setdefault(child, {"pid": child, "ppid": None, "exe": None, "argv": []})
                processes[child]["ppid"] = pid
            elif call == "execve" and ok and strings:
                processes[pid]["exe"] = strings[0]
                processes[pid]["argv"] = strings[1:]
            elif call in ("openat", "open", "creat") and ok and strings:
                if call == "creat" or WRITE_FLAGS.search(args):
                    events.append({"event": "file_write", "path": strings[0], "pid": str(pid),
                                   "source": "strace"})
                elif SENSITIVE_READS.match(strings[0]):
                    events.append({"event": "file_read", "path": strings[0], "pid": str(pid)})
            elif call in ("unlink", "unlinkat", "rmdir") and ok and strings:
                events.append({"event": "file_delete", "path": strings[0], "pid": str(pid)})
            elif call in ("rename", "renameat", "renameat2") and ok and len(strings) >= 2:
                events.append({"event": "file_write", "path": strings[1], "pid": str(pid),
                               "source": "strace"})
            elif call in ("chmod", "fchmodat", "fchmodat2") and ok and strings:
                mode = re.search(r", (0[0-7]+)", args)
                if mode and int(mode.group(1), 8) & 0o111:
                    events.append({"event": "chmod_exec", "path": strings[0], "pid": str(pid)})
            elif call == "connect":
                c = CONNECT.search(args)
                if c:
                    events.append({"event": "connect", "dst": c.group(2), "port": c.group(1),
                                   "proto": "tcp", "pid": str(pid), "source": "strace"})

    for proc in processes.values():
        if proc["exe"]:
            events.append({"event": "process", "exe": proc["exe"],
                           "cmdline": " ".join([proc["exe"], *proc["argv"][1:]]),
                           "pid": str(proc["pid"])})
    return sorted(processes.values(), key=lambda p: p["pid"]), events


def parse_pcap(path, vm_ip):
    """Connection attempts the VM made, from a classic libpcap Ethernet capture."""
    attempts = {}
    data = Path(path).read_bytes()[:MAX_PCAP_BYTES] if Path(path).exists() else b""
    if len(data) < 24:
        return []
    endian = "<" if data[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
    off = 24
    while off + 16 <= len(data):
        incl_len = struct.unpack(endian + "I", data[off + 8:off + 12])[0]
        frame = data[off + 16:off + 16 + incl_len]
        off += 16 + incl_len
        if len(frame) < 34 or frame[12:14] != b"\x08\x00":  # IPv4 only
            continue
        ip = frame[14:]
        ihl = (ip[0] & 0x0F) * 4
        proto = ip[9]
        src, dst = str(ipaddress.IPv4Address(ip[12:16])), str(ipaddress.IPv4Address(ip[16:20]))
        if src != vm_ip or len(ip) < ihl + 4:
            continue
        dport = struct.unpack("!H", ip[ihl + 2:ihl + 4])[0]
        if proto == 6:  # TCP: count only connection openings (SYN without ACK)
            if len(ip) < ihl + 14 or (ip[ihl + 13] & 0x12) != 0x02:
                continue
            key = ("tcp", dst, dport)
        elif proto == 17 and dport not in (67, 68):  # UDP, minus DHCP
            key = ("udp", dst, dport)
        else:
            continue
        attempts[key] = attempts.get(key, 0) + 1
    return [{"event": "connect", "proto": p, "dst": d, "port": str(port), "count": str(n),
             "source": "pcap"} for (p, d, port), n in sorted(attempts.items())]


def parse_sinkhole(log_path, vm_ip, start, end):
    """DNS and HTTP events from the sinkhole log for this VM and time window."""
    events = []
    try:
        lines = Path(log_path).read_text(errors="replace").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("src") != vm_ip or not start <= e.get("ts", 0) <= end:
            continue
        if e.get("type") == "dns":
            events.append({"event": "dns", "name": str(e.get("name", "")).lower()})
        elif e.get("type") == "http":
            host = str(e.get("host", "")).split(":")[0].lower()
            events.append({"event": "http", "method": str(e.get("method", "")), "host": host,
                           "url": f"http://{host}{e.get('path', '')}",
                           "user_agent": str(e.get("user_agent", "")),
                           "body": str(e.get("body", ""))[:512]})
    return events


def parse_fsdiff(path):
    """File changes the agent detected by comparing before/after snapshots."""
    try:
        diff = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return [], []
    dropped = [f for f in diff.get("files", []) if f.get("sha256")]
    events = [{"event": "file_write", "path": f["path"], "change": f.get("change", ""),
               "source": "fsdiff"} for f in diff.get("files", [])]
    events += [{"event": "file_delete", "path": p, "source": "fsdiff"}
               for p in diff.get("deleted", [])]
    return dropped, events


def mark_dropped_executions(events):
    """Flag processes started from a file the sample itself wrote."""
    written = {e["path"] for e in events if e["event"] == "file_write"}
    for e in events:
        if e["event"] == "process":
            e["dropped"] = "true" if e["exe"] in written else "false"
    return events


def extract_iocs(events, dropped, sink_ip):
    def unique(values):
        return sorted({v for v in values if v})
    return {
        "domains": unique(e["name"] for e in events if e["event"] == "dns"),
        "urls": unique(e["url"] for e in events if e["event"] == "http"),
        "ips": unique(e["dst"] for e in events if e["event"] == "connect" and e["dst"] != sink_ip),
        "sha256": unique(f["sha256"] for f in dropped),
    }
