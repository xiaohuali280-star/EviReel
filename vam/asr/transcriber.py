from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Protocol

from vam.asr.audio import ASRError


@dataclass
class Transcription:
    segments: list[dict]
    language: str | None = None


class Transcriber(Protocol):
    def fingerprint(self) -> dict: ...
    def transcribe(self, audio_path: Path) -> Transcription: ...


class MockTranscriber:
    """Explicit injection for tests; never selected by production configuration."""
    def __init__(self, segments=(), language='zh', identity='mock-v1'):
        self.segments, self.language, self.identity = list(segments), language, identity
        self.calls = 0

    def fingerprint(self):
        digest = hashlib.sha256(json.dumps(self.segments, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {'provider': 'mock', 'model': self.identity, 'fixture_hash': digest, 'language': self.language}

    def transcribe(self, audio_path):
        self.calls += 1
        return Transcription(self.segments, self.language)


class FasterWhisperTranscriber:
    def __init__(self, settings):
        self.settings = settings

    def config(self):
        s = self.settings
        return {'model': s.asr_model, 'revision': s.asr_model_revision or None,
                'device': s.asr_device, 'compute_type': s.asr_compute_type,
                'cpu_threads': s.asr_cpu_threads, 'language': s.asr_language or None,
                'beam_size': s.asr_beam_size, 'vad_filter': s.asr_vad_filter,
                'download_root': str(Path(s.asr_model_cache).expanduser().resolve()),
                'local_files_only': s.asr_local_files_only,
                'max_audio_duration_s': 2*s.max_video_duration_s,
                'max_segments': s.asr_max_segments, 'max_chars': s.asr_max_text_chars}

    def fingerprint(self):
        cfg = self.config()
        # Cache paths / permission to download do not change transcription semantics.
        for key in ('download_root', 'local_files_only'):
            cfg.pop(key)
        try:
            package = version('faster-whisper')
        except PackageNotFoundError:
            package = 'not-installed'
        # Locally supplied models can be replaced in place; hash their actual files.
        path = Path(self.settings.asr_model)
        if path.is_dir():
            digest = hashlib.sha256()
            for file in sorted(p for p in path.rglob('*') if p.is_file()):
                digest.update(str(file.relative_to(path)).encode())
                with file.open('rb') as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(block)
            cfg['local_model_sha256'] = digest.hexdigest()
        return {'provider': 'faster-whisper', 'package_version': package, 'config': cfg, 'adapter_version': 1}

    def transcribe(self, audio_path):
        payload = {'audio_path': str(audio_path), 'config': self.config()}
        try:
            process = subprocess.run([sys.executable, '-m', 'vam.asr.worker'],
                                     input=json.dumps(payload), text=True, encoding='utf-8',
                                     capture_output=True, timeout=self.settings.asr_timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise ASRError('asr_timeout', 'Speech transcription exceeded its configured timeout') from exc
        if len(process.stdout.encode('utf-8')) > self.settings.asr_max_text_chars * 10 + 4_000_000:
            raise ASRError('transcript_limit', 'ASR worker response exceeds the size limit')
        try:
            result = json.loads(process.stdout)
        except (ValueError, TypeError) as exc:
            raise ASRError('asr_worker_failed', 'ASR worker exited without a valid result') from exc
        if process.returncode or 'error_code' in result:
            raise ASRError(result.get('error_code', 'asr_failed'), result.get('message', 'Speech transcription failed'))
        return Transcription(result['segments'], result.get('language'))
