"""Detonator API: accepts samples, stores them, and queues analysis jobs.

Samples are identified by SHA-256. The file goes to S3-compatible object storage,
its metadata to Postgres, and a job message to RabbitMQ for the worker. A file
that was already submitted returns its existing record instead of a new job.
"""
import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import boto3
import pika
import psycopg
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import Response
from prometheus_client import Counter, make_asgi_app
from psycopg.rows import dict_row

DATABASE_URL = os.environ["DATABASE_URL"]
AMQP_URL = os.environ["AMQP_URL"]
S3_ENDPOINT = os.environ["S3_ENDPOINT"]
S3_ACCESS_KEY = os.environ["S3_ACCESS_KEY"]
S3_SECRET_KEY = os.environ["S3_SECRET_KEY"]
BUCKET = os.environ.get("S3_BUCKET", "detonator")

# Same limit as the guest agent: the sample has to fit through it.
MAX_SAMPLE_BYTES = 64 * 1024 * 1024
JOBS_QUEUE = "analysis.jobs"
DEAD_EXCHANGE = "analysis.dlx"
DEAD_QUEUE = "analysis.dead"

submissions = Counter("detonator_submissions_total", "Sample submissions", ["result"])

s3 = boto3.client("s3", endpoint_url=S3_ENDPOINT, aws_access_key_id=S3_ACCESS_KEY,
                  aws_secret_access_key=S3_SECRET_KEY, region_name="us-east-1")


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def declare_topology(channel):
    """Jobs queue plus a dead-letter queue for jobs that fail repeatedly.

    Idempotent; the worker declares the same topology with the same arguments.
    """
    channel.exchange_declare(DEAD_EXCHANGE, "fanout", durable=True)
    channel.queue_declare(DEAD_QUEUE, durable=True, arguments={"x-queue-type": "quorum"})
    channel.queue_bind(DEAD_QUEUE, DEAD_EXCHANGE)
    channel.queue_declare(JOBS_QUEUE, durable=True, arguments={
        "x-queue-type": "quorum",          # replicated, durable queue type
        "x-dead-letter-exchange": DEAD_EXCHANGE,
        "x-delivery-limit": 3,             # after 3 failed deliveries → analysis.dead
    })


def publish_job(job):
    # One short connection per job keeps this simple; volume is low. Publisher
    # confirms make basic_publish raise unless the broker has stored the message.
    connection = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
    try:
        channel = connection.channel()
        channel.confirm_delivery()
        channel.basic_publish("", JOBS_QUEUE, json.dumps(job).encode(),
                              pika.BasicProperties(delivery_mode=pika.DeliveryMode.Persistent,
                                                   content_type="application/json"),
                              mandatory=True)
    finally:
        connection.close()


def init():
    with db() as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text())
    try:
        s3.head_bucket(Bucket=BUCKET)
    except ClientError:
        s3.create_bucket(Bucket=BUCKET)
    connection = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
    try:
        declare_topology(connection.channel())
    finally:
        connection.close()


@asynccontextmanager
async def lifespan(_app):
    # Dependencies may still be starting when the pod comes up; wait for them.
    # init() blocks, so it runs in a thread and the event loop stays free.
    for attempt in range(30):
        try:
            await asyncio.to_thread(init)
            break
        except Exception as e:  # noqa: BLE001 - any failure means "not ready yet"
            print(f"init attempt {attempt + 1} failed: {e}", flush=True)
            await asyncio.sleep(2)
    else:
        raise RuntimeError("dependencies not reachable")
    yield


app = FastAPI(title="detonator", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


@app.get("/healthz")
def healthz():
    with db() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/samples", status_code=202)
def submit(file: UploadFile, response: Response, force: bool = False):
    data = file.file.read(MAX_SAMPLE_BYTES + 1)
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_SAMPLE_BYTES:
        raise HTTPException(413, f"sample larger than {MAX_SAMPLE_BYTES} bytes")
    sha256 = hashlib.sha256(data).hexdigest()
    filename = os.path.basename(file.filename or "") or "sample"

    with db() as conn:
        row = conn.execute(
            "INSERT INTO samples (sha256, filename, size) VALUES (%s, %s, %s) "
            "ON CONFLICT (sha256) DO NOTHING RETURNING *", (sha256, filename, len(data))
        ).fetchone()
        if row is None:  # seen before
            existing = conn.execute("SELECT * FROM samples WHERE sha256 = %s", (sha256,)).fetchone()
            if not force:
                submissions.labels("duplicate").inc()
                response.status_code = 200
                return existing
            row = conn.execute(
                "UPDATE samples SET status = 'queued', verdict = NULL, error = NULL, "
                "finished_at = NULL WHERE sha256 = %s RETURNING *", (sha256,)).fetchone()
        # Content-addressed: the object key is the hash, so re-uploads are harmless.
        s3.put_object(Bucket=BUCKET, Key=f"samples/{sha256}", Body=data)
        # Publish before the transaction commits: if RabbitMQ is down, the row is
        # rolled back instead of being left "queued" with no job behind it.
        try:
            publish_job({"sha256": sha256, "filename": filename})
        except pika.exceptions.AMQPError as e:
            raise HTTPException(503, f"queue unavailable: {e}") from e
    submissions.labels("requeued" if force else "new").inc()
    return row


@app.get("/samples/{sha256}")
def status(sha256: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM samples WHERE sha256 = %s", (sha256.lower(),)).fetchone()
    if row is None:
        raise HTTPException(404, "unknown sample")
    return row


@app.get("/samples/{sha256}/report")
def report(sha256: str):
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=f"reports/{sha256.lower()}.json")
    except ClientError as e:
        raise HTTPException(404, "no report yet") from e
    return Response(obj["Body"].read(), media_type="application/json")
