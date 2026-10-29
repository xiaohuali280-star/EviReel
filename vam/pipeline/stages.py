"""Stage bodies. Each returns StageResult(output, skip, job_result); side effects are either
immutable (attempt-keyed objects / index generations) or content-addressed (transcript versions),
so a re-run or a stale attempt cannot corrupt a published result. Publication of anything the
user sees happens only in `publish`, after the ledger says which products won."""
from __future__ import annotations

import asyncio
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
STAGE_CODE_VERSION = {"asr": 1, "embed": 1}


@dataclass
class StageResult:
    output: dict
    skip: dict = field(default_factory=dict)
    job_result: dict | None = None


def _chunk_config(cfg):
    return canonical({"version": CHUNK_RULE_VERSION, "max_chars": cfg.transcript_chunk_max_chars,
                      "max_duration_s": cfg.transcript_chunk_max_duration_s, "pause_s": cfg.transcript_chunk_pause_s,
                      "audio_mapping_version": 1})


def _outputs(ctx, job_id):
    return {name: row for name, row in ctx.repo.stages(job_id).items()}


# ---------------------------------------------------------------------------------------------
def prepare(ctx, claim):
    """Media/audio preparation: playback file (video jobs) and the 16 kHz mono PCM audio artifact."""
    job, token = claim["job"], claim["stage"]["attempt_token"]
    video_id, cfg = job["video_id"], ctx.settings
    source = ctx.source(video_id, token)
    try:
        from vam.video import _probe_video_metadata
        duration = _probe_video_metadata(str(source))["duration"]
        if job["kind"] == "video":
            playback = prepare_playback(source, ctx.registry.directory(video_id), cfg)
            if playback == source:  # already browser-playable: point at the registry's own copy
                playback = ctx.registry.asset(video_id, ctx.registry.get(video_id)["source_path"])
            ctx.registry.update_video(video_id, playback_path=playback.name)
    except DomainError as exc:
        raise StageError(exc.code, exc.message, transient=False) from exc
    source_sha = sha256_file(source)
    output = {"source_sha256": source_sha, "duration": duration, "audio": None}
    if not cfg.asr_enabled:
        return StageResult(output | {"audio_reason": "disabled"},
                           skip={"asr": "asr_disabled", "transcript_index": "asr_disabled"})
    try:
        audio = extract_audio(source, ctx.workdir(token) / "audio.wav", cfg)
    except ASRError as exc:
        raise StageError(exc.code, str(exc), transient=exc.code == "audio_timeout") from exc
    if audio is None:
        return StageResult(output | {"audio_reason": "no_audio"},
                           skip={"asr": "no_audio", "transcript_index": "no_audio"})
    ref = ctx.put_artifact(ctx.artifact_key(job, "prepare", token, "audio.wav"), audio.path, "audio/wav")
    output["audio"] = ref | {"offset_s": audio.offset_s, "duration_s": audio.duration_s, "sample_rate": audio.sample_rate}
    return StageResult(output)


# ---------------------------------------------------------------------------------------------
def asr(ctx, claim):
    """Speech recognition with a content-addressed checkpoint (source sha + ASR identity + chunk rules)."""
    job, token = claim["job"], claim["stage"]["attempt_token"]
    prep = _outputs(ctx, job["job_id"])["prepare"]["output"]
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
    audio = ctx.fetch(prep["audio"], work / "audio.wav")
    try:
        result = ctx.transcriber.transcribe(audio)
    except ASRError as exc:
        transient = exc.code in {"asr_timeout", "asr_worker_failed"}
        raise StageError(exc.code, str(exc), transient=transient) from exc
    payload = {"segments": result.segments, "language": result.language, "offset_s": prep["audio"]["offset_s"],
               "audio_duration_s": prep["audio"]["duration_s"], "sample_rate": prep["audio"]["sample_rate"],
               "duration": prep["duration"]}
    ref = ctx.put_json(ctx.artifact_key(job, "asr", token, "segments.json"), payload, work / "segments.json")
    fault("after_asr_upload")
    winner = ctx.repo.save_checkpoint(reuse_key, ref["key"], ref["sha256"], ref["size"], token)
    ref = {"key": winner["object_key"], "sha256": winner["sha256"]}
    return StageResult({"asr": ref, "transcription_key": transcription_key, "asr_identity": asr_identity, "reused": False})


