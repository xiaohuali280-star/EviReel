from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import json
import logging
import math
import httpx
import asyncio
import os
import time

from vam.config import get_settings

logger = logging.getLogger(__name__)
from vam.vision.image_limits import ensure_image_limit


def _read_timeout_env(primary: str, legacy: str, default: str) -> float:
    raw = os.getenv(primary)
    if raw is None:
        raw = os.getenv(legacy, default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _read_retry_env(primary: str, legacy: str, default: str) -> int:
    raw = os.getenv(primary)
    if raw is None:
        raw = os.getenv(legacy, default)
    try:
        return max(1, int(raw))
    except Exception:
        return int(default)


def build_mm_user_content(
    text: str,
    images: Optional[List[str]] = None,
    videos: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for url in images or []:
        processed_url = ensure_image_limit(url)
        content.append({"type": "image_url", "image_url": {"url": processed_url}})
    for url in videos or []:
        content.append({"type": "video_url", "video_url": {"url": url}})
    return content


class TokenUsage:
    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.reasoning_tokens = 0
        self.request_count = 0

    def add(self, usage: Optional[Dict[str, Any]]) -> None:
        payload = usage or {}
        self.prompt_tokens += int(payload.get("prompt_tokens") or 0)
        self.completion_tokens += int(payload.get("completion_tokens") or 0)
        self.total_tokens += int(payload.get("total_tokens") or 0)
        self.reasoning_tokens += int(
            payload.get("reasoning_tokens")
            or payload.get("reasoningTokens")
            or 0
        )
        self.request_count += 1

    def to_dict(self) -> Dict[str, int]:
        return {
            "request_count": self.request_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }

    def __repr__(self) -> str:
        data = self.to_dict()
        return (
            "TokenUsage("
            f"requests={data['request_count']}, "
            f"prompt={data['prompt_tokens']}, "
            f"completion={data['completion_tokens']}, "
            f"reasoning={data['reasoning_tokens']}, "
            f"total={data['total_tokens']}"
            ")"
        )


class UsageLedger:
    def __init__(self) -> None:
        self.total = TokenUsage()
        self.by_model: Dict[str, TokenUsage] = {}

    def add(self, *, model_id: str, usage: Optional[Dict[str, Any]]) -> None:
        self.total.add(usage)
        key = (model_id or "unknown").strip() or "unknown"
        entry = self.by_model.get(key)
        if entry is None:
            entry = TokenUsage()
            self.by_model[key] = entry
        entry.add(usage)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total.to_dict(),
            "by_model": {k: v.to_dict() for k, v in self.by_model.items()},
        }

    def __repr__(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


global_usage = UsageLedger()


def get_global_usage_snapshot() -> Dict[str, Any]:
    return global_usage.to_dict()


class OpenRouterClient:
    def __init__(self, *, model_id: Optional[str] = None) -> None:
        cfg = get_settings()
        self.model_id = model_id or cfg.model_id

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        text, raw = await self._chat_impl(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            extra_params=extra_params,
        )
        if raw and "usage" in raw:
            model_name = str(raw.get("model") or self.model_id or "unknown")
            global_usage.add(model_id=model_name, usage=raw["usage"])
            total = global_usage.total
            if total.request_count % 10 == 0:
                print(
                    f"[TokenStats] Requests={total.request_count} "
                    f"Prompt={total.prompt_tokens} "
                    f"Completion={total.completion_tokens} "
                    f"Reasoning={total.reasoning_tokens} "
                    f"Total={total.total_tokens}"
                )
        return text, raw

    async def _chat_impl(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        cfg = get_settings()
        if not cfg.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY, SILICONFLOW_API_KEY, or OPENROUTER_API_KEY is missing")

        base_url = (cfg.chat_base_url or "https://api.siliconflow.cn/v1").rstrip("/")
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": cfg.app_name,
        }

        payload: Dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stream:
            payload["stream"] = True
        if extra_params:
            payload.update(extra_params)

        image_count = 0
        video_count = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "image_url":
                        image_count += 1
                    if part.get("type") == "video_url":
                        video_count += 1

        request_timeout_s = _read_timeout_env("DASHSCOPE_REQUEST_TIMEOUT_S", "SILICONFLOW_REQUEST_TIMEOUT_S", "240")
        connect_timeout_s = _read_timeout_env("DASHSCOPE_CONNECT_TIMEOUT_S", "SILICONFLOW_CONNECT_TIMEOUT_S", "60")
        max_retries = _read_retry_env("DASHSCOPE_MAX_RETRIES", "SILICONFLOW_MAX_RETRIES", "10")

        from vam.gateway import estimate_tokens, get_gateway
        text_chars = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                text_chars += len(content)
            elif isinstance(content, list):
                text_chars += sum(len(str(part.get("text") or "")) for part in content if isinstance(part, dict))
        estimate_in = estimate_tokens(text_chars) + 1000 * image_count + 4000 * video_count

        raw: Dict[str, Any]
        timeout = httpx.Timeout(request_timeout_s, connect=connect_timeout_s)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async def attempt():
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code >= 400:
                    # Status only: provider bodies may echo prompts or credentials.
                    logger.warning("llm_http_error status=%s model=%s", resp.status_code, self.model_id)
                resp.raise_for_status()
                body = resp.json()
                usage = body.get("usage") if isinstance(body, dict) and isinstance(body.get("usage"), dict) else None
                return body, ({"input_tokens": int(usage.get("prompt_tokens") or 0),
                               "output_tokens": int(usage.get("completion_tokens") or 0)} if usage else None)

            # One gateway run = admission + ledger + timeout per attempt, shared retry policy (E3).
            raw = await get_gateway().run(operation="chat", model=self.model_id, attempt_fn=attempt,
                                          est_input_tokens=estimate_in, est_output_tokens=int(max_tokens or 1024),
                                          timeout=request_timeout_s, attempts=max_retries)

        text: Optional[str] = None
        try:
            text = ((raw.get("choices") or [{}])[0].get("message") or {}).get("content")
        except Exception:
            text = None
        if text is not None and not isinstance(text, str):
            try:
                text = json.dumps(text, ensure_ascii=False)
            except Exception:
                text = str(text)

        return text, raw


class OpenRouterEmbeddingsClient:
    def __init__(self, *, model_id: Optional[str] = None) -> None:
        cfg = get_settings()
        self.model_id = model_id or cfg.embedding_model_id

    def _headers(self) -> Dict[str, str]:
        cfg = get_settings()
        if not (cfg.embedding_api_key_override or cfg.api_key):
            raise RuntimeError("DASHSCOPE_API_KEY, SILICONFLOW_API_KEY, or OPENROUTER_API_KEY is missing")
        return {
            "Authorization": f"Bearer {(cfg.embedding_api_key_override or cfg.api_key)}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": cfg.app_name,
        }

    def _base_payload(self, *, input_type: Optional[str] = None) -> Dict[str, Any]:
        cfg = get_settings()
        payload: Dict[str, Any] = {
            "model": self.model_id,
            "encoding_format": "float",
        }
        if input_type:
            payload["input_type"] = input_type
        if cfg.embedding_dimensions > 0:
            payload["dimensions"] = cfg.embedding_dimensions
        return payload

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """One embedding request through the ModelGateway (E3): shared limits, ledger, retries, timeouts."""
        from vam.gateway import estimate_tokens, get_gateway
        cfg = get_settings()
        base_url = (cfg.embedding_base_url_override or cfg.chat_base_url or "https://api.siliconflow.cn/v1").rstrip("/")
        url = f"{base_url}/embeddings"
        headers = self._headers()
        items = payload.get("input") or []
        chars = sum(len(item) for item in items if isinstance(item, str))
        images = sum(1 for item in items if not isinstance(item, str))

        def attempt(timeout: float):
            response = httpx.post(url, headers=headers, json=payload,
                                  timeout=httpx.Timeout(min(60.0, timeout), connect=min(10.0, timeout)))
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, dict):
                raise RuntimeError(f"unexpected embedding response: {raw}")
            usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
            tokens = (usage or {}).get("prompt_tokens") or (usage or {}).get("total_tokens")
            return raw, ({"input_tokens": int(tokens), "output_tokens": 0} if tokens else None)

        return get_gateway().run_sync(operation="embedding", model=self.model_id, attempt_fn=attempt,
                                      est_input_tokens=estimate_tokens(chars) + 1000 * images, timeout=60.0, attempts=4)

    def _extract_embeddings(self, raw: Dict[str, Any]) -> Tuple[List[List[float]], int]:
        data = raw.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"unexpected embedding response: {raw}")
        if any(isinstance(item, dict) and "index" in item for item in data):
            if any(not isinstance(item, dict) or type(item.get("index")) is not int for item in data):
                raise RuntimeError("embedding response has invalid indices")
            if sorted(item["index"] for item in data) != list(range(len(data))):
                raise RuntimeError("embedding response has duplicate or missing indices")
            data = sorted(data, key=lambda item: item["index"])
        vectors: List[List[float]] = []
        for item in data:
            emb = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(emb, list):
                raise RuntimeError(f"missing embedding in response item: {item}")
            vectors.append([float(v) for v in emb])
        from vam.vision.backends import validate_embeddings
        validate_embeddings(vectors, expected_count=len(vectors), expected_dim=get_settings().embedding_dimensions or None)
        return vectors, len(vectors[0])

    def generate_text_embeddings(
        self,
        texts: List[str],
        *,
        input_type: Optional[str] = None,
    ) -> Tuple[List[List[float]], int]:
        payload = self._base_payload(input_type=input_type)
        payload["input"] = list(texts)
        vectors, dim = self._extract_embeddings(self._post(payload))
        if len(vectors) != len(payload["input"]):
            raise RuntimeError("embedding response count mismatch")
        return vectors, dim

    def generate_image_embeddings(
        self,
        image_urls: List[str],
        *,
        input_type: Optional[str] = None,
    ) -> Tuple[List[List[float]], int]:
        payload = self._base_payload(input_type=input_type)
        image_urls = [ensure_image_limit(url) for url in image_urls]
        payload["input"] = [
            {
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": url},
                    }
                ]
            }
            for url in image_urls
        ]
        vectors, dim = self._extract_embeddings(self._post(payload))
        if len(vectors) != len(payload["input"]):
            raise RuntimeError("embedding response count mismatch")
        return vectors, dim
