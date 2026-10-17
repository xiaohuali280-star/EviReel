from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile

from vam.asr.audio import extract_audio
from vam.asr.chunker import CHUNK_RULE_VERSION, make_chunks, normalize_segments
from vam.memory.transcript import embedding_fingerprint
from vam.runtime import run_sync
from vam.vision.backends import validate_embeddings


def checksum(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


async def index_transcript(*, source, source_sha256, store, settings, transcriber, backend, progress, work_root):
    cfg = settings
    if not cfg.asr_enabled:
        return {'status': 'skipped', 'reason': 'disabled', **store.state()}
    progress('asr_audio', 0, 1)
    Path(work_root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='asr-', dir=work_root) as temporary:
        audio = await run_sync(extract_audio, source, Path(temporary) / 'audio.wav', cfg)
        if audio is None:
            return {'status': 'skipped', 'reason': 'no_audio', **store.state()}
        asr_identity = canonical(await run_sync(transcriber.fingerprint))
        chunk_config = canonical({'version': CHUNK_RULE_VERSION, 'max_chars': cfg.transcript_chunk_max_chars,
                                  'max_duration_s': cfg.transcript_chunk_max_duration_s, 'pause_s': cfg.transcript_chunk_pause_s,
                                  'audio_mapping_version': 1})
        transcription_key = hashlib.sha256(canonical([source_sha256, asr_identity, chunk_config]).encode()).hexdigest()
        identity = embedding_fingerprint(backend)
        version = hashlib.sha256(canonical([transcription_key, identity]).encode()).hexdigest()
        existing = await run_sync(store.version, version)
        if existing and existing['status'] in {'search_ready', 'empty'}:
            await run_sync(store.publish, version, empty=existing['status'] == 'empty')
            return {'status': 'empty' if existing['status'] == 'empty' else 'ready', 'reused': True, **store.state()}
        cached = await run_sync(store.cached_text, transcription_key)
        if cached is not None:
            segments, chunks, language, metadata = cached
        else:
            progress('asr_transcribe', 0, 1)
            result = await run_sync(transcriber.transcribe, audio.path)
            # The source's actual duration bounds subtitles, independently of config limits.
            from vam.video import _probe_video_metadata
            duration = (await run_sync(_probe_video_metadata, str(source)))['duration']
            segments = normalize_segments(result.segments, offset_s=audio.offset_s, duration_s=duration,
                                          language=result.language, max_segments=cfg.asr_max_segments, max_chars=cfg.asr_max_text_chars)
            chunks = make_chunks(segments, max_chars=cfg.transcript_chunk_max_chars,
                                 max_duration_s=cfg.transcript_chunk_max_duration_s, pause_s=cfg.transcript_chunk_pause_s)
            language = result.language
            metadata = {'audio_offset_s': audio.offset_s, 'audio_duration_s': audio.duration_s, 'sample_rate': audio.sample_rate,
                        'clipped_segments': sum(s.time_clipped for s in segments),
                        'timing_precision': 'segment', 'long_segment_chunks': sum(c.end_t-c.start_t > cfg.transcript_chunk_max_duration_s for c in chunks)}
        progress('asr_chunk', len(chunks), len(chunks))
        await run_sync(store.stage, version=version, transcription_key=transcription_key, source_sha256=source_sha256,
                       asr_fingerprint=asr_identity, chunk_config=chunk_config, embedding_identity=identity,
                       segments=segments, chunks=chunks, language=language, metadata=metadata)
        if not chunks:
            await run_sync(store.publish, version, empty=True)
            return {'status': 'empty', 'reason': 'empty_transcription', **store.state()}
        try:
            vectors, dim = [], None
            for pos in range(0, len(chunks), 32):
                batch = chunks[pos:pos+32]
                progress('transcript_embed', pos, len(chunks))
                batch_vectors, batch_dim = await run_sync(backend.embed_texts_batch, [c.text for c in batch],
                                                          instruction='search_document', batch_size=32)
                validate_embeddings(batch_vectors, expected_count=len(batch), expected_dim=dim or batch_dim)
                if not isinstance(batch_dim, int) or batch_dim != len(batch_vectors[0]):
                    raise RuntimeError('transcript embedding dimension mismatch')
                if any(math.sqrt(sum(x*x for x in vector)) <= 0 for vector in batch_vectors):
                    raise RuntimeError('transcript embeddings must be nonzero')
                vectors.extend(batch_vectors)
                dim = batch_dim
            await run_sync(store.publish, version, vectors=vectors, dim=dim)
        except Exception:
            await run_sync(store.publish, version)
            return {'status': 'partial', 'error_code': 'transcript_embedding_failed',
                    'error': 'Transcript text is available; semantic embedding failed. Retry the transcript job.',
                    'text_reused': cached is not None, **store.state()}
        progress('transcript_embed', len(chunks), len(chunks))
        return {'status': 'ready', 'text_reused': cached is not None, **store.state()}
