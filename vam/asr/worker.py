"""Isolated optional model process, terminated by the caller on timeout."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import wave


def resolve_model(cfg):
    from faster_whisper.utils import download_model
    path = Path(cfg['model'])
    if not path.is_dir():
        path = Path(download_model(cfg['model'], cache_dir=cfg['download_root'],
                                   local_files_only=cfg['local_files_only'], revision=cfg['revision']))
    if not (path / 'tokenizer.json').is_file():
        raise RuntimeError('ASR model must include tokenizer.json')
    return str(path)


def run(payload):
    try:
        from faster_whisper import WhisperModel
    except (ImportError, OSError):
        return {'error_code': 'asr_dependency_missing', 'message': 'Install the optional asr extra: pip install -e ".[asr]"'}
    cfg = payload['config']
    model_args = {k: cfg[k] for k in ('device', 'compute_type', 'cpu_threads', 'download_root', 'local_files_only', 'revision')}
    try:
        # Prevent tokenizer fallback from contacting the network for local-only models.
        model = WhisperModel(resolve_model(cfg), num_workers=1, **model_args)
    except Exception:
        return {'error_code': 'asr_model_unavailable', 'message': 'Cannot load configured ASR model; check local model/cache, revision, device and compute type'}
    try:
        import numpy as np
        # Audio has already been decoded by our bounded FFmpeg extraction. Read
        # that exact PCM timeline rather than decoding it again through PyAV.
        with wave.open(payload['audio_path'], 'rb') as wav:
            if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) != (1, 16000, 2):
                return {'error_code': 'audio_format', 'message': 'ASR worker requires 16 kHz mono PCM16'}
            if wav.getnframes() > 16000*cfg['max_audio_duration_s']:
                return {'error_code': 'audio_limit', 'message': 'Extracted audio exceeds the configured duration limit'}
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32) / 32768.
        if not len(audio):
            return {'segments': [], 'language': cfg['language']}
        iterator, info = model.transcribe(audio, language=cfg['language'], beam_size=cfg['beam_size'],
                                          vad_filter=cfg['vad_filter'], word_timestamps=False, temperature=0)
        segments, chars = [], 0
        for segment in iterator:
            chars += len(segment.text)
            if len(segments) >= cfg['max_segments'] or chars > cfg['max_chars']:
                return {'error_code': 'transcript_limit', 'message': 'ASR output exceeds configured limits'}
            segments.append({'start': segment.start, 'end': segment.end, 'text': segment.text,
                             'avg_logprob': segment.avg_logprob, 'no_speech_prob': segment.no_speech_prob,
                             'compression_ratio': segment.compression_ratio, 'temperature': segment.temperature})
        return {'segments': segments, 'language': info.language}
    except Exception:
        return {'error_code': 'asr_failed', 'message': 'Configured ASR model failed during transcription'}


if __name__ == '__main__':
    try:
        result = run(json.load(sys.stdin))
    except Exception:
        result = {'error_code': 'asr_worker_failed', 'message': 'Invalid ASR worker input or internal failure'}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    raise SystemExit(1 if 'error_code' in result else 0)
