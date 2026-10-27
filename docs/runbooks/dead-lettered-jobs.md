# DeadLetteredJobs

**Meaning:** A job failed three times and RabbitMQ moved it to `analysis.dead`.
Its sample stays in status `error` and gets no report.

**Check:**
```bash
kubectl -n detonator exec postgres-0 -- psql -U detonator -c \
  "select sha256, filename, error from samples where status = 'error'"
journalctl -u 'detonator-worker@*' -o cat | grep job_failed | tail
```

**Fix:**
- Fix the cause shown in `error` (worker bug, broken VM, malformed sample).
- Re-analyse: resubmit the file with `POST /samples?force=true`.
- Then purge the dead-letter queue:
  `kubectl -n detonator exec rabbitmq-0 -- rabbitmqctl purge_queue analysis.dead`
