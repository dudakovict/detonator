# JobFailureRate

**Meaning:** More than 10% of jobs failed over the last 15 minutes. Failed jobs
are retried, so this often precedes [DeadLetteredJobs](dead-lettered-jobs.md).

**Check:**
```bash
journalctl -u 'detonator-worker@*' -o cat --since -30min | grep job_failed
```
Group the errors: one VM only, or all of them? One sample type, or all?

**Fix:**
- One VM: its snapshot may be broken; `make snapshot`.
- All VMs after an image change: roll back `base_image` in Terraform.
- Storage or database errors: check the control plane pods.
