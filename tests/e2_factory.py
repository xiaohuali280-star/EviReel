"""TEST DOUBLE for model adapters inside E2 worker processes (PIPELINE_FACTORY=tests.e2_factory:build).

Media, database, object storage, broker and worker processes are all real in the E2 tests; only the
paid/heavy models are replaced: image/text embeddings by a colour/keyword vector, ASR by fixed
segments. Calls are counted in files (E2_COUNTER_DIR) so the test process can observe what a
separate worker process actually recomputed. Failure modes are switched by environment:
  E2_ASR=ok|transient|permanent      E2_EMBED_TEXT=ok|fail|fail_after:<n>     E2_VISION=ok|fail
"""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import numpy as np
from PIL import Image

from vam.asr.audio import ASRError
from vam.asr.transcriber import Transcription

SEGMENTS = [
    {"start": 0.10, "end": 0.60, "text": "红色 red slide one."},
    {"start": 0.70, "end": 1.20, "text": "blue vector two."},
    {"start": 1.30, "end": 1.80, "text": "红色 red three."},
    {"start": 1.90, "end": 2.40, "text": "blue four ends."},
    {"start": 2.50, "end": 2.90, "text": "red five final."},
]


def count(name) -> int:
    directory = os.environ.get("E2_COUNTER_DIR")
    if not directory:
        return 0
    path = Path(directory) / name
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
    return len(path.read_text(encoding="utf-8").splitlines())


def calls(directory, name) -> int:
    path = Path(directory) / name
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


class Backend:
    name = "e2-test-color-v1"

    def fingerprint(self):
        return {"backend": self.name, "encoding_version": 1}

    def embed_image_base64(self, value, *, instruction):
        if os.environ.get("E2_VISION") == "fail":
            raise RuntimeError("e2 test: image embedding outage")
        pixels = np.asarray(Image.open(io.BytesIO(base64.b64decode(value.split(",", 1)[1]))).convert("RGB"))
        vector = pixels.mean(axis=(0, 1)).astype(float) + 1e-3
        return (vector / float(np.linalg.norm(vector))).tolist(), 3

    def embed_images_base64_batch(self, images, *, instruction, batch_size):
        return [self.embed_image_base64(v, instruction=instruction)[0] for v in images], 3

    def embed_texts_batch(self, texts, *, instruction, batch_size):
        if instruction == "search_document":
            n = count("embed_texts")
            mode = os.environ.get("E2_EMBED_TEXT", "ok")
            if mode == "fail" or (mode.startswith("fail_after:") and n > int(mode.split(":")[1])):
                raise ConnectionError("e2 test: text embedding provider unavailable")
        return [([0., 0., 1.] if "blue" in t.lower() else [1., 0., 0.]) for t in texts], 3

    def embed_text(self, text, *, instruction):
        return self.embed_texts_batch([text], instruction=instruction, batch_size=1)[0][0], 3


class Transcriber:
    def fingerprint(self):
        return {"provider": "e2-test", "model": "fixed-segments-v1", "segments": len(SEGMENTS)}

    def transcribe(self, audio_path):
        count("transcribe")
        mode = os.environ.get("E2_ASR", "ok")
        if mode == "transient":
            raise ASRError("asr_timeout", "e2 test: ASR timed out")
        if mode == "permanent":
            raise ASRError("asr_failed", "e2 test: ASR rejected the audio")
        return Transcription([dict(s) for s in SEGMENTS], "zh")


class StudyModel:
    """Notes model double: one chapter per batch covering every source; E2_NOTES=fail_first fails the
    first call (a retryable invalid output)."""
    identity = "e2-test-notes-v1"

    async def __call__(self, messages):
        import json
        n = count("notes")
        if os.environ.get("E2_NOTES") == "fail_first" and n == 1:
            return "not json"
        sources = json.loads(messages[-1]["content"])
        return json.dumps({"chapters": [{"title": f"章节 {n}", "points": ["要点：" + sources[0]["text"][:40]],
                                         "source_ids": [s["source_id"] for s in sources]}]})


def build(settings):
    return Backend(), Transcriber()


def study_model(settings):
    return StudyModel()
