# detonator

A small, self-hosted **dynamic malware-analysis sandbox**.

A file is submitted to an API, detonated inside a throwaway KVM virtual machine
on an isolated network, its behaviour (processes, file writes, network activity)
is recorded, indicators of compromise (IOCs) are extracted, and a report is
produced. The platform around it is provisioned as code, deployed through CI/CD
and monitored.

> **Safety:** this project is exercised with **benign test samples** only (EICAR
> and scripts that mimic malware behaviour). It is designed as if input were
> hostile (no-egress network, snapshot revert), but running real malware calls
> for a dedicated, air-gapped host.

## Architecture

```
 workstation ── git, Terraform, Ansible, Packer, kubectl, Helm
      │ SSH / kubectl / Ansible
 sandbox node / Rocky Linux 9 + KVM
  ├── k3s ── API · Postgres · RabbitMQ · SeaweedFS (S3) · Prometheus · Grafana · ELK
  └── worker (systemd) ── libvirt / KVM
       └── isolated network (no egress, sinkhole DNS/HTTP)
            ├── sandbox VM 1   ← reverted to a clean snapshot after every job
            └── sandbox VM 2
```

### Components

| Component | Runs on | Responsibility |
|---|---|---|
| API (FastAPI) | k3s | Accept samples, dedupe by SHA-256, store in object storage, enqueue, serve status/reports |
| Postgres | k3s | Job metadata, verdicts, IOCs |
| RabbitMQ | k3s | Job queue (manual acks, prefetch = free VMs, dead-letter queue) |
| SeaweedFS | k3s | S3-compatible object storage for samples, artifacts and reports |
| Worker | sandbox node (systemd) | Pull jobs, drive VMs via libvirt, capture, parse, score, report |
| Guest agent | inside each sandbox VM | Receive sample, execute under tracing, return results |
| Sinkhole | sandbox node | Answer all DNS with the host IP; log HTTP requests ("fake internet") |
| Observability | k3s | Metrics and alerts (Prometheus, Grafana); logs (Elasticsearch, Kibana, Filebeat) |

### Analysis pipeline (per job)

1. A worker receives a job from RabbitMQ. There is one worker per sandbox VM, each taking one job at a time.
2. Revert its VM: `virsh snapshot-revert <vm> clean` (live snapshot, no cold boot).
3. Start `tcpdump` on the VM's network interface.
4. Agent `POST /upload`, then `POST /execute` (runs under `strace -ff`, hard timeout).
5. Fetch results (syscall traces, file-system diff, dropped files).
6. Stop capture; parse the pcap (connection attempts) and the sinkhole log (DNS, HTTP).
7. Normalise into a report; extract IOCs (domains, IPs, URLs, dropped-file hashes).
8. Apply YAML behaviour rules → score → verdict (`clean` / `suspicious` / `malicious`).
9. Store artifacts and report in object storage, verdict and IOCs in Postgres.
10. Ack the message, revert the VM.

## Tooling

| Area | Tools |
|---|---|
| Virtualization | KVM, QEMU, libvirt on Rocky Linux 9 |
| Images & infrastructure | Packer, Terraform (libvirt provider), Ansible (+ Molecule) |
| Platform | k3s, Helm, Docker, Python (FastAPI), Postgres, RabbitMQ, SeaweedFS (S3) |
| CI/CD | GitHub Actions (hosted + one self-hosted runner), GHCR, Trivy, gitleaks |
| Observability | Prometheus, Alertmanager, Grafana, ELK (Elasticsearch, Kibana, Filebeat), Redash |

## Status

| Milestone | Scope | Status |
|---|---|---|
| 1 | Repo skeleton, README, CI (secret scan) | ✅ |
| 2 | Golden guest image (Packer + Ansible) + guest agent | ✅ |
| 3 | Isolated network + VM pool (Terraform + Ansible) | ✅ |
| 4 | Control plane on k3s (Helm): API, Postgres, RabbitMQ, object storage | ✅ |
| 5 | Worker / orchestrator: detonation, parsing, IOCs, verdicts | ✅ |
| 6 | Observability: Prometheus/Grafana metrics, ELK logs, alerts, runbooks | ⏳ |
| 7 | CI/CD: build, scan, deploy, end-to-end tests | |

## Getting started

Prerequisites:
- A sandbox node: Rocky Linux 9 (or another RHEL 9 rebuild) with hardware
  virtualization (`/dev/kvm`), at least 6 GB of RAM, and SSH key access for a
  sudo-capable user.
- A Linux workstation with the tools listed above.

```bash
make help        # list available targets
make ssh         # log in to the sandbox node (SBX_HOST, SSH_KEY to override)
make node        # configure the node: KVM, libvirt, Packer, firewall, sinkhole (Ansible)
make image       # build the guest image on the node (Packer + Ansible)
make image-test  # boot the image once and run a harmless sample through the agent
make vms         # create the isolated network and sandbox VMs (Terraform)
make snapshot    # take each VM's "clean" snapshot
make net-test    # prove from inside a VM that the network is isolated
make deploy      # build the API image and install the control plane on k3s (Helm)
make api-test    # submit a harmless sample and check it was stored and queued
make worker      # install the detonation workers on the node (Ansible)
make e2e         # analyse the benign samples end to end and check their verdicts
```
