from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import struct
import time

from vam.asr.chunker import Segment, Chunk
from vam.video_registry import DomainError
from vam.vision.backends import validate_embeddings


def embedding_fingerprint(backend):
    factory = getattr(backend, 'fingerprint', None)
    value = factory() if factory else {'backend': getattr(backend, 'name', 'unknown'), 'encoding_version': 1}
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class TranscriptStore:
    """Own tables in a per-video FrameStore SQLite file; never rewrites visual tables.

    The registry pins this file independently of later visual generations. Text and
    search heads switch transactionally here, so a failed embedding rebuild can
    expose new readable text while retaining the last usable semantic index.
    """
    def __init__(self, path, video_id):
        self.path, self.video_id = Path(path), video_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript((Path(__file__).parent / 'migrations/001_transcripts.sql').read_text(encoding='utf-8'))
            if conn.execute('SELECT version FROM transcript_schema WHERE singleton=1').fetchone()[0] != 1:
                raise RuntimeError('unsupported transcript schema')
            owners = conn.execute('SELECT DISTINCT video_id FROM transcript_versions').fetchall()
            if any(row[0] != video_id for row in owners):
                raise DomainError(409, 'transcript_owner_mismatch', 'Transcript database belongs to another video')

    @contextmanager
    def connect(self, *, write=False):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                conn.execute('BEGIN IMMEDIATE')
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def version(self, version):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM transcript_versions WHERE video_id=? AND version=?', (self.video_id, version)).fetchone()
        return dict(row) if row else None

    def state(self):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM transcript_heads WHERE video_id=?', (self.video_id,)).fetchone()
        text = self.version(row['text_version']) if row and row['text_version'] else None
        search = self.version(row['search_version']) if row and row['search_version'] else None
        return {'text_version': text['version'] if text else None,
                'search_version': search['version'] if search else None,
                'readable': bool(text and text['status'] != 'empty'), 'searchable': bool(search),
                'text_status': text['status'] if text else 'not_ready',
                'language': text['language'] if text else None,
                'search_uses_older_text': bool(text and search and text['version'] != search['version'])}

    def cached_text(self, transcription_key):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM transcript_versions WHERE video_id=? AND transcription_key=? ORDER BY created_at DESC LIMIT 1', (self.video_id, transcription_key)).fetchone()
            if not row:
                return None
            segs = [Segment(r['segment_id'], r['start_t'], r['end_t'], r['text'], r['language'], json.loads(r['quality']),
                            r['raw_text'], r['raw_start_t'], r['raw_end_t'], bool(r['time_clipped']))
                    for r in conn.execute('SELECT * FROM transcript_segments WHERE version=? ORDER BY start_t,end_t,segment_id', (row['version'],))]
            chunks = [Chunk(r['doc_id'], r['start_t'], r['end_t'], r['text'], json.loads(r['segment_ids']))
                      for r in conn.execute('SELECT * FROM transcript_chunks WHERE version=? ORDER BY doc_id', (row['version'],))]
        return segs, chunks, row['language'], json.loads(row['metadata'])

    def stage(self, *, version, transcription_key, source_sha256, asr_fingerprint, chunk_config,
              embedding_identity, segments, chunks, language, metadata):
        with self.connect(write=True) as conn:
            # Repeated jobs reuse one deterministic version and never duplicate rows.
            if conn.execute('SELECT 1 FROM transcript_versions WHERE version=?', (version,)).fetchone():
                return
            conn.execute('INSERT INTO transcript_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         (version, self.video_id, transcription_key, source_sha256, asr_fingerprint,
                          chunk_config, embedding_identity, None, language, 'staged', json.dumps(metadata), time.time()))
            conn.executemany('INSERT INTO transcript_segments VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                             [(version, s.segment_id, s.start_t, s.end_t, s.text, s.language, json.dumps(s.quality), s.raw_text,
                               s.raw_start_t, s.raw_end_t, int(s.time_clipped)) for s in segments])
            conn.executemany('INSERT INTO transcript_chunks VALUES(?,?,?,?,?,?,NULL)',
                             [(version, c.doc_id, c.start_t, c.end_t, c.text, json.dumps(c.segment_ids)) for c in chunks])

    def publish(self, version, *, vectors=None, dim=None, empty=False):
        with self.connect(write=True) as conn:
            current = conn.execute('SELECT status FROM transcript_versions WHERE video_id=? AND version=?', (self.video_id, version)).fetchone()
            if current is None:
                raise RuntimeError('cannot publish missing transcript version')
            docs = conn.execute('SELECT doc_id FROM transcript_chunks WHERE version=? ORDER BY doc_id', (version,)).fetchall()
            if vectors is not None:
                validate_embeddings(vectors, expected_count=len(docs), expected_dim=dim)
                conn.executemany('UPDATE transcript_chunks SET embedding=? WHERE version=? AND doc_id=?',
                                 [(struct.pack(f'<{dim}f', *vector), version, doc['doc_id']) for doc, vector in zip(docs, vectors)])
            status = 'empty' if empty else ('search_ready' if vectors is not None or current['status'] == 'search_ready' else 'text_ready')
            conn.execute('UPDATE transcript_versions SET status=?,dim=COALESCE(?,dim) WHERE version=?', (status, dim, version))
            conn.execute('INSERT OR IGNORE INTO transcript_heads(video_id) VALUES(?)', (self.video_id,))
            conn.execute('UPDATE transcript_heads SET text_version=? WHERE video_id=?', (version, self.video_id))
            if status == 'search_ready' or empty:
                conn.execute('UPDATE transcript_heads SET search_version=? WHERE video_id=?', (None if empty else version, self.video_id))

    def list(self, *, kind='chunks', offset=0, limit=50, min_time=0., max_time=None):
        state = self.state()
        version = state['text_version']
        if version is None:
            raise DomainError(409, 'transcript_not_ready', 'No readable transcript version is available')
        table = 'transcript_chunks' if kind == 'chunks' else 'transcript_segments'
        where = 'version=? AND end_t>=? AND start_t<=?'
        args = (version, min_time, max_time if max_time is not None else float('inf'))
        with self.connect() as conn:
            total = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}', args).fetchone()[0]
            rows = conn.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY start_t,end_t LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        return {'video_id': self.video_id, **state, 'kind': kind, 'offset': offset, 'limit': limit, 'total': total,
                'items': [self.public_item(dict(row)) for row in rows]}

    def public_item(self, row):
        row.pop('embedding', None)
        row['video_id'] = self.video_id
        row['transcript_version'] = row.pop('version')
        row['timing_precision'] = 'segment'
        if 'time_clipped' in row:
            row['time_clipped'] = bool(row['time_clipped'])
        for key in ('segment_ids', 'quality'):
            if key in row:
                row[key] = json.loads(row[key])
        return row

    def search(self, query_vector, *, fingerprint, dim, top_k=5, min_time=0., max_time=None, version_id=None):
        state = self.state()
        if version_id is not None:
            state['search_version'] = version_id
            state['search_uses_older_text'] = state['text_version'] != version_id
        version = self.version(state['search_version']) if state['search_version'] else None
        if not version:
            raise DomainError(409, 'transcript_search_not_ready', 'Transcript text may be readable, but semantic search is not ready')
        if version['embedding_fingerprint'] != fingerprint or version['dim'] != dim:
            raise DomainError(409, 'incompatible_transcript_model', 'Transcript embedding model changed; rebuild its semantic index')
        validate_embeddings([query_vector], expected_count=1, expected_dim=dim)
        norm = math.sqrt(sum(x*x for x in query_vector))
        if norm <= 0:
            raise DomainError(409, 'invalid_query_embedding', 'Query embedding must have a nonzero norm')
        hits = []
        with self.connect() as conn:
            for raw in conn.execute('SELECT * FROM transcript_chunks WHERE version=? AND end_t>=? AND start_t<=?',
                                    (version['version'], min_time, max_time if max_time is not None else float('inf'))):
                row = dict(raw)
                if row['embedding'] is None or len(row['embedding']) != 4*dim:
                    raise DomainError(409, 'corrupt_transcript_index', 'Transcript vector is missing or malformed; rebuild the index')
                vector = struct.unpack(f'<{dim}f', row['embedding'])
                length = math.sqrt(sum(x*x for x in vector))
                if length <= 0 or not math.isfinite(length):
                    raise DomainError(409, 'corrupt_transcript_index', 'Transcript vector is invalid; rebuild the index')
                row['score'] = sum(a*b for a, b in zip(vector, query_vector)) / (norm*length)
                row['quality_warnings'] = ['transcript_time_clipped'] if json.loads(version['metadata']).get('clipped_segments') else []
                for sid in json.loads(row['segment_ids']):
                    segment = conn.execute('SELECT quality FROM transcript_segments WHERE version=? AND segment_id=?', (version['version'], sid)).fetchone()
                    quality = json.loads(segment['quality']) if segment else {}
                    if quality.get('no_speech_prob', 0) > .6 or quality.get('avg_logprob', 0) < -1:
                        row['quality_warnings'].append('possible_asr_error')
                        break
                hits.append(self.public_item(row))
        hits.sort(key=lambda h: (-h['score'], h['start_t'], h['doc_id']))
        return {'video_id': self.video_id, **state, 'results': hits[:top_k]}
