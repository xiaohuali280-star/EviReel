from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import re

from vam.asr.audio import ASRError

CHUNK_RULE_VERSION = 1


@dataclass(frozen=True)
class Segment:
    segment_id: str
    start_t: float
    end_t: float
    text: str
    language: str | None = None
    quality: dict = field(default_factory=dict)
    raw_text: str = ''
    raw_start_t: float | None = None
    raw_end_t: float | None = None
    time_clipped: bool = False


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    start_t: float
    end_t: float
    text: str
    segment_ids: list[str]
    timing_precision: str = 'segment'


def normalize_segments(raw, *, offset_s, duration_s, language=None, max_segments=20000, max_chars=2000000):
    normalized = []
    chars = 0
    for i, item in enumerate(raw):
        if i >= max_segments:
            raise ASRError('transcript_limit', 'ASR output exceeds the segment limit')
        try:
            start, end = item['start'], item['end']
            if isinstance(start, bool) or isinstance(end, bool):
                raise ValueError('boolean time')
            start, end = float(start), float(end)
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError('invalid segment interval')
            text = item['text']
            if not isinstance(text, str):
                raise ValueError('text must be a string')
            raw_text = text
            text = ' '.join(text.split())
            chars += len(text)
            if chars > max_chars:
                raise ASRError('transcript_limit', 'ASR output exceeds the text limit')
            quality = {}
            for key in ('avg_logprob', 'no_speech_prob', 'compression_ratio', 'temperature'):
                value = item.get(key)
                if value is not None:
                    if isinstance(value, bool) or not math.isfinite(float(value)):
                        raise ValueError('invalid quality metric')
                    quality[key] = float(value)
            raw_start, raw_end = start, end
            start, end = max(0., start + offset_s), min(duration_s, end + offset_s)
            if text and end > start:
                clipped = start != raw_start + offset_s or end != raw_end + offset_s
                normalized.append(Segment(f's-{i:06d}', start, end, text, item.get('language') or language,
                                          quality, raw_text, raw_start, raw_end, clipped))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ASRError('invalid_transcript', 'ASR returned invalid segment data') from exc
    # Overlap is preserved; never invent word times or silently push segments forward.
    return sorted(normalized, key=lambda s: (s.start_t, s.end_t, s.segment_id))


def make_chunks(segments, *, max_chars=500, max_duration_s=30., pause_s=1.5):
    """Split text at sentence boundaries, retaining the source segment interval/IDs.

    An indivisible long segment can exceed max_duration_s: segment-level timestamps
    cannot truthfully assign a shorter interval to its text fragments.
    """
    chunks, group = [], []

    def flush():
        if group:
            ids = list(dict.fromkeys(s.segment_id for s, _ in group))
            chunks.append(Chunk(f'tc-{len(chunks):06d}', min(s.start_t for s, _ in group),
                                max(s.end_t for s, _ in group), ' '.join(t for _, t in group), ids))
            group.clear()

    for segment in segments:
        sentences = re.split(r'(?<=[。！？!?；;])\s*|(?<=\.)\s+|(?<=\.)$', segment.text)
        for sentence in filter(None, sentences):
            for pos in range(0, len(sentence), max_chars):
                part = sentence[pos:pos + max_chars]
                if group:
                    start = min(s.start_t for s, _ in group)
                    end = max(s.end_t for s, _ in group)
                    length = sum(len(t) for _, t in group) + len(group) + len(part)
                    if length > max_chars or max(end, segment.end_t) - start > max_duration_s or segment.start_t - end > pause_s:
                        flush()
                group.append((segment, part))
                if part.endswith(('。', '！', '？', '.', '!', '?', '；', ';')):
                    flush()
    flush()
    return chunks


def serialize(items):
    return [asdict(item) for item in items]
