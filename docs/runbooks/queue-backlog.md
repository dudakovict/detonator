# QueueBacklog

**Meaning:** More than 20 jobs have waited in `analysis.jobs` for 10 minutes.
Submissions arrive faster than the sandbox VMs can analyse them.

**Check:**
- Grafana → *Detonator - platform health* → *Jobs queue*: are consumers present?
- Grafana → *Detonator - analysis* → *Job duration*: did jobs get slower?

**Fix:**
- Consumers missing: see [WorkerDown](worker-down.md).
- Jobs slower than usual: look at the node's CPU and memory panels; a starved
  node slows every VM.
- Sustained higher load: add sandbox VMs (`vm_count` in `infra/terraform`, then
  `make vms`, `make snapshot`, `make worker`) if the node has memory to spare,
  otherwise add a node.
