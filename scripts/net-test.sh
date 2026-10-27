#!/usr/bin/env bash
# Proves the sandbox network isolation from inside a sandbox VM: the guest must
# not reach the internet or the node's other services, every name must resolve
# to the sinkhole, and an HTTP request must show up in the sinkhole log.
# Runs on the sandbox node; `make net-test` calls it. Reverts the VM afterwards.
set -euo pipefail
export LIBVIRT_DEFAULT_URI=qemu:///system

VM="${1:-sbx-linux-01}"
SINK_LOG=/var/log/sinkhole/events.jsonl
ip="$(virsh net-dumpxml sbx-isolated | python3 -c "
import sys, xml.etree.ElementTree as ET
print(next(h.get('ip') for h in ET.parse(sys.stdin).iter('host') if h.get('name') == '$VM'))")"
url="http://$ip:8000"
marker="nettest-$(date +%s)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"; virsh snapshot-revert "$VM" clean --running >/dev/null' EXIT
virsh snapshot-revert "$VM" clean --running

cat > "$tmp/probe.py" <<EOF
#!/usr/bin/env python3
import socket, urllib.request

def check(name, fn):
    try:
        print(f"{name} -> {fn()}")
    except Exception as e:
        print(f"{name} -> blocked ({type(e).__name__})")

check("tcp 1.1.1.1:443", lambda: socket.create_connection(("1.1.1.1", 443), timeout=3) and "connected")
check("tcp node 192.168.200.10:22", lambda: socket.create_connection(("192.168.200.10", 22), timeout=3) and "connected")
check("tcp sink 10.66.0.1:22", lambda: socket.create_connection(("10.66.0.1", 22), timeout=3) and "connected")
check("tcp k8s 10.66.0.1:6443", lambda: socket.create_connection(("10.66.0.1", 6443), timeout=3) and "connected")
check("tcp k8s 192.168.200.10:6443", lambda: socket.create_connection(("192.168.200.10", 6443), timeout=3) and "connected")
check("tcp api 10.66.0.1:30080", lambda: socket.create_connection(("10.66.0.1", 30080), timeout=3) and "connected")
check("tcp api 192.168.200.10:30080", lambda: socket.create_connection(("192.168.200.10", 30080), timeout=3) and "connected")
check("dns evil-c2.example", lambda: socket.gethostbyname("evil-c2.example"))
check("http evil-c2.example", lambda: urllib.request.urlopen("http://evil-c2.example/$marker", timeout=5).status)
EOF

curl -fsS --data-binary @"$tmp/probe.py" "$url/upload?name=probe.py" >/dev/null
curl -fsS -d '{"timeout": 30}' "$url/execute" >/dev/null
curl -fsS -o "$tmp/results.tar.gz" "$url/results"
tar -xzf "$tmp/results.tar.gz" -C "$tmp"
cat "$tmp/results/stdout.log"

fail=0
expect() {
  if grep -qF -- "$1" "$tmp/results/stdout.log"; then echo "PASS  $2"; else echo "FAIL  $2"; fail=1; fi
}
expect "tcp 1.1.1.1:443 -> blocked" "no route to the internet"
expect "tcp node 192.168.200.10:22 -> blocked" "node's other addresses unreachable"
expect "tcp sink 10.66.0.1:22 -> blocked" "only DNS/HTTP allowed on the sinkhole IP"
expect "tcp k8s 10.66.0.1:6443 -> blocked" "Kubernetes API unreachable via 10.66.0.1"
expect "tcp k8s 192.168.200.10:6443 -> blocked" "Kubernetes API unreachable via 192.168.200.10"
expect "tcp api 10.66.0.1:30080 -> blocked" "API NodePort unreachable via 10.66.0.1"
expect "tcp api 192.168.200.10:30080 -> blocked" "API NodePort unreachable via 192.168.200.10"
expect "dns evil-c2.example -> 10.66.0.1" "every name resolves to the sinkhole"
expect "http evil-c2.example -> 200" "HTTP answered by the sinkhole"
if grep -qF "\"path\": \"/$marker\"" "$SINK_LOG"; then
  echo "PASS  request recorded in the sinkhole log"
else
  echo "FAIL  request recorded in the sinkhole log"; fail=1
fi
exit "$fail"
