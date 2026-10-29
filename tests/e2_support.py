"""Shared helpers for E2 tests (real PostgreSQL + real S3; see e1_support for the environment)."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from e1_support import MiB, S3, fingerprint, upload_parts, wait_state


def make_media(path: Path, *, seconds=3, color="red") -> Path:
    """Real playable video with an audio track (the ASR branch needs audio)."""
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"color=c={color}:s=160x120:r=4", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
                    "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y",
                    str(path)], check=True, timeout=60)
    return path


def e2_values(tmp_path, infra, **overrides):
    values = dict(_env_file=None, data_root=str(tmp_path / "assets"), frame_store_path=str(tmp_path / "default.sqlite3"),
                  enable_retrieval=False, asr_enabled=True, video_fps=1, video_laplacian_min=0, video_diff_threshold=0,
                  video_ssim_threshold=1.1, enable_event_finalize=False, query_include_latest_segment_video=False,
                  study_auto_generate=False, siliconflow_api_key="", dashscope_api_key="", openrouter_api_key="",
                  database_url=infra["db_url"], s3_endpoint=S3["endpoint"], s3_public_endpoint=S3["endpoint"],
                  s3_access_key=S3["access_key"], s3_secret_key=S3["secret_key"], s3_bucket=infra["bucket"],
                  upload_part_size=5 * MiB, upload_reconcile_interval_s=3600, upload_lease_s=30,
                  # Every fixture segment ends a sentence -> 5 chunks -> 3 embedding batches of <= 2.
                  pipeline_mode="worker", pipeline_factory="e2_factory:build", transcript_chunk_max_chars=40,
                  pipeline_embed_batch=2, pipeline_lease_s=30, pipeline_retry_base_s=0.2, pipeline_retry_cap_s=1,
                  pipeline_stage_max_attempts=3, pipeline_dispatch_interval_s=0.2, pipeline_redispatch_after_s=5,
                  pipeline_scratch=str(tmp_path / "scratch"))
    values.update(overrides)
    return values


def env_for(values) -> dict:
    """Settings as environment variables for worker/dispatcher subprocesses."""
    env = {}
    for key, value in values.items():
        if key.startswith("_"):
            continue
        env[key.upper()] = ("true" if value else "false") if isinstance(value, bool) else str(value)
    return env


def upload_video(client, user, data, name="lecture.mp4"):
    """E1 resumable upload through the API; returns the completed upload view (job accepted durably)."""
    response = user.post("/api/uploads", json={"filename": name, "size": len(data), "fingerprint": fingerprint(data),
                                               "content_type": "video/mp4"})
    assert response.status_code == 201, response.text
    session = response.json()
    upload_parts(user, session, data, list(range(1, session["part_count"] + 1)))
    user.post(f"/api/uploads/{session['upload_id']}/complete")
    done = wait_state(user, session["upload_id"])
    assert done["state"] == "completed", done
    deadline = time.monotonic() + 30
    while not done.get("job_id") and time.monotonic() < deadline:
        time.sleep(0.2)
        done = user.get(f"/api/uploads/{session['upload_id']}").json()
    assert done["job_id"], done
    return done


def job_row(db, job_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE job_id=%s", (job_id,)).fetchone()


def stage_rows(db, job_id):
    with db.connect() as conn:
        return {r["stage"]: r for r in conn.execute("SELECT * FROM job_stages WHERE job_id=%s", (job_id,))}


def attempt_rows(db, job_id, stage=None):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM stage_attempts WHERE job_id=%s ORDER BY started_at", (job_id,)).fetchall()
    return [r for r in rows if stage is None or r["stage"] == stage]


def drain_inline(ctx, job_id, *, timeout=120, before_execute=None):
    """Broker stand-in for in-process tests: publish due outbox rows into a list and run them with the
    real runner. Exercises every ledger transition without RabbitMQ (that path is test_e2_faults)."""
    from vam.pipeline.runner import execute
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sent = []
        ctx.repo.publish_due(sent.append)
        for row in sent:
            if before_execute:
                before_execute(row)
            execute(ctx, row["job_id"], row["stage"])
        job = job_row(ctx.db, job_id)
        # The ledger decides at publish; the final `project` stage then applies it to the read model.
        if job["status"] in {"succeeded", "partial", "failed"} and all(
                s["status"] in {"succeeded", "failed", "skipped"} for s in stage_rows(ctx.db, job_id).values()):
            return job
        if not sent:
            ctx.repo.scan()
            time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish: {stage_rows(ctx.db, job_id)}")
