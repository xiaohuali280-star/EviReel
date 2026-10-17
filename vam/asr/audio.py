from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import subprocess
import wave

from vam.video import _probe_video_metadata, _require_binary


class ASRError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AudioAsset:
    path: Path
    offset_s: float
    duration_s: float
    sample_rate: int = 16000


def extract_audio(source: Path, destination: Path, settings) -> AudioAsset | None:
    """WAV time zero is the first audio sample; offset maps it to video time zero."""
    try:
        meta = _probe_video_metadata(str(source))
        audio = next((s for s in meta['streams'] if s.get('codec_type') == 'audio'), None)
        if audio is None:
            return None
        video = next(s for s in meta['streams'] if s.get('codec_type') == 'video')
        video_start = float(video.get('start_time') or 0)
        audio_start = float(audio.get('start_time') or video_start)
        offset = audio_start - video_start
        if not math.isfinite(offset) or abs(offset) > settings.max_video_duration_s:
            raise ASRError('audio_timeline', 'Audio/video timestamp offset is invalid')
        duration = meta['duration'] - min(offset, 0)
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            _require_binary('ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-protocol_whitelist', 'file,pipe', '-i', str(source), '-map', '0:a:0', '-vn',
            '-af', 'asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0',
            '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-t', str(duration), '-y', str(destination),
        ], capture_output=True, check=True, timeout=settings.media_timeout_s)
        with wave.open(str(destination), 'rb') as wav:
            if wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise ASRError('audio_format', 'Extracted audio is not 16 kHz mono PCM16')
            actual = wav.getnframes() / wav.getframerate()
        return AudioAsset(destination, offset, actual)
    except ASRError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise ASRError('audio_timeout', 'Audio probing or extraction timed out') from exc
    except (OSError, RuntimeError, ValueError, StopIteration, wave.Error) as exc:
        raise ASRError('audio_extract_failed', 'Cannot probe or extract audio; check FFmpeg/FFprobe and the source') from exc
