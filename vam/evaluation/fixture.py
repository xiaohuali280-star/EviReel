"""Synthetic evaluation corpus: writes published generations (FrameStore + TranscriptStore schema, as the
indexers leave them) from a JSON spec into a DATA_ROOT and points the registry at them.

Spec: {"videos": [{"key", "title", "duration", "segments": [{"start", "end", "text"}],
                   "frames": [{"t", "caption"}], "events": [{"start", "end", "text"}]}]}
Frames carry their caption in a PNG text chunk (vam.evaluation.simulator), so only the simulator can 'see'
them. The corpus is synthetic text written for the evaluation, not a recording.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path


def build(spec_path, service, backend) -> dict:
    """Returns {video key: {"video_id", "generation", "transcript_version"}}."""
    from vam.asr.chunker import Chunk, Segment
    from vam.evaluation.simulator import caption_png
    from vam.memory.transcript import TranscriptStore, embedding_fingerprint
    from vam.retrieval.frame_store import FrameStore, _tensor_to_blob
    import torch
    spec = json.loads(Path(spec_path).read_text(encoding='utf-8'))
    registry = service.registry
    vec = lambda text: torch.tensor(backend._vector(text), dtype=torch.float16)
    out = {}
    for video in spec['videos']:
        video_id, generation = 'v_' + uuid.uuid4().hex, uuid.uuid4().hex
        directory = registry.directory(video_id)
        directory.mkdir(parents=True)
        (directory / 'source.mp4').write_bytes(b'synthetic')
        registry.register(video_id, video['title'], 'source.mp4', 9, float(video['duration']))
        path = registry.asset(video_id, f'memory/{generation}.sqlite3')
        path.parent.mkdir(parents=True, exist_ok=True)
        store = FrameStore(backend=backend, persist_path=str(path))
        frames = sorted(video['frames'], key=lambda f: f['t'])
        frame_ids = [f'f{i:05d}' for i in range(len(frames))]
        with sqlite3.connect(path) as conn:
            conn.executemany('INSERT INTO frames VALUES (?,?,?,?,?,?,?)',
                             [(fid, float(f['t']), None, caption_png(f['caption']), _tensor_to_blob(vec(f['caption'])), 'long', None)
                              for fid, f in zip(frame_ids, frames)])
            rows = []
            for e in video.get('events', []):
                inside = [fid for fid, f in zip(frame_ids, frames) if e['start'] <= f['t'] <= e['end']]
                rows.append((uuid.uuid4().hex, 'long', 'event', float(e['start']), float(e['end']), None, None, e['text'],
                             inside[0] if inside else None, json.dumps(inside), _tensor_to_blob(vec(e['text'])), None))
            conn.executemany('INSERT INTO memory_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows)
            conn.execute('UPDATE store_meta SET backend=?, dim=?, revision=1, model_fingerprint=? WHERE store_key=1',
                         (backend.name, backend.dim, store.model_fingerprint()))
        transcript = TranscriptStore(path, video_id)
        version = uuid.uuid4().hex
        segments = [Segment(f's{i}', float(s['start']), float(s['end']), s['text'], 'zh', {}, s['text'],
                            float(s['start']), float(s['end']), False) for i, s in enumerate(video['segments'])]
        chunks = [Chunk(f'c{i:05d}', s.start_t, s.end_t, s.text, [s.segment_id]) for i, s in enumerate(segments)]
        transcript.stage(version=version, transcription_key='synthetic', source_sha256='0' * 64, asr_fingerprint='synthetic',
                         chunk_config='one-chunk-per-segment', embedding_identity=embedding_fingerprint(backend),
                         segments=segments, chunks=chunks, language='zh', metadata={})
        transcript.publish(version, vectors=[backend._vector(c.text) for c in chunks], dim=backend.dim)
        registry.update_video(video_id, status='succeeded', active_generation=generation, transcript_generation=generation,
                              transcript_status='ready', playback_path='source.mp4')
        out[video['key']] = {'video_id': video_id, 'generation': generation, 'transcript_version': version}
    return out


def check_references(spec_path, questions) -> list[str]:
    """Every reference of a synthetic question must fall inside a fixture item of its source."""
    spec = {v['key']: v for v in json.loads(Path(spec_path).read_text(encoding='utf-8'))['videos']}
    problems = []
    for q in questions:
        video = spec.get(q.video)
        if video is None:
            problems.append(f'{q.id}: unknown video {q.video}')
            continue
        for r in q.references:
            spans = []
            if r.source in ('transcript', 'any'):
                spans += [(s['start'], s['end']) for s in video['segments']]
            if r.source in ('frame', 'any'):
                spans += [(f['t'], f['t']) for f in video['frames']]
            if r.source in ('event', 'any'):
                spans += [(e['start'], e['end']) for e in video.get('events', [])]
            if not any(a <= r.end_t and b >= r.start_t for a, b in spans):
                problems.append(f'{q.id}: reference {r.start_t}-{r.end_t} ({r.source}) covers no fixture item')
    return problems
