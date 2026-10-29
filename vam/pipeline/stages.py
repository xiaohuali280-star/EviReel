"""Stage bodies. Each returns StageResult(output, skip, job_result, follow_up).

Side effects before publication are immutable (attempt-keyed objects / index generations) or
content-addressed (staged transcript versions), never visible to readers. Everything a user sees -
active visual generation, playback path, transcript heads, video/job status, study artifacts - is
written only by `publish` / `notes_publish`, fenced by the job's sequence number (`jobs.seq`): a
stale attempt of an older job cannot overwrite a newer publication, and a re-run of the same job
writes identical values (which is how the PostgreSQL/SQLite commit window recovers)."""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field

from vam.asr.audio import ASRError, extract_audio
from vam.asr.chunker import CHUNK_RULE_VERSION, make_chunks, normalize_segments
from vam.indexing.media import prepare_playback
from vam.memory.transcript import embedding_fingerprint
from vam.pipeline.context import StageError, fault, sha256_file
from vam.pipeline.repository import canonical, sha256_hex
from vam.video_registry import DomainError
from vam.vision.backends import validate_embeddings

# Bump when a stage's product format/semantics change: part of every checkpoint reuse key.
STAGE_CODE_VERSION = {"asr": 1, "embed": 1, "notes": 1}


@dataclass
class StageResult:
    output: dict
    skip: dict = field(default_factory=dict)
    job_result: dict | None = None
    follow_up: list = field(default_factory=list)


def _chunk_config(cfg):
    return canonical({"version": CHUNK_RULE_VERSION, "max_chars": cfg.transcript_chunk_max_chars,
                      "max_duration_s": cfg.transcript_chunk_max_duration_s, "pause_s": cfg.transcript_chunk_pause_s,
                      "audio_mapping_version": 1})


def _outputs(ctx, job_id):
    return {name: row for name, row in ctx.repo.stages(job_id).items()}


# ---------------------------------------------------------------------------------------------
def prepare(ctx, claim):
    """Source validation shared by both branches, plus the playback file for video jobs.
    A source that is not a usable video fails here, which correctly stops both branches."""
    job, token = claim["job"], claim["stage"]["attempt_token"]
    video_id, cfg = job["video_id"], ctx.settings
    source = ctx.source(video_id, token)
    output = {}
    try:
        from vam.video import _probe_video_metadata
        output["duration"] = _probe_video_metadata(str(source))["duration"]
        if job["kind"] == "video":
            # prepare_playback writes playback.mp4 atomically from the immutable source (identical
            # content on any re-run); readers only see it once publish sets playback_path.
            playback = prepare_playback(source, ctx.registry.directory(video_id), cfg)
            if playback == source:  # already browser-playable: point at the registry's own copy
                playback = ctx.registry.asset(video_id, ctx.registry.get(video_id)["source_path"])
            output["playback_path"] = playback.name
    except DomainError as exc:
        raise StageError(exc.code, exc.message, transient=False) from exc
    output["source_sha256"] = sha256_file(source)
    return StageResult(output)


def audio(ctx, claim):
    """Speech-branch only: 16 kHz mono PCM audio artifact. Its failures never block the visual branch."""
    job, token = claim["job"], claim["stage"]["attempt_token"]
    cfg = ctx.settings
    if not cfg.asr_enabled:
        return StageResult({"audio": None, "reason": "disabled"},
                           skip={"asr": "asr_disabled", "transcript_index": "asr_disabled"})
    source = ctx.source(job["video_id"], token)
    try:
        result = extract_audio(source, ctx.workdir(token) / "audio.wav", cfg)
    except ASRError as exc:
        raise StageError(exc.code, str(exc), transient=exc.code == "audio_timeout") from exc
    if result is None:
        return StageResult({"audio": None, "reason": "no_audio"},
                           skip={"asr": "no_audio", "transcript_index": "no_audio"})
    ref = ctx.put_artifact(ctx.artifact_key(job, "audio", token, "audio.wav"), result.path, "audio/wav")
    return StageResult({"audio": ref | {"offset_s": result.offset_s, "duration_s": result.duration_s,
                                        "sample_rate": result.sample_rate}})


