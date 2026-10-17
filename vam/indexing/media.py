from __future__ import annotations

import math
import subprocess
from pathlib import Path

from vam.video import _probe_video_metadata, _require_binary
from vam.video_registry import DomainError

ALLOWED_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}

def inspect_upload(path, settings):
    with open(path, "rb") as handle:
        header = handle.read(32)
    is_container = (header[:4] == b"\x1a\x45\xdf\xa3" or
                    (header[:4] == b"RIFF" and header[8:12] == b"AVI ") or
                    header[4:8] in {b"ftyp", b"moov", b"mdat", b"wide", b"free"})
    if not is_container:
        raise DomainError(415, "invalid_media", "File header is not a supported video container")
    try:
        meta = _probe_video_metadata(str(path))
    except FileNotFoundError as exc:
        raise DomainError(503, "ffprobe_missing", "FFprobe is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise DomainError(422, "probe_timeout", "Video probing timed out") from exc
    except RuntimeError as exc:
        if "required but" in str(exc):
            raise DomainError(503, "ffprobe_missing", "FFprobe is unavailable") from exc
        raise DomainError(415, "invalid_media", "File is not a readable video") from exc
    except (ValueError, OSError) as exc:
        raise DomainError(415, "invalid_media", "File is not a readable video") from exc
    video = next((s for s in meta["streams"] if s.get("codec_type") == "video"), None)
    if not video or not video.get("width") or not video.get("height"):
        raise DomainError(415, "no_video_track", "Media must contain a video track")
    duration = meta["duration"]
    if not math.isfinite(duration) or duration <= 0:
        raise DomainError(422, "invalid_duration", "Video duration must be finite and positive")
    if duration > settings.max_video_duration_s:
        raise DomainError(413, "duration_limit", "Video exceeds the configured duration limit")
    return meta

def prepare_playback(source: Path, directory: Path, settings):
    meta = inspect_upload(source, settings)
    video = next(s for s in meta["streams"] if s.get("codec_type") == "video")
    audio = [s for s in meta["streams"] if s.get("codec_type") == "audio"]
    if source.suffix.lower() in {".mp4", ".m4v"} and video.get("codec_name") == "h264" and all(s.get("codec_name") == "aac" for s in audio):
        return source
    destination = directory / "playback.mp4"
    temporary = directory / "playback.pending.mp4"
    try:
        subprocess.run([_require_binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-protocol_whitelist", "file,pipe", "-i", str(source),
                        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                        "-c:a", "aac", "-movflags", "+faststart", "-y", str(temporary)],
                       capture_output=True, check=True, timeout=settings.media_timeout_s)
        converted = inspect_upload(temporary, settings)
        if abs(converted["duration"] - meta["duration"]) > max(0.25, 2 / meta["src_fps"]):
            raise RuntimeError("playback conversion changed the video timeline")
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
