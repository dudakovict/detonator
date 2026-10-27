#!/usr/bin/env python3
"""Detonation worker: runs analysis jobs from RabbitMQ in one sandbox VM.

One instance per sandbox VM (systemd: detonator-worker@<vm>), each taking one
job at a time, so concurrency equals the number of VMs. A job is acked only
after its report is stored; if the worker dies mid-job, RabbitMQ redelivers it.

Usage: worker.py <vm name>
"""
import functools
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import analysis
import boto3
import pika
import prometheus_client as prom
import psycopg
import rules

DATABASE_URL = os.environ["DATABASE_URL"]
AMQP_URL = os.environ["AMQP_URL"]
S3_ENDPOINT = os.environ["S3_ENDPOINT"]
BUCKET = os.environ.get("S3_BUCKET", "detonator")
SAMPLE_TIMEOUT = int(os.environ.get("SAMPLE_TIMEOUT", "30"))
SINK_IP = os.environ.get("SINK_IP", "10.66.0.1")
SINK_LOG = os.environ.get("SINK_LOG", "/var/log/sinkhole/events.jsonl")
NETWORK = os.environ.get("SANDBOX_NETWORK", "sbx-isolated")
WORK_DIR = Path(os.environ.get("WORK_DIR", "/var/lib/detonator-worker"))
JOBS_QUEUE = "analysis.jobs"
AGENT_PORT = 8000
# Metrics on the node's own address only (never the sandbox network); each
# instance gets base + its VM number, e.g. sbx-linux-01 → 9411.
METRICS_ADDR = os.environ.get("METRICS_ADDR", "127.0.0.1")
METRICS_BASE_PORT = int(os.environ.get("METRICS_BASE_PORT", "9410"))

JOBS = prom.Counter("detonator_jobs_total", "Finished jobs", ["vm", "status", "verdict"])
JOB_DURATION = prom.Histogram("detonator_job_duration_seconds", "Wall time per job", ["vm"],
                              buckets=(2, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300))
VM_BUSY = prom.Gauge("detonator_vm_busy", "1 while the VM is running a job", ["vm"])
REVERT_FAILURES = prom.Counter("detonator_vm_revert_failures_total", "Failed snapshot reverts", ["vm"])

s3 = boto3.client("s3", endpoint_url=S3_ENDPOINT, region_name="us-east-1",
                  aws_access_key_id=os.environ["S3_ACCESS_KEY"],
                  aws_secret_access_key=os.environ["S3_SECRET_KEY"])
RULES = rules.load_rules()


def log(event, **fields):
    """One JSON object per line on stdout; journald keeps it, Filebeat ships it."""
    print(json.dumps({"ts": datetime.now(UTC).isoformat(), "event": event, **fields}),
          flush=True)


# ---------------------------------------------------------------- sandbox VM

def virsh(*args):
    return subprocess.run(["virsh", "-c", "qemu:///system", *args], check=True,
                          capture_output=True, text=True).stdout