# ---------------------------------------------------------------------------------------------
def asr(ctx, claim):
    """Speech recognition with a content-addressed checkpoint (source sha + ASR identity + chunk rules)."""
    job, token = claim["job"], claim["stage"]["attempt_token"]
    outputs = _outputs(ctx, job["job_id"])
    prep, sound = outputs["prepare"]["output"], outputs["audio"]["output"]["audio"]
    asr_identity = canonical(ctx.transcriber.fingerprint())
    transcription_key = sha256_hex(canonical([prep["source_sha256"], asr_identity, _chunk_config(ctx.settings)]))
    reuse_key = "asr:" + sha256_hex(canonical([transcription_key, STAGE_CODE_VERSION["asr"]]))
    work = ctx.workdir(token)
    hit = ctx.repo.checkpoint(reuse_key)
    if hit:
        ref = {"key": hit["object_key"], "sha256": hit["sha256"]}
        try:
            ctx.fetch(ref, work / "asr-check.json")   # a checkpoint is only reused after verification
            return StageResult({"asr": ref, "transcription_key": transcription_key, "asr_identity": asr_identity,
                                "reused": True})
        except StageError as exc:
            if exc.transient:
                raise
            ctx.repo.drop_checkpoint(reuse_key, hit["object_key"])  # corrupt/missing product: recompute
    wav = ctx.fetch(sound, work / "audio.wav")
    try:
        result = ctx.transcriber.transcribe(wav)
    except ASRError as exc:
        transient = exc.code in {"asr_timeout", "asr_worker_failed"}
        raise StageError(exc.code, str(exc), transient=transient) from exc
    payload = {"segments": result.segments, "language": result.language, "offset_s": sound["offset_s"],
               "audio_duration_s": sound["duration_s"], "sample_rate": sound["sample_rate"],
               "duration": prep["duration"]}
    ref = ctx.put_json(ctx.artifact_key(job, "asr", token, "segments.json"), payload, work / "segments.json")
    fault("after_asr_upload")
    winner = ctx.repo.save_checkpoint(reuse_key, ref["key"], ref["sha256"], ref["size"], token)
    if winner is None:   # our upload was claimed by the orphan sweep while we were paused: never reference it
        raise StageError("artifact_reclaimed", "The uploaded ASR product was reclaimed; re-running", transient=True)
    ref = {"key": winner["object_key"], "sha256": winner["sha256"]}
    return StageResult({"asr": ref, "transcription_key": transcription_key, "asr_identity": asr_identity, "reused": False})


