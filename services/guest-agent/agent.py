#!/usr/bin/env python3
"""Sandbox guest agent: receives a sample, runs it under strace, returns the results.

Runs inside every sandbox VM. It listens on the isolated analysis network only and
has no authentication: the network, not the agent, is the security boundary.
Standard library only, so the guest image needs nothing beyond python3 and strace.

API
  GET  /health              liveness check
  POST /upload?name=<file>  request body is the sample; resets the previous job
  POST /execute             JSON body {"timeout": seconds}; runs the sample
  GET  /results             tar.gz with traces, output, file-system diff, dropped files
"""
import hashlib
import io
import json
import os
import pwd
import shutil
import signal
import subprocess
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PORT = int(os.environ.get("AGENT_PORT", "8000"))
JOB_DIR = Path(os.environ.get("AGENT_JOB_DIR", "/var/lib/guest-agent/job"))
# The sample runs as an ordinary desktop-style user, not as root.
RUN_AS = os.environ.get("AGENT_RUN_AS", "alex")

MAX_SAMPLE_BYTES = 64 * 1024 * 1024
MAX_TIMEOUT_S = 300
MAX_DROPPED_BYTES = 10 * 1024 * 1024
WATCHED_DIRS = ["/etc", "/home", "/root", "/tmp", "/var/tmp", "/var/spool/cron", "/dev/shm"]

busy = threading.Lock()


def snapshot():
    """Map every regular file under WATCHED_DIRS to (size, mtime) for diffing."""
    state = {}
    for top in WATCHED_DIRS:
        for root, _, files in os.walk(top):
            for name in files:
                path = os.path.join(root, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if os.path.isfile(path) and not os.path.islink(path):
                    state[path] = (st.st_size, st.st_mtime_ns)
    return state


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_dropped(before, after, sample_path):
    """Diff the two snapshots and copy new or changed files into the job directory."""
    created = sorted(set(after) - set(before) - {str(sample_path)})
    modified = sorted(p for p in set(after) & set(before) if after[p] != before[p])
    deleted = sorted(set(before) - set(after))
    dropped_dir = JOB_DIR / "dropped"
    dropped_dir.mkdir()
    files = []
    for path in created + modified:
        entry = {"path": path, "size": after[path][0],
                 "change": "created" if path in created else "modified"}
        if after[path][0] <= MAX_DROPPED_BYTES:
            try:
                entry["sha256"] = sha256_file(path)
                shutil.copyfile(path, dropped_dir / entry["sha256"])
            except OSError as e:
                entry["error"] = str(e)
        files.append(entry)
    return {"files": files, "deleted": deleted}


def execute(timeout):
    meta = json.loads((JOB_DIR / "meta.json").read_text())
    sample = Path(meta["path"])
    user = pwd.getpwnam(RUN_AS)
    (JOB_DIR / "trace").mkdir()
    env = {"HOME": user.pw_dir, "USER": RUN_AS, "LOGNAME": RUN_AS,
           "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    # strace follows every child (-ff, one file per process) and drops privileges
    # to RUN_AS (-u) before executing the sample.
    cmd = ["strace", "-ff", "-tt", "-s", "256", "-o", str(JOB_DIR / "trace" / "strace"),
           "-u", RUN_AS, str(sample)]

    before = snapshot()
    started = time.monotonic()
    with open(JOB_DIR / "stdout.log", "wb") as out, open(JOB_DIR / "stderr.log", "wb") as err:
        proc = subprocess.Popen(cmd, cwd=user.pw_dir, env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, start_new_session=True)
        try:
            exit_code, timed_out = proc.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            exit_code, timed_out = proc.wait(), True
    duration = round(time.monotonic() - started, 3)
    # Anything the sample left running (daemonised children) is killed too.
    subprocess.run(["pkill", "-KILL", "-u", RUN_AS], check=False)

    fs = collect_dropped(before, snapshot(), sample)
    meta.update(exit_code=exit_code, timed_out=timed_out, duration_s=duration)
    (JOB_DIR / "meta.json").write_text(json.dumps(meta, indent=2))
    (JOB_DIR / "fsdiff.json").write_text(json.dumps(fs, indent=2))
    return {"exit_code": exit_code, "timed_out": timed_out, "duration_s": duration,
            "files_changed": len(fs["files"]), "files_deleted": len(fs["deleted"])}


class Handler(BaseHTTPRequestHandler):
    server_version = "guest-agent/0.1"

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self, limit):
        length = int(self.headers.get("Content-Length", 0))
        if length > limit:
            raise ValueError(f"body larger than {limit} bytes")
        return self.rfile.read(length)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "busy": busy.locked()})
        elif self.path == "/results":
            if not (JOB_DIR / "fsdiff.json").exists():
                return self._json(409, {"error": "no finished job"})
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                tar.add(JOB_DIR, arcname="results")
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path not in ("/upload", "/execute"):
            return self._json(404, {"error": "not found"})
        if not busy.acquire(blocking=False):
            return self._json(409, {"error": "a job is running"})
        try:
            if url.path == "/upload":
                self._upload(parse_qs(url.query).get("name", ["sample"])[0])
            else:
                self._execute()
        except ValueError as e:
            self._json(400, {"error": str(e)})
        finally:
            busy.release()

    def _upload(self, name):
        data = self._body(MAX_SAMPLE_BYTES)
        if not data:
            raise ValueError("empty sample")
        user = pwd.getpwnam(RUN_AS)
        shutil.rmtree(JOB_DIR, ignore_errors=True)
        JOB_DIR.mkdir(parents=True, mode=0o700)
        # Like a downloaded file: in the user's home, owned by them, executable.
        path = Path(user.pw_dir) / (os.path.basename(name) or "sample")
        path.write_bytes(data)
        os.chown(path, user.pw_uid, user.pw_gid)
        path.chmod(0o755)
        meta = {"name": path.name, "path": str(path), "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}
        (JOB_DIR / "meta.json").write_text(json.dumps(meta, indent=2))
        self._json(200, meta)

    def _execute(self):
        if not (JOB_DIR / "meta.json").exists():
            raise ValueError("upload a sample first")
        if (JOB_DIR / "fsdiff.json").exists():
            raise ValueError("sample already executed; upload again")
        params = json.loads(self._body(4096) or b"{}")
        timeout = min(int(params.get("timeout", 60)), MAX_TIMEOUT_S)
        self._json(200, execute(timeout))

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
