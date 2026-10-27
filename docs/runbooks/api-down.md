# ApiDown

**Meaning:** The API pod hasn't been scraped successfully for 2 minutes. New
samples can't be submitted; running jobs continue.

**Check:**
```bash
kubectl -n detonator get pods -l app.kubernetes.io/component=api
kubectl -n detonator describe pod -l app.kubernetes.io/component=api | tail -20
kubectl -n detonator logs deploy/api --tail 50
```

**Fix:**
- `CrashLoopBackOff` with "dependencies not reachable": check Postgres, RabbitMQ
  and the object store pods in the same namespace.
- `ImagePullBackOff`/`ErrImageNeverPull`: the image isn't on the node; `make deploy`.
- `OOMKilled`: raise `api.resources.limits.memory` in the chart values.
