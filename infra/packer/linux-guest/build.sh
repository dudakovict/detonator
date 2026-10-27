#!/usr/bin/env bash
# Builds the sandbox guest image and installs it into libvirt's base image
# directory. Runs on the sandbox node (it needs /dev/kvm); `make image` calls it.
set -euo pipefail
cd "$(dirname "$0")"

BASE_DIR=/var/lib/libvirt/images/base
# The build VM takes 512 MB on a node that also runs the platform. Without that
# headroom (plus a margin) the node runs out of memory and stops responding.
MIN_AVAILABLE_MB=900

available_mb=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)
if (( available_mb < MIN_AVAILABLE_MB )); then
  echo "Only ${available_mb} MB available, need ${MIN_AVAILABLE_MB} MB to build safely." >&2
  echo "Free memory first, e.g. scale Kibana down: kubectl -n monitoring scale deploy/kibana --replicas=0" >&2
  exit 1
fi
rm -rf output # leftovers of an interrupted build

key_dir="$(mktemp -d)"
trap 'rm -rf "$key_dir"' EXIT
ssh-keygen -q -t ed25519 -N '' -C packer-build -f "$key_dir/id"

packer init .
packer build -var "ssh_key_file=$key_dir/id" .

imgs=(output/*/sbx-linux-*.qcow2) # this build's only output; it is removed below
img="${imgs[0]}"
sudo install -m 0644 "$img" "$BASE_DIR/"
sudo ln -sfn "$(basename "$img")" "$BASE_DIR/sbx-linux-latest.qcow2"
rm -rf "$(dirname "$img")"
echo "==> Installed $BASE_DIR/$(basename "$img")"