class SandboxVM:
    def __init__(self, name):
        self.name = name
        hosts = ET.fromstring(virsh("net-dumpxml", NETWORK)).iter("host")
        self.ip = next(h.get("ip") for h in hosts if h.get("name") == name)
        self.agent = f"http://{self.ip}:{AGENT_PORT}"

    def revert(self):
        virsh("snapshot-revert", self.name, "clean", "--running")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{self.agent}/health", timeout=2) as r:
                    if json.load(r).get("status") == "ok":
                        return
            except OSError:
                time.sleep(0.5)
        raise RuntimeError(f"agent on {self.name} not healthy after revert")

    def tap_device(self):
        # A revert re-creates the tap device, so look it up after every revert.
        for line in virsh("domiflist", self.name).splitlines()[2:]:
            if line.split() and line.split()[0].startswith("vnet"):
                return line.split()[0]
        raise RuntimeError(f"no network interface for {self.name}")

    def call(self, method, path, data=None, timeout=30):
        req = urllib.request.Request(f"{self.agent}{path}", data=data, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()


# ---------------------------------------------------------------- one job

def set_status(sha256, status, **fields):
    columns = {"status": status, **fields}
    assignments = ", ".join(f"{k} = %s" for k in columns)
    with psycopg.connect(DATABASE_URL) as conn:
        return conn.execute(f"UPDATE samples SET {assignments} WHERE sha256 = %s",
                            (*columns.values(), sha256)).rowcount


def store_iocs(sha256, iocs):
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("DELETE FROM iocs WHERE sha256 = %s", (sha256,))
        rows = [(sha256, kind, value) for kind, values in iocs.items() for value in values]
        if rows:
            conn.cursor().executemany(
                "INSERT INTO iocs (sha256, type, value) VALUES (%s, %s, %s)", rows)


def detonate(vm, job, work):
    sha256, filename = job["sha256"], job.get("filename") or "sample"
    sample = s3.get_object(Bucket=BUCKET, Key=f"samples/{sha256}")["Body"].read()

    vm.revert()
    pcap = work / "capture.pcap"
    # tcpdump writes to a pipe rather than a file: SELinux confines tcpdump to a
    # domain that may not write into the worker's directory.
    with open(pcap, "wb") as pcap_file:
        capture = subprocess.Popen(["tcpdump", "-i", vm.tap_device(), "-U", "-s", "256", "-w", "-"],
                                   stdout=pcap_file, stderr=subprocess.DEVNULL)
        started = time.time()
        try:
            vm.call("POST", f"/upload?name={urllib.request.quote(filename)}", sample)
            execution = json.loads(vm.call("POST", "/execute",
                                           json.dumps({"timeout": SAMPLE_TIMEOUT}).encode(),
                                           timeout=SAMPLE_TIMEOUT + 60))
            (work / "results.tar.gz").write_bytes(vm.call("GET", "/results", timeout=60))
        finally:
            time.sleep(1)  # let the last packets and sinkhole lines land
            capture.send_signal(signal.SIGINT)
            capture.wait(timeout=10)
    finished = time.time()

    results = work / "results"
    with tarfile.open(work / "results.tar.gz") as tar:
        tar.extractall(work, filter="data")  # "data" filter: no absolute paths, links or devices

    processes, events = analysis.parse_strace(results / "trace")
    dropped, fs_events = analysis.parse_fsdiff(results / "fsdiff.json")
    events += fs_events
    events += analysis.parse_pcap(pcap, vm.ip)
    events += analysis.parse_sinkhole(SINK_LOG, vm.ip, started, finished)
    events = analysis.mark_dropped_executions(events)
    signatures, score, verdict = rules.evaluate(RULES, events)
    iocs = analysis.extract_iocs(events, dropped, SINK_IP)

    def of(kind, *keys):
        return [{k: e[k] for k in keys if k in e} for e in events if e["event"] == kind]

    report = {
        "sample": {"sha256": sha256, "filename": filename, "size": len(sample)},
        "analysis": {"vm": vm.name, "started": started, "finished": finished,
                     "duration_s": round(finished - started, 2), "execution": execution},
        "verdict": verdict,
        "score": score,
        "signatures": signatures,
        "processes": processes,
        "files": {
            "written": sorted({e["path"] for e in events if e["event"] == "file_write"}),
            "deleted": sorted({e["path"] for e in events if e["event"] == "file_delete"}),
            "dropped": dropped,
        },
        "network": {"dns": of("dns", "name"), "http": of("http", "method", "url", "user_agent", "body"),
                    "connections": of("connect", "proto", "dst", "port", "source")},
        "iocs": iocs,
    }

    s3.put_object(Bucket=BUCKET, Key=f"artifacts/{sha256}/results.tar.gz",
                  Body=(work / "results.tar.gz").read_bytes())
    if pcap.exists():
        s3.put_object(Bucket=BUCKET, Key=f"artifacts/{sha256}/capture.pcap", Body=pcap.read_bytes())
    s3.put_object(Bucket=BUCKET, Key=f"reports/{sha256}.json", ContentType="application/json",
                  Body=json.dumps(report, indent=2).encode())
    return report


def run_job(vm, body):
    """Process one message; returns True to ack, False to requeue."""
    job = json.loads(body)
    sha256 = job["sha256"]
    log("job_started", sha256=sha256, vm=vm.name)
    if not set_status(sha256, "running", started_at=datetime.now(UTC), error=None):
        log("job_skipped", sha256=sha256, reason="no such sample")
        return True
    work = Path(tempfile.mkdtemp(dir=WORK_DIR))
    VM_BUSY.labels(vm.name).set(1)
    started = time.monotonic()
    try:
        report = detonate(vm, job, work)
        store_iocs(sha256, report["iocs"])
        set_status(sha256, "done", verdict=report["verdict"], score=report["score"],
                   finished_at=datetime.now(UTC))
        log("job_finished", sha256=sha256, vm=vm.name, verdict=report["verdict"],
            score=report["score"], duration_s=report["analysis"]["duration_s"])
        JOBS.labels(vm.name, "done", report["verdict"]).inc()
        return True
    except Exception as e:  # noqa: BLE001 - any failure: record it and let RabbitMQ retry
        log("job_failed", sha256=sha256, vm=vm.name, error=repr(e))
        set_status(sha256, "error", error=repr(e)[:500])
        JOBS.labels(vm.name, "error", "none").inc()
        return False
    finally:
        JOB_DURATION.labels(vm.name).observe(time.monotonic() - started)
        subprocess.run(["rm", "-rf", str(work)], check=False)
        try:
            vm.revert()  # never leave a sample running
        except Exception as e:  # noqa: BLE001
            REVERT_FAILURES.labels(vm.name).inc()
            log("revert_failed", vm=vm.name, error=repr(e))
        VM_BUSY.labels(vm.name).set(0)


# ---------------------------------------------------------------- RabbitMQ

def main(vm_name):
    vm = SandboxVM(vm_name)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    port = METRICS_BASE_PORT + int(vm_name.rsplit("-", 1)[1])
    prom.start_http_server(port, addr=METRICS_ADDR)
    VM_BUSY.labels(vm.name).set(0)
    for status, verdict in [("done", v) for v in ("clean", "suspicious", "malicious")] + [("error", "none")]:
        JOBS.labels(vm.name, status, verdict)  # export zeros, so rates work from the start
    connection = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
    channel = connection.channel()
    channel.basic_qos(prefetch_count=1)  # one job at a time per VM

    def on_message(ch, method, _props, body):
        # The analysis runs in a thread so this thread keeps serving the
        # connection (heartbeats); the thread hands the ack back when done.
        def work():
            ok = run_job(vm, body)
            settle = (functools.partial(ch.basic_ack, method.delivery_tag) if ok else
                      functools.partial(ch.basic_nack, method.delivery_tag, requeue=True))
            connection.add_callback_threadsafe(settle)
        threading.Thread(target=work, daemon=True).start()

    channel.basic_consume(JOBS_QUEUE, on_message)
    log("worker_ready", vm=vm.name, ip=vm.ip)
    channel.start_consuming()


if __name__ == "__main__":
    main(sys.argv[1])
