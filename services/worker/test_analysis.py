"""Unit tests for the parsers and rules; no VM needed. Run with: pytest services/worker"""
import json
import struct

import analysis
import rules

TRACE_PARENT = """\
12:00:00.000001 execve("/home/alex/dropper.sh", ["/home/alex/dropper.sh"], 0x7ffd /* 5 vars */) = 0
12:00:00.000100 openat(AT_FDCWD, "/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = 3
12:00:00.000200 openat(AT_FDCWD, "/tmp/.update", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3
12:00:00.000300 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|SIGCHLD) = 101
12:00:00.000400 openat(AT_FDCWD, "/etc/passwd", O_RDONLY|O_CLOEXEC) = 4
12:00:00.000500 connect(5, {sa_family=AF_INET, sin_port=htons(4444), sin_addr=inet_addr("203.0.113.50")}, 16) = -1 EINPROGRESS (Operation now in progress)
"""
TRACE_CHILD = """\
12:00:00.000600 execve("/usr/bin/chmod", ["chmod", "+x", "/tmp/.update"], 0x5 /* 5 vars */) = 0
12:00:00.000700 fchmodat(AT_FDCWD, "/tmp/.update", 0755) = 0
"""


def test_strace(tmp_path):
    (tmp_path / "strace.100").write_text(TRACE_PARENT)
    (tmp_path / "strace.101").write_text(TRACE_CHILD)
    processes, events = analysis.parse_strace(tmp_path)
    assert [(p["pid"], p["ppid"], p["exe"]) for p in processes] == [
        (100, None, "/home/alex/dropper.sh"), (101, 100, "/usr/bin/chmod")]
    kinds = {(e["event"], e.get("path") or e.get("dst")) for e in events}
    assert ("file_write", "/tmp/.update") in kinds
    assert ("file_write", "/etc/ld.so.cache") not in kinds  # read-only opens are ignored
    assert ("file_read", "/etc/passwd") in kinds
    assert ("chmod_exec", "/tmp/.update") in kinds
    assert ("connect", "203.0.113.50") in kinds


def _frame(src, dst, proto, dport, tcp_flags=0x02):
    ip_header = bytes([0x45, 0, 0, 40, 0, 0, 0, 0, 64, proto, 0, 0]) + bytes(map(int, src.split("."))) \
        + bytes(map(int, dst.split(".")))
    l4 = struct.pack("!HH", 40000, dport) + b"\0" * 9 + bytes([tcp_flags]) + b"\0" * 6
    return b"\0" * 12 + b"\x08\x00" + ip_header + l4


def test_pcap(tmp_path):
    frames = [_frame("10.66.0.11", "203.0.113.50", 6, 4444),
              _frame("10.66.0.11", "203.0.113.50", 6, 4444),           # retransmitted SYN
              _frame("10.66.0.11", "10.66.0.1", 6, 80, tcp_flags=0x10),  # ACK, not an opening
              _frame("10.66.0.1", "10.66.0.11", 6, 8000)]              # node → VM, ignored
    data = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for f in frames:
        data += struct.pack("<IIII", 0, 0, len(f), len(f)) + f
    (tmp_path / "c.pcap").write_bytes(data)
    attempts = analysis.parse_pcap(tmp_path / "c.pcap", "10.66.0.11")
    assert attempts == [{"event": "connect", "proto": "tcp", "dst": "203.0.113.50", "port": "4444",
                         "count": "2", "source": "pcap"}]


def test_sinkhole_window(tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in [
        {"ts": 5, "type": "dns", "src": "10.66.0.11", "name": "Evil.Example"},
        {"ts": 6, "type": "http", "src": "10.66.0.11", "method": "POST", "host": "evil.example",
         "path": "/c2"},
        {"ts": 6, "type": "dns", "src": "10.66.0.12", "name": "other-vm.example"},
        {"ts": 99, "type": "dns", "src": "10.66.0.11", "name": "too-late.example"},
    ]) + "\nnot json\n")
    events = analysis.parse_sinkhole(log, "10.66.0.11", 1, 10)
    assert [e.get("name") or e.get("url") for e in events] == ["evil.example", "http://evil.example/c2"]


def test_rules_and_verdict():
    events = analysis.mark_dropped_executions([
        {"event": "file_write", "path": "/var/spool/cron/crontabs/alex"},
        {"event": "file_write", "path": "/home/alex/.bashrc"},
        {"event": "file_write", "path": "/tmp/.update"},
        {"event": "process", "exe": "/tmp/.update", "cmdline": "/tmp/.update"},
        {"event": "connect", "dst": "10.66.0.1", "port": "80"},
    ])
    signatures, score, verdict = rules.evaluate(rules.load_rules(), events)
    assert {s["id"] for s in signatures} == {
        "persistence_crontab", "persistence_shell_profile", "executes_dropped_file"}
    assert (score, verdict) == (100, "malicious")
    assert rules.evaluate(rules.load_rules(), [])[1:] == (0, "clean")