# ---------------------------------------------------------------------------------------------
def transcript_index(ctx, claim):
    """Chunking + text embeddings with per-batch checkpoints, then (content-addressed) local publish."""
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
    ctx.registry.update_video(video_id, transcript_status="running", transcript_error=None)
    existing = store.version(version)
    if existing and existing["status"] in {"search_ready", "empty"}:
        store.publish(version, empty=existing["status"] == "empty")
        return StageResult({"status": "empty" if existing["status"] == "empty" else "ready", "version": version,
                            "reused": True, "batches": {"reused": 0, "computed": 0}})
    store.stage(version=version, transcription_key=transcription_key, source_sha256=prep["source_sha256"],
                asr_fingerprint=asr_out["asr_identity"], chunk_config=_chunk_config(cfg), embedding_identity=identity,
                segments=segments, chunks=chunks, language=payload["language"], metadata=metadata)
    if not chunks:
        store.publish(version, empty=True)
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
        store.publish(version)
        message = "Transcript text is available; semantic embedding failed. Retry the transcript job."
        ctx.registry.update_video(video_id, transcript_status="partial", transcript_error=message)
        return StageResult({"status": "partial", "error_code": "transcript_embedding_failed", "error": message,
                            "version": version, "batches": stats})
    store.publish(version, vectors=vectors, dim=dim)
    ctx.registry.update_video(video_id, transcript_status="ready", transcript_error=None)
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
        if fields:
            self.ctx.registry.update_job(self.job["job_id"], status="running", **fields)

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
    """Outcome of the speech branch (prepare -> asr -> transcript_index): the first real failure wins;
    a skip caused by a failed dependency is that failure, not "no speech"."""
    for name in ("prepare", "asr", "transcript_index"):
        row = stages[name]
        if row["status"] == "failed":
            return {"status": "failed", "error_code": row["error_code"] or f"{name}_failed",
                    "error": row["error"] or "Transcript indexing failed; previous published text/index is retained"}
        if name == "prepare" and row["status"] == "succeeded" and row["output"].get("audio_reason"):
            return {"status": "skipped", "reason": row["output"]["audio_reason"]}
    final = stages["transcript_index"]
    if final["status"] == "succeeded":
        return final["output"]
    return {"status": "skipped", "reason": final["error_code"] or "dependency_not_succeeded"}


def publish(ctx, claim):
    """Join: publish whatever branch succeeded into the read model; failed branches keep the old index."""
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
    if t == "failed":
        ctx.registry.update_video(video_id, transcript_status="failed", transcript_error=transcript.get("error"))
    error = transcript if t in {"failed", "partial"} else (vis if vis and vis.get("status") == "failed" else {})
    result = dict(vis["result"]) if vision_ok else {}
    vision_state = ({"status": vis["status"]} if vision_ok else
                    (vis if vis else {"status": "not_requested"}))
    result.update(video_id=video_id, stages={"vision": vision_state, "transcript": transcript})
    row = ctx.registry.get(video_id)
    if vision_ok:
        row["active_generation"] = vis["generation"]
    result["capabilities"] = ctx.registry.public(row)["capabilities"]
    # Idempotent: a re-run of publish (crash before the ledger commit) writes the same values.
    ctx.project(job)
    ctx.registry.finish(video_id, job_id, status=status, result=result,
                        error_code=error.get("error_code"), error=error.get("error"),
                        vision_generation=vis["generation"] if vision_ok else None,
                        fingerprint=vis["fingerprint"] if vision_ok else None)
    # Deferred cleanup: generations of vision attempts that did not win are never published.
    for attempt in ctx.repo.attempts(job_id):
        if attempt["stage"] == "vision" and not (vision_ok and attempt["attempt_token"] == vis["generation"]):
            ctx.drop_generation(video_id, attempt["attempt_token"])
    if job["kind"] == "video" and t in {"failed", "skipped"}:
        ctx.drop_generation(video_id, job_id)  # a transcript file this job created but never published
    return StageResult({"status": status}, job_result={
        "status": status, "result": result, "error_code": error.get("error_code"), "error": error.get("error")})


STAGES = {"prepare": prepare, "asr": asr, "transcript_index": transcript_index, "vision": vision, "publish": publish}