# ---------------------------------------------------------------------------------------------
def transcript_index(ctx, claim):
    """Chunking + text embeddings with per-batch checkpoints into a content-addressed transcript version.
    Readers do not see it until `publish` moves the heads (fenced)."""
    job, stage, token = claim["job"], claim["stage"], claim["stage"]["attempt_token"]
    video_id, cfg, backend = job["video_id"], ctx.settings, ctx.backend
    outputs = _outputs(ctx, job["job_id"])
    prep, asr_out = outputs["prepare"]["output"], outputs["asr"]["output"]
    work = ctx.workdir(token)
    payload = ctx.fetch_json(asr_out["asr"], work / "segments.json")
    segments = normalize_segments(payload["segments"], offset_s=payload["offset_s"], duration_s=payload["duration"],
                                  language=payload["language"], max_segments=cfg.asr_max_segments,
                                  max_chars=cfg.asr_max_text_chars)
    chunks = make_chunks(segments, max_chars=cfg.transcript_chunk_max_chars,
                         max_duration_s=cfg.transcript_chunk_max_duration_s, pause_s=cfg.transcript_chunk_pause_s)
    metadata = {"audio_offset_s": payload["offset_s"], "audio_duration_s": payload["audio_duration_s"],
                "sample_rate": payload["sample_rate"], "clipped_segments": sum(s.time_clipped for s in segments),
                "timing_precision": "segment",
                "long_segment_chunks": sum(c.end_t - c.start_t > cfg.transcript_chunk_max_duration_s for c in chunks)}
    identity = embedding_fingerprint(backend)
    transcription_key = asr_out["transcription_key"]
    version = sha256_hex(canonical([transcription_key, identity]))
    store = ctx.transcript_store(video_id, job["job_id"])
    existing = store.version(version)
    if existing and existing["status"] in {"search_ready", "empty"}:
        return StageResult({"status": "empty" if existing["status"] == "empty" else "ready", "version": version,
                            "reused": True, "batches": {"reused": 0, "computed": 0}})
    store.stage(version=version, transcription_key=transcription_key, source_sha256=prep["source_sha256"],
                asr_fingerprint=asr_out["asr_identity"], chunk_config=_chunk_config(cfg), embedding_identity=identity,
                segments=segments, chunks=chunks, language=payload["language"], metadata=metadata)
    if not chunks:
        store.complete_version(version, empty=True)
        return StageResult({"status": "empty", "reason": "empty_transcription", "version": version})
    vectors, dim, stats = [], None, {"reused": 0, "computed": 0}
    size = cfg.pipeline_embed_batch
    try:
        for index, pos in enumerate(range(0, len(chunks), size)):
            texts = [c.text for c in chunks[pos:pos + size]]
            reuse_key = "emb:" + sha256_hex(canonical([identity, "search_document", texts, STAGE_CODE_VERSION["embed"]]))
            hit = ctx.repo.checkpoint(reuse_key)
            batch = None
            if hit:
                try:
                    batch = ctx.fetch_json({"key": hit["object_key"], "sha256": hit["sha256"]}, work / f"emb-{index}.json")
                    stats["reused"] += 1
                except StageError as exc:
                    if exc.transient:
                        raise
                    ctx.repo.drop_checkpoint(reuse_key, hit["object_key"])
            if batch is None:
                batch_vectors, batch_dim = backend.embed_texts_batch(texts, instruction="search_document", batch_size=size)
                validate_embeddings(batch_vectors, expected_count=len(texts), expected_dim=dim or batch_dim)
                if not isinstance(batch_dim, int) or batch_dim != len(batch_vectors[0]):
                    raise RuntimeError("transcript embedding dimension mismatch")
                if any(math.sqrt(sum(x * x for x in v)) <= 0 for v in batch_vectors):
                    raise RuntimeError("transcript embeddings must be nonzero")
                batch = {"dim": batch_dim, "vectors": batch_vectors}
                ref = ctx.put_json(ctx.artifact_key(job, "transcript_index", token, f"emb-{index}.json"), batch,
                                   work / f"emb-{index}.json")
                # None = the object was reclaimed meanwhile: the vectors in hand are still valid, only
                # not reusable later (the object is not referenced and will be deleted).
                ctx.repo.save_checkpoint(reuse_key, ref["key"], ref["sha256"], ref["size"], token)
                stats["computed"] += 1
                fault(f"after_embed_batch:{index}")
            if dim is not None and batch["dim"] != dim:
                raise RuntimeError("transcript embedding dimension changed between batches")
            dim = batch["dim"]
            vectors.extend(batch["vectors"])
    except StageError:
        raise
    except Exception as exc:
        # Embedding outage: retry the stage (finished batches are checkpointed); on the last attempt
        # degrade exactly like the inline indexer did - publish the text, report "partial".
        if stage["attempts"] < cfg.pipeline_stage_max_attempts:
            raise StageError("transcript_embedding_failed", f"{type(exc).__name__}: {exc}", transient=True) from exc
        store.complete_version(version)
        message = "Transcript text is available; semantic embedding failed. Retry the transcript job."
        return StageResult({"status": "partial", "error_code": "transcript_embedding_failed", "error": message,
                            "version": version, "batches": stats})
    store.complete_version(version, vectors=vectors, dim=dim)
    return StageResult({"status": "ready", "version": version, "batches": stats})


# ---------------------------------------------------------------------------------------------
class _VisionProgress:
    """index_video_async's job-manager protocol, mirrored into the UI read model."""
    def __init__(self, ctx, job):
        self.ctx, self.job, self.result, self.error = ctx, job, None, None

    def log(self, job_id, msg):
        pass

    def progress(self, job_id, *, phase=None, current=None, total=None):
        fields = {k: v for k, v in {"phase": phase, "current": current, "total": total}.items() if v is not None}
        if fields:   # never reopens a finished row (a stale attempt keeps reporting progress)
            self.ctx.registry.progress_job(self.job["job_id"], status="running", **fields)

    def done(self, job_id, *, result):
        self.result = result

    def fail(self, job_id, *, error):
        self.error = str(error)[:800]


