#!/usr/bin/env bash
# Boots the latest guest image once (on a throwaway overlay) and checks the agent
# end to end: health, upload + execute a harmless script, fetch the results.
# Runs on the sandbox node; `make image-test` calls it.
set -euo pipefail

BASE=/var/lib/libvirt/images/base/sbx-linux-latest.qcow2
PORT=18000
URL="http://127.0.0.1:$PORT"

tmp="$(mktemp -d)"
cleanup() {
  [[ -f "$tmp/pid" ]] && kill "$(cat "$tmp/pid")" 2>/dev/null
  rm -rf "$tmp"
}
trap cleanup EXIT

qemu-img create -q -f qcow2 -F qcow2 -b "$(readlink -f "$BASE")" "$tmp/disk.qcow2"
# restrict=on: the guest can reach nothing (no host, no internet); only the
# forwarded agent port is reachable, from the node's localhost.
/usr/libexec/qemu-kvm -machine q35,accel=kvm -cpu host -m 512 -smp 1 -display none \
  -drive file="$tmp/disk.qcow2",if=virtio \
  -netdev "user,id=n0,restrict=on,hostfwd=tcp:127.0.0.1:$PORT-:8000" \
  -device virtio-net-pci,netdev=n0 \
  -daemonize -pidfile "$tmp/pid"

echo "==> Waiting for the agent"
for _ in $(seq 60); do
  curl -fsS "$URL/health" 2>/dev/null && break
  sleep 2
done
echo

cat > "$tmp/install.sh" <<'EOF'
#!/bin/sh
echo hello > /tmp/hello.txt
(crontab -l 2>/dev/null; echo "@reboot /tmp/hello.txt") | crontab -
EOF

echo "==> Upload"
curl -fsS --data-binary @"$tmp/install.sh" "$URL/upload?name=install.sh"; echo
echo "==> Execute"
curl -fsS -d '{"timeout": 20}' "$URL/execute"; echo
echo "==> Results"
curl -fsS -o "$tmp/results.tar.gz" "$URL/results"
tar -xzf "$tmp/results.tar.gz" -C "$tmp"
ls "$tmp/results" "$tmp/results/trace"
python3 -m json.tool "$tmp/results/fsdiff.json"
