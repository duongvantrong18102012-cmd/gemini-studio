"""Gemini API service: streaming chat, tool loops, models, tokens, images."""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from ..animations import format_duration
from ..config import Settings, get_api_key, system_prompt
from ..tools.registry import ToolContext, ToolError, ToolRegistry
from .models import ModelInfo, fallback_models, load_cached_models, save_cached_models, sort_models
from .sessions import Usage

MODEL_CACHE_TTL = 3600
RETRYABLE_CODES = {429, 500, 502, 503, 504}
MAX_TOOL_STEPS = 16


class GeminiServiceError(RuntimeError):
    """A user-facing failure from the Gemini API."""


@dataclass
class StreamEvent:
    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _api_error_code(error: BaseException) -> int:
    for attribute in ("code", "status_code"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    text = str(error)
    for code in sorted(RETRYABLE_CODES | {400, 401, 403, 404}):
        if f" {code}" in text[:300] or f"{code} " in text[:300]:
            return code
    return 400


def friendly_error(error: BaseException) -> str:
    name = type(error).__name__
    raw = str(error)
    code = _api_error_code(error)
    lowered = raw.lower()
    if "api key" in lowered or "unauthenticated" in lowered or code in (401, 403):
        if "not valid" in lowered or "invalid" in lowered or "api key" in lowered:
            return (
                "The Gemini API key was rejected. Set GEMINI_API_KEY in "
                "~/.config/gemini-tui/.env (get one at https://aistudio.google.com/apikey)."
            )
        return f"Permission denied by the Gemini API (HTTP {code}). Check key restrictions and billing."
    if code == 404 or "not found" in lowered:
        return "That model is not available for this API key. Open the Models view and pick another one."
    if code == 429 or "quota" in lowered or "rate limit" in lowered:
        return "Rate limit or quota exhausted. Wait a moment, switch model, or check your AI Studio quota."
    if code >= 500:
        return f"Gemini is having trouble right now (HTTP {code}). Try again in a few seconds."
    if "safety" in lowered or "blocked" in lowered:
        return "The request was blocked by safety filters. Rephrase the prompt or adjust safety settings."
    if "unsupported" in lowered and "thinking" in lowered:
        return "This model does not support the current thinking configuration. Lower /think in the composer."
    if "function" in lowered and "declaration" in lowered:
        return f"A tool declaration was rejected: {raw[:400]}"
    return f"{name}: {raw[:600]}"


class GeminiService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None
        self._model_cache: list[ModelInfo] = []
        self._model_cache_at = 0.0

    # ── client ────────────────────────────────────────────────────
    @property
    def has_key(self) -> bool:
        return bool(get_api_key())

    def client(self) -> Any:
        if self._client is None:
            api_key = get_api_key()
            if not api_key:
                raise GeminiServiceError(
                    "No Gemini API key found. Create ~/.config/gemini-tui/.env with "
                    "GEMINI_API_KEY=... (free key: https://aistudio.google.com/apikey) "
                    "or set the environment variable."
                )
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=600_000),
            )
        return self._client

    def reset_client(self) -> None:
        self._client = None

    # ── models ────────────────────────────────────────────────────
    async def list_models(self, force: bool = False) -> list[ModelInfo]:
        if not force and self._model_cache and time.time() - self._model_cache_at < MODEL_CACHE_TTL:
            return self._model_cache
        if not force:
            cached, fetched_at = load_cached_models()
            if cached and time.time() - fetched_at < MODEL_CACHE_TTL:
                self._model_cache = sort_models(cached)
                self._model_cache_at = fetched_at
                return self._model_cache
        try:
            pager = await self.client().aio.models.list()
            models: list[ModelInfo] = []
            async for model in pager:
                try:
                    info = ModelInfo.from_api(model)
                except Exception:
                    continue
                if info.id:
                    models.append(info)
            if models:
                self._model_cache = sort_models(models)
                self._model_cache_at = time.time()
                save_cached_models(models)
                return self._model_cache
        except Exception as error:
            cached, _ = load_cached_models()
            if cached:
                self._model_cache = sort_models(cached)
                self._model_cache_at = time.time()
                return self._model_cache
            if isinstance(error, GeminiServiceError):
                raise
            raise GeminiServiceError(friendly_error(error)) from error
        self._model_cache = fallback_models()
        return self._model_cache

    async def count_tokens(self, model: str, contents: list[Any]) -> int:
        try:
            response = await self.client().aio.models.count_tokens(model=model, contents=contents)
            return int(getattr(response, "total_tokens", 0) or 0)
        except Exception:
            return 0

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        response = await self.client().aio.models.embed_content(
            model=model or "gemini-embedding-001", contents=texts
        )
        vectors: list[list[float]] = []
        for embedding in getattr(response, "embeddings", None) or []:
            vectors.append(list(getattr(embedding, "values", []) or []))
        return vectors

    # ── image generation ──────────────────────────────────────────
    async def generate_image(self, prompt: str, model: str, output_dir: Path) -> list[Path]:
        from google.genai import types

        output_dir.mkdir(parents=True, exist_ok=True)
        config = types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
        response = await self.client().aio.models.generate_content(
            model=model, contents=prompt, config=config
        )
        saved: list[Path] = []
        candidates = getattr(response, "candidates", None) or []
        for part in (getattr(candidates[0], "content", None).parts if candidates else []) or []:
            inline = getattr(part, "inline_data", None)
            if not inline or not getattr(inline, "data", None):
                continue
            data = inline.data
            if isinstance(data, str):
                data = base64.b64decode(data)
            mime = getattr(inline, "mime_type", "image/png") or "image/png"
            extension = mime.split("/")[-1].split("+")[0]
            path = output_dir / f"gemini-{int(time.time())}-{len(saved) + 1}.{extension}"
            path.write_bytes(data)
            saved.append(path)
        if not saved:
            text = getattr(response, "text", "") or ""
            raise GeminiServiceError(
                "The model returned no image." + (f" Message: {text[:300]}" if text else "")
            )
        return saved

    # ── chat turn ─────────────────────────────────────────────────
    def _build_config(self, registry: ToolRegistry, system_suffix: str = "") -> Any:
        from google.genai import types

        settings = self.settings
        declarations = registry.declarations()
        return types.GenerateContentConfig(
            system_instruction=system_prompt(settings, system_suffix),
            temperature=max(0.0, min(2.0, float(settings.temperature))),
            max_output_tokens=max(256, int(settings.max_output_tokens)),
            thinking_config=types.ThinkingConfig(
                thinking_level=(settings.thinking_level or "low").lower(),
                include_thoughts=bool(settings.show_thoughts),
            ),
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def run_turn(
        self,
        contents: list[Any],
        model: str,
        registry: ToolRegistry,
        tool_ctx: ToolContext,
        *,
        max_steps: int = MAX_TOOL_STEPS,
        system_suffix: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """Stream one user turn, executing tool calls until the model stops.

        `contents` is mutated in place so a cancelled turn still leaves a valid
        conversation history behind.
        """
        from google.genai import types

        client = self.client()
        started = time.perf_counter()
        usage = Usage()
        steps = 0

        yield StreamEvent("status", "thinking")
        while steps < max_steps:
            steps += 1
            try:
                config = self._build_config(registry, system_suffix)
            except Exception as error:
                yield StreamEvent("error", friendly_error(error))
                return

            collected_parts: list[Any] = []
            thoughts: list[str] = []
            calls: list[Any] = []
            finish_reason = ""
            text_this_step: list[str] = []
            attempt = 0
            succeeded = False

            while not succeeded:
                attempt += 1
                emitted_text = False
                attempt_usage = Usage()
                collected_parts = []
                thoughts = []
                calls = []
                text_this_step = []
                try:
                    stream = await client.aio.models.generate_content_stream(
                        model=model, contents=contents, config=config
                    )
                    async for chunk in stream:
                        chunk_usage = getattr(chunk, "usage_metadata", None)
                        if chunk_usage:
                            attempt_usage.add(chunk_usage)

                        for candidate in getattr(chunk, "candidates", None) or []:
                            finish_reason = str(getattr(candidate, "finish_reason", "") or "")
                            content = getattr(candidate, "content", None)
                            if content is None:
                                continue
                            for part in getattr(content, "parts", None) or []:
                                call = getattr(part, "function_call", None)
                                if call:
                                    collected_parts.append(part)
                                    calls.append(call)
                                    continue
                                part_text = getattr(part, "text", None)
                                if not part_text:
                                    continue
                                if getattr(part, "thought", False):
                                    thoughts.append(part_text)
                                    yield StreamEvent("thought", part_text)
                                else:
                                    text_this_step.append(part_text)
                                    emitted_text = True
                                    yield StreamEvent("text", part_text)
                    succeeded = True
                except asyncio.CancelledError:
                    partial = "".join(text_this_step)
                    if partial:
                        contents.append(
                            types.Content(role="model", parts=[types.Part(text=partial)])
                        )
                    raise
                except Exception as error:
                    code = _api_error_code(error)
                    message = str(error).lower()
                    if code == 400 and "thinking" in message and getattr(config, "thinking_config", None):
                        config = config.model_copy(update={"thinking_config": None})
                        yield StreamEvent("notice", "Model rejected thinking config; retrying without it.")
                        continue
                    if code == 400 and "tool" in message and getattr(config, "tools", None):
                        config = config.model_copy(update={"tools": None})
                        yield StreamEvent("notice", "Model rejected tool declarations; retrying without tools.")
                        continue
                    if code in RETRYABLE_CODES and attempt <= 3 and not emitted_text and not text_this_step:
                        delay = min(8.0, 1.2 * (2 ** (attempt - 1)))
                        yield StreamEvent(
                            "retry",
                            f"HTTP {code} · retrying in {delay:.1f}s ({attempt}/3)",
                            {"code": code, "attempt": attempt},
                        )
                        await asyncio.sleep(delay)
                        continue
                    yield StreamEvent("error", friendly_error(error))
                    return

            for key in ("prompt", "output", "thoughts", "total", "cached", "calls", "latency"):
                setattr(usage, key, getattr(usage, key) + getattr(attempt_usage, key))
            yield StreamEvent(
                "usage",
                data={
                    "prompt": attempt_usage.prompt,
                    "output": attempt_usage.output,
                    "thoughts": attempt_usage.thoughts,
                    "total": attempt_usage.total,
                    "cached": attempt_usage.cached,
                    "turn_total": usage.total,
                },
            )

            model_parts: list[Any] = []
            text_blob = "".join(text_this_step)
            if text_blob:
                model_parts.append(types.Part(text=text_blob))
            model_parts.extend(collected_parts)
            if model_parts:
                contents.append(types.Content(role="model", parts=model_parts))
            if thoughts:
                yield StreamEvent("thoughts_done", "\n".join(thoughts))

            if not calls:
                if "MAX_TOKENS" in finish_reason.upper():
                    yield StreamEvent(
                        "notice",
                        "Output truncated at max_output_tokens. Raise the limit or ask for less.",
                    )
                yield StreamEvent(
                    "done",
                    data={
                        "finish_reason": finish_reason,
                        "usage": usage.to_dict(),
                        "steps": steps,
                        "elapsed": time.perf_counter() - started,
                    },
                )
                return

            response_parts: list[Any] = []
            for call in calls:
                call_id = getattr(call, "id", "") or f"{call.name}-{steps}"
                args = dict(getattr(call, "args", None) or {})
                yield StreamEvent(
                    "tool_start",
                    data={
                        "id": call_id,
                        "name": call.name,
                        "args": args,
                        "dangerous": call.name in registry.dangerous_names,
                    },
                )
                tool_started = time.perf_counter()
                try:
                    output = await registry.call(call.name, args, tool_ctx)
                    ok = True
                except ToolError as error:
                    output = f"ERROR: {error}"
                    ok = False
                except asyncio.CancelledError:
                    if model_parts and contents and getattr(contents[-1], "role", "") == "model":
                        contents.pop()
                    raise
                except Exception as error:
                    output = f"ERROR: {type(error).__name__}: {error}"
                    ok = False
                elapsed = time.perf_counter() - tool_started
                yield StreamEvent(
                    "tool_end",
                    data={
                        "id": call_id,
                        "name": call.name,
                        "ok": ok,
                        "output": output,
                        "elapsed": elapsed,
                    },
                )
                response_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=call.name,
                            response={"result": output[:200_000]},
                        )
                    )
                )
            if response_parts:
                contents.append(types.Content(role="user", parts=response_parts))
            yield StreamEvent("status", "thinking")

        yield StreamEvent(
            "error",
            f"Stopped after {max_steps} tool rounds without a final answer. "
            "Ask me to continue, or narrow the task.",
        )


def _usage_dict(meta: Any) -> dict[str, int]:
    return {
        "prompt": int(getattr(meta, "prompt_token_count", 0) or 0),
        "output": int(getattr(meta, "candidates_token_count", 0) or 0),
        "thoughts": int(getattr(meta, "thoughts_token_count", 0) or 0),
        "total": int(getattr(meta, "total_token_count", 0) or 0),
        "cached": int(getattr(meta, "cached_content_token_count", 0) or 0),
    }


def describe_usage(data: dict[str, Any]) -> str:
    total = int(data.get("total") or 0)
    return (
        f"{total:,} tok · {int(data.get('prompt') or 0):,} in / "
        f"{int(data.get('output') or 0):,} out · {format_duration(data.get('elapsed'))}"
    )
