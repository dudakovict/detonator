#!/usr/bin/env bash
# Builds the sandbox guest image and installs it into libvirt's base image
# directory. Runs on the sandbox node (it needs /dev/kvm); `make image` calls it.
set -euo pipefail
cd "$(dirname "$0")"

BASE_DIR=/var/lib/libvirt/images/base

key_dir="$(mktemp -d)"
trap 'rm -rf "$key_dir"' EXIT
ssh-keygen -q -t ed25519 -N '' -C packer-build -f "$key_dir/id"

packer init .
packer build -var "ssh_key_file=$key_dir/id" .

img="$(ls -t output/*/sbx-linux-*.qcow2 | head -1)"
sudo install -m 0644 "$img" "$BASE_DIR/"
sudo ln -sfn "$(basename "$img")" "$BASE_DIR/sbx-linux-latest.qcow2"
rm -rf "$(dirname "$img")"
echo "==> Installed $BASE_DIR/$(basename "$img")"