def vision(ctx, claim):
    """Frames, image embeddings and events into an attempt-keyed index generation (memory/<token>)."""
    from vam.retrieval.frame_store import FrameStore
    from vam.video import index_video_async
    job, token = claim["job"], claim["stage"]["attempt_token"]
    video_id, cfg = job["video_id"], ctx.settings
    source = ctx.source(video_id, token)
    path = ctx.registry.asset(video_id, f"memory/{token}.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        store = FrameStore(backend=ctx.backend, persist_path=str(path))
    except RuntimeError as exc:
        raise StageError("incompatible_index", str(exc), transient=False) from exc
    assets = ctx.registry.asset(video_id, f"cache/{token}")
    assets.mkdir(parents=True, exist_ok=True)
    store.asset_root = str(assets)
    progress = _VisionProgress(ctx, job)

    async def run():
        await index_video_async(
            tmp_path=str(source), cleanup_temp=False, fps=cfg.video_fps, max_frames=cfg.video_max_frames,
            laplacian_min=cfg.video_laplacian_min, diff_threshold=cfg.video_diff_threshold,
            ssim_threshold=cfg.video_ssim_threshold, hist_threshold=cfg.video_hist_threshold,
            similarity_threshold=cfg.video_similarity_threshold, frame_id_prefix="v",
            video_absolute_start_time=None, debug=False, store_instance=store, manager_instance=progress,
            existing_job_id=job["job_id"], cache_root=store.asset_root, run_inline=True)
        return await store.list_frames()
    frames = asyncio.run(run())
    if progress.error:
        raise StageError("index_failed", progress.error, transient=True)
    result = progress.result or {}
    if result.get("count", 0) <= 0 or not frames:
        raise StageError("index_empty", "Index contains no usable frames", transient=False)
    partial = bool(result.get("truncated") or result.get("filter_report", {}).get("embed_failures", {}).get("count"))
    result["model"] = {"fingerprint": store.model_fingerprint(), "dimension": store._dim}
    return StageResult({"generation": token, "status": "partial" if partial else "ready", "result": result,
                        "fingerprint": store.model_fingerprint()})


# ---------------------------------------------------------------------------------------------
def _branch(stages, name, fallback_reason):
    row = stages.get(name)
    if row is None:
        return None
    if row["status"] == "succeeded":
        return row["output"]
    if row["status"] == "skipped":
        return {"status": "skipped", "reason": row["error_code"] or fallback_reason}
    return {"status": "failed", "error_code": row["error_code"] or f"{name}_failed",
            "error": row["error"] or f"{name} failed; previous published results are retained"}




def _speech(stages):
    """Outcome of the speech branch (prepare -> audio -> asr -> transcript_index): the first real failure
    wins; a skip caused by a failed dependency is that failure, not "no speech"."""
    for name in ("prepare", "audio", "asr", "transcript_index"):
        row = stages[name]
        if row["status"] == "failed":
            return {"status": "failed", "error_code": row["error_code"] or f"{name}_failed",
                    "error": row["error"] or "Transcript indexing failed; previous published text/index is retained"}
        if name == "audio" and row["status"] == "succeeded" and row["output"].get("reason"):
            return {"status": "skipped", "reason": row["output"]["reason"]}
    final = stages["transcript_index"]
    if final["status"] == "succeeded":
        return final["output"]
    return {"status": "skipped", "reason": final["error_code"] or "dependency_not_succeeded"}


def publish(ctx, claim):
    """Join: DECIDE the job's outcome. Writes nothing visible: the decision becomes the job's final state
    through the attempt-token-guarded ledger commit, and only `project` applies it to the read model."""
    job = claim["job"]
    job_id, video_id = job["job_id"], job["video_id"]
    stages = ctx.repo.stages(job_id)
    vis = _branch(stages, "vision", "dependency_not_succeeded") if job["kind"] == "video" else None
    transcript = _speech(stages)
    vision_ok = bool(vis and vis.get("generation"))
    t = transcript.get("status")
    if job["kind"] == "transcript":
        status = "failed" if t == "failed" else ("partial" if t == "partial" else "succeeded")
    elif vision_ok:
        status = "partial" if vis["status"] == "partial" or t in {"failed", "partial"} else "succeeded"
    else:
        status = "partial" if t in {"ready", "partial", "empty"} else "failed"
    error = transcript if t in {"failed", "partial"} else (vis if vis and vis.get("status") == "failed" else {})
    result = dict(vis["result"]) if vision_ok else {}
    vision_state = ({"status": vis["status"]} if vision_ok else (vis if vis else {"status": "not_requested"}))
    result.update(video_id=video_id, stages={"vision": vision_state, "transcript": transcript})
    row = ctx.registry.get(video_id)
    if vision_ok:
        row["active_generation"] = vis["generation"]
    prep = stages["prepare"]
    playback = prep["output"].get("playback_path") if prep["status"] == "succeeded" else None
    if playback:
        row["playback_path"] = playback
    result["capabilities"] = ctx.registry.public(row)["capabilities"]
    decision = {"status": status, "error_code": error.get("error_code"), "error": error.get("error"),
                "vision_generation": vis["generation"] if vision_ok else None,
                "fingerprint": vis["fingerprint"] if vision_ok else None, "playback_path": playback,
                "transcript": {"status": t, "version": transcript.get("version"), "error": transcript.get("error")}}
    follow = []
    if (job["kind"] == "video" and status in {"succeeded", "partial"}
            and ctx.settings.study_auto_generate and ctx.study_model_configured):
        follow.append({"kind": "notes", "request_id": f"auto-notes:{job_id}"})   # same transaction as the decision
    return StageResult({"decision": decision}, follow_up=follow,
                       job_result={"status": status, "result": result, "error_code": error.get("error_code"),
                                   "error": error.get("error")})


def job_fence(job) -> int:
    """Projection fence: jobs.seq. Projections are derived only from the committed ledger, so every
    attempt of one job writes identical values; the fence only has to order different jobs."""
    return int(job["seq"])


def project(ctx, claim):
    """Final stage of every video/transcript/notes job: apply the ledger's committed outcome to the
    read model (SQLite registry, transcript heads, study_jobs). Runs after publish whatever happened
    (succeeded, failed, or failed by the scanner after a lost worker); idempotent and fenced by job order."""
    job = claim["job"]
    job_id, video_id, fence = job["job_id"], job["video_id"], job_fence(job)
    stages = ctx.repo.stages(job_id)
    if job["kind"] == "notes":
        published = stages["notes_publish"]
        if published["status"] == "succeeded":
            ctx.registry.publish_study_artifact_state(job_id, published["output"]["artifact_id"])
            return StageResult({"status": "succeeded"})
        failed = next((s for s in (stages["notes_draft"], published) if s["status"] == "failed"), None)
        code = failed["error_code"] if failed else "study_generation_failed"
        ctx.project(job)
        ctx.registry.finish_study_job(job_id, status="failed", error_code=code)
        return StageResult({"status": "failed", "error_code": code})
    ctx.project(job)
    decided = stages["publish"]
    if decided["status"] != "succeeded":   # the ledger failed the job (worker error or attempts exhausted)
        ledger = ctx.repo.job(job_id)
        published = ctx.registry.publish_fenced(video_id, job_id, fence=fence, status="failed", result={},
                                                error_code=ledger["error_code"] or decided["error_code"],
                                                error=ledger["error"] or decided["error"])
        return StageResult({"status": "failed", "published": published})
    d = decided["output"]["decision"]
    heads_moved = None
    if d["transcript"]["status"] in {"ready", "partial", "empty"}:
        heads_moved = ctx.transcript_store(video_id, job_id).publish_head(d["transcript"]["version"], fence=fence)
    ledger = ctx.repo.job(job_id)
    published = ctx.registry.publish_fenced(
        video_id, job_id, fence=fence, status=d["status"], result=ledger["result"] or {}, error_code=d["error_code"],
        error=d["error"], vision_generation=d["vision_generation"], fingerprint=d["fingerprint"],
        playback_path=d["playback_path"], transcript_status=d["transcript"]["status"],
        transcript_error=d["transcript"]["error"])
    if not published:
        ctx.registry.progress_job(job_id, status=d["status"], phase="superseded")   # a newer job already published
    # Deferred cleanup: generations of vision attempts that did not win are never published.
    for attempt in ctx.repo.attempts(job_id):
        if attempt["stage"] == "vision" and attempt["attempt_token"] != d["vision_generation"]:
            ctx.drop_generation(video_id, attempt["attempt_token"])
    if job["kind"] == "video" and d["transcript"]["status"] in {"failed", "skipped"}:
        ctx.drop_generation(video_id, job_id)  # a transcript file this job created but never published
    return StageResult({"status": d["status"], "published": published, "transcript_heads_moved": heads_moved})



# ---------------------------------------------------------------------------------------------
def _study(ctx):
    from vam.study import StudyService
    return StudyService(ctx.study_indexing(), model=ctx.study_model)


def notes_draft(ctx, claim):
    """Study notes, one model call per batch; every validated batch is a checkpoint, so an interrupted
    job resumes at the first missing batch (and never pays twice for a verified batch)."""
    job, token = claim["job"], claim["stage"]["attempt_token"]
    video_id = job["video_id"]
    study = _study(ctx)
    try:
        row, versions, records = asyncio.run(study.corpus(video_id))
        batches = study.batches(records)
    except DomainError as exc:
        raise StageError(exc.code, exc.message, transient=False) from exc
    ctx.project(job, status="running", phase="generating", total=len(batches))
    identity = ctx.study_identity()
    work, chapters, stats = ctx.workdir(token), [], {"reused": 0, "computed": 0}
    for index, batch in enumerate(batches):
        reuse_key = "notes:" + sha256_hex(canonical([identity, STAGE_CODE_VERSION["notes"],
                                                     [(e["evidence_id"], e["text"]) for e in batch]]))
        hit, drafted = ctx.repo.checkpoint(reuse_key), None
        if hit:
            try:
                drafted = ctx.fetch_json({"key": hit["object_key"], "sha256": hit["sha256"]}, work / f"notes-{index}.json")
                stats["reused"] += 1
            except StageError as exc:
                if exc.transient:
                    raise
                ctx.repo.drop_checkpoint(reuse_key, hit["object_key"])
        if drafted is None:
            try:
                drafted = asyncio.run(study.draft(batch))
            except ValueError as exc:   # invalid/incomplete model output: a new attempt may succeed
                raise StageError("study_invalid_output", str(exc)[:300], transient=True) from exc
            except DomainError as exc:
                raise StageError(exc.code, exc.message, transient=exc.status >= 500) from exc
            ref = ctx.put_json(ctx.artifact_key(job, "notes_draft", token, f"batch-{index}.json"), drafted,
                               work / f"notes-{index}.json")
            ctx.repo.save_checkpoint(reuse_key, ref["key"], ref["sha256"], ref["size"], token)  # None: not reusable
            stats["computed"] += 1
            fault(f"after_notes_batch:{index}")
        chapters.extend(drafted)
        ctx.project(job, current=index + 1)
    chapters.sort(key=lambda c: (c["start_t"], c["end_t"]))
    content = {"title": row["original_name"], "chapters": chapters, "evidence": records,
               "warnings": ["AI 生成笔记，请结合原视频核对；语音文稿可能存在识别误差。"]}
    ref = ctx.put_json(ctx.artifact_key(job, "notes_draft", token, "notes.json"),
                       {"versions": versions, "content": content}, work / "notes.json")
    return StageResult({"notes": ref, "batches": stats})


def notes_publish(ctx, claim):
    """Idempotent publication of the notes artifact (deterministic id, ordered by job creation time)."""
    job, token = claim["job"], claim["stage"]["attempt_token"]
    draft = ctx.repo.stages(job["job_id"])["notes_draft"]["output"]
    payload = ctx.fetch_json(draft["notes"], ctx.workdir(token) / "notes.json")
    ctx.project(job)
    artifact_id = "a_" + job["job_id"]
    ctx.registry.publish_study_artifact(job["video_id"], job["job_id"], artifact_id=artifact_id,
                                        versions=payload["versions"], content=payload["content"],
                                        created_at=job["created_at"].timestamp())
    return StageResult({"artifact_id": artifact_id})


EXPORT_TYPES = {"md": "text/markdown; charset=utf-8", "pdf": "application/pdf",
                "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}


def export(ctx, claim):
    """Render a finished study artifact into object storage; the API serves it by reference."""
    from vam.study_exports import export_study
    job, token = claim["job"], claim["stage"]["attempt_token"]
    params = job["params"] if isinstance(job["params"], dict) else json.loads(job["params"])
    artifact_id, fmt = params["artifact_id"], params["format"]
    try:
        artifact = _study(ctx).artifact(job["video_id"], artifact_id)
        content = export_study(artifact, fmt, ctx.settings.study_max_export_pages)
    except DomainError as exc:
        raise StageError(exc.code, exc.message, transient=False) from exc
    path = ctx.workdir(token) / f"export.{fmt}"
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    ref = ctx.put_artifact(ctx.artifact_key(job, "export", token, f"EviReel-{artifact_id}.{fmt}"), path, EXPORT_TYPES[fmt])
    return StageResult({"export": ref, "format": fmt, "content_type": EXPORT_TYPES[fmt],
                        "filename": f"EviReel-{artifact_id}.{fmt}"},
                       job_result={"status": "succeeded", "result": {"artifact_id": artifact_id, "format": fmt}})


STAGES = {"prepare": prepare, "audio": audio, "asr": asr, "transcript_index": transcript_index, "vision": vision,
          "publish": publish, "project": project, "notes_draft": notes_draft, "notes_publish": notes_publish, "export": export}