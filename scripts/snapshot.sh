#!/usr/bin/env bash
# Takes the "clean" snapshot of every sandbox VM: waits until its agent answers,
# then saves disk and memory, so each job can revert to a booted, idle VM.
# Runs on the sandbox node; `make snapshot` calls it.
set -euo pipefail
export LIBVIRT_DEFAULT_URI=qemu:///system

# VM name and IP from the network's static DHCP entries (set by Terraform).
hosts="$(virsh net-dumpxml sbx-isolated | python3 -c '
import sys, xml.etree.ElementTree as ET
for h in ET.parse(sys.stdin).iter("host"):
    print(h.get("name"), h.get("ip"))')"

while read -r vm ip; do
  if virsh snapshot-info "$vm" clean &>/dev/null; then
    virsh snapshot-delete "$vm" clean >/dev/null
  fi
  echo "==> $vm: waiting for the agent at $ip"
  for _ in $(seq 60); do
    curl -fsS -m 2 "http://$ip:8000/health" >/dev/null 2>&1 && break
    sleep 2
  done
  curl -fsS -m 2 "http://$ip:8000/health" >/dev/null
  virsh snapshot-create-as "$vm" clean --description "booted, agent idle" --atomic
done <<< "$hosts"
