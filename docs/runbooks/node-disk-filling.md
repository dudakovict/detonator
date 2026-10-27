# NodeDiskFilling

**Meaning:** Less than 15% of the node's root disk is free. Images, VM overlays,
Prometheus data and object storage all live there.

**Check** (on the node):
```bash
sudo du -sh /var/lib/libvirt/images/* /var/lib/rancher/k3s/storage/* 2>/dev/null | sort -h
```

**Fix:**
- Old golden images: delete unused `sbx-linux-*.qcow2` in
  `/var/lib/libvirt/images/base/` (never the one `sbx-linux-latest` points to,
  or one a VM still uses as its backing file).
- Packer cache: `rm -rf ~/.cache/packer`.
- Object storage growth is expected; plan a retention policy for artifacts.
