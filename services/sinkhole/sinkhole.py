#!/usr/bin/env python3
"""Sinkhole for the isolated sandbox network: a fake internet for samples.

DNS: every A query is answered with SINK_IP, so any domain a sample looks up
resolves to this host. HTTP: any request on port 80 gets an empty 200. Every
query and request is logged as one JSON line; the worker correlates them with
jobs by source IP and time. Standard library only.
"""
import json
import os
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SINK_IP = os.environ.get("SINK_IP", "10.66.0.1")
LOG_FILE = os.environ.get("SINK_LOG", "/var/log/sinkhole/events.jsonl")
# Lets the sockets bind SINK_IP before libvirt has created the bridge that owns it.
IP_FREEBIND = 15
MAX_BODY_BYTES = 1024 * 1024
MAX_LOGGED_BODY = 4096

log_lock = threading.Lock()


def log(event):
    event = {"ts": round(time.time(), 3), **event}
    line = json.dumps(event)
    with log_lock, open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def parse_question(packet):
    """Return (name, qtype, end offset) of the single question in a DNS query."""
    labels, off = [], 12
    while (length := packet[off]) != 0:
        if length & 0xC0:
            raise ValueError("compressed name in question")
        labels.append(packet[off + 1:off + 1 + length].decode("ascii", "replace"))
        off += 1 + length
    qtype, _qclass = struct.unpack("!HH", packet[off + 1:off + 5])
    return ".".join(labels), qtype, off + 5


def dns_response(query):
    tid, flags, qdcount = struct.unpack("!HHH", query[:6])
    if qdcount != 1 or flags & 0x8000:
        raise ValueError("not a single-question query")
    name, qtype, end = parse_question(query)
    answers = b""
    if qtype == 1:  # A record: point the name at the sinkhole
        answers = (b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4)
                   + socket.inet_aton(SINK_IP))
    # Response, authoritative, recursion desired copied, recursion available, NOERROR.
    reply_flags = 0x8000 | 0x0400 | (flags & 0x0100) | 0x0080
    header = struct.pack("!HHHHHH", tid, reply_flags, 1, 1 if answers else 0, 0, 0)
    return name, qtype, header + query[12:end] + answers


def serve_dns():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_IP, IP_FREEBIND, 1)
    sock.bind((SINK_IP, 53))
    while True:
        query, addr = sock.recvfrom(512)
        try:
            name, qtype, reply = dns_response(query)
        except (ValueError, IndexError, struct.error):
            continue
        sock.sendto(reply, addr)
        log({"type": "dns", "src": addr[0], "name": name, "qtype": qtype})


class Sink(BaseHTTPRequestHandler):
    # Look like an ordinary web server rather than Python's http.server.
    server_version = "nginx"
    sys_version = ""

    def _sink(self):
        try:
            length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        log({"type": "http", "src": self.client_address[0], "method": self.command,
             "host": self.headers.get("Host", ""), "path": self.path,
             "user_agent": self.headers.get("User-Agent", ""), "body_size": len(body),
             "body": body[:MAX_LOGGED_BODY].decode("utf-8", "replace")})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_PATCH = _sink

    def log_message(self, fmt, *args):
        pass  # every request is already logged as JSON


class FreebindHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_IP, IP_FREEBIND, 1)
        super().server_bind()


if __name__ == "__main__":
    threading.Thread(target=serve_dns, daemon=True).start()
    FreebindHTTPServer((SINK_IP, 80), Sink).serve_forever()
