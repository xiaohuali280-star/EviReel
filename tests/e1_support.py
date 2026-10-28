"""Shared fixtures for E1 tests against REAL PostgreSQL and a REAL S3-compatible store.

Required environment (tests are skipped - and reported as skipped - when absent):
  EVIREEL_TEST_DATABASE_URL  admin URL of a scratch PostgreSQL server (needs CREATEDB);
                             every test gets its own freshly created database.
  EVIREEL_TEST_S3_ENDPOINT, EVIREEL_TEST_S3_ACCESS_KEY, EVIREEL_TEST_S3_SECRET_KEY
                             every test gets its own bucket.
Nothing here mocks storage or the database; failure points are injected around
real calls (e.g. a DB transition raising after a real multipart completion).
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

MiB = 1 << 20
DB_URL = os.environ.get("EVIREEL_TEST_DATABASE_URL", "")
S3 = {k: os.environ.get(f"EVIREEL_TEST_S3_{k.upper()}", "") for k in ("endpoint", "access_key", "secret_key")}
REAL_INFRA = bool(DB_URL and all(S3.values()))
requires_infra = pytest.mark.skipif(not REAL_INFRA, reason="real PostgreSQL + S3 not configured (EVIREEL_TEST_DATABASE_URL / EVIREEL_TEST_S3_*)")
PASSWORD = "e1-test-" + secrets.token_hex(8)   # generated per run, never printed


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + name))


@contextmanager
def scratch_database():
    import psycopg
    name = "evireel_e1_" + uuid.uuid4().hex[:12]
    with psycopg.connect(DB_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_db(DB_URL, name)
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@contextmanager
def scratch_bucket():
    import boto3
    from botocore.config import Config
    client = boto3.client("s3", endpoint_url=S3["endpoint"], aws_access_key_id=S3["access_key"],
                          aws_secret_access_key=S3["secret_key"], region_name="us-east-1",
                          config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    bucket = "e1-" + uuid.uuid4().hex[:16]
    try:
        yield bucket, client
    finally:
        try:
            for up in client.list_multipart_uploads(Bucket=bucket).get("Uploads", []):
                client.abort_multipart_upload(Bucket=bucket, Key=up["Key"], UploadId=up["UploadId"])
            for obj in client.list_objects_v2(Bucket=bucket).get("Contents", []):
                client.delete_object(Bucket=bucket, Key=obj["Key"])
            client.delete_bucket(Bucket=bucket)
        except Exception:
            pass


def make_video(path: Path, min_bytes: int, *, seconds: int = 3) -> Path:
    """Real, playable media large enough to need several 5 MiB parts (random-noise frames)."""
    while True:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=gray:s=640x360:r=25,noise=alls=100:allf=t+u", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=16000", "-t", str(seconds), "-c:v", "mpeg4", "-q:v", "1",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(path)], check=True, timeout=120)
        if path.stat().st_size >= min_bytes:
            return path
        seconds *= 2


def md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def fingerprint(data: bytes) -> str:
    return "fp1-" + hashlib.sha256(data[:MiB] + data[-MiB:] + str(len(data)).encode()).hexdigest()


def part_bytes(data: bytes, session: dict, n: int) -> bytes:
    start = (n - 1) * session["part_size"]
    return data[start:start + session["part_size"]] if n < session["part_count"] else data[start:]


def put_part(url: str, body: bytes, digest: str | None = None):
    import httpx
    return httpx.put(url, content=body, headers={"Content-MD5": digest or md5_b64(body)}, timeout=60)


def sign(client, upload_id, session, data, numbers):
    response = client.post(f"/api/uploads/{upload_id}/parts/sign", json={"parts": [
        {"part_number": n, "size": len(part_bytes(data, session, n)), "md5_b64": md5_b64(part_bytes(data, session, n))}
        for n in numbers]})
    assert response.status_code == 200, response.text
    return {p["part_number"]: p["url"] for p in response.json()["parts"]}


def upload_parts(client, session, data, numbers):
    urls = sign(client, session["upload_id"], session, data, numbers)
    for n in numbers:
        r = put_part(urls[n], part_bytes(data, session, n))
        assert r.status_code == 200, r.text


def wait_state(client, upload_id, states=("completed", "failed"), timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = client.get(f"/api/uploads/{upload_id}").json()
        if view["state"] in states:
            return view
        time.sleep(0.25)
    raise AssertionError(f"upload {upload_id} stuck in {view['state']}")


def wait_job(client, job_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.25)
    raise AssertionError("index job did not finish")
