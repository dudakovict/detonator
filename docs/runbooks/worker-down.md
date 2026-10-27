# WorkerDown

**Meaning:** Prometheus can't scrape a detonation worker for 2 minutes. Its sandbox
VM takes no jobs, so throughput drops by one VM.

**Check** (on the node, `make ssh`):
```bash
systemctl status detonator-worker@sbx-linux-01
journalctl -u detonator-worker@sbx-linux-01 -n 50 -o cat
virsh -c qemu:///system list --all          # is the VM running?
```

**Fix:**
- Crashed or restarting: the journal shows the exception. Common causes are the
  control plane being unreachable (`kubectl -n detonator get pods`) or the VM
  missing its `clean` snapshot (`make snapshot`).
- Stopped: `sudo systemctl start detonator-worker@<vm>`.
- After a change to the worker or its credentials: `make worker`.
