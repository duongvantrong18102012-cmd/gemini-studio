"""Model registry, capability metadata and routing.

Nothing here imports Textual or the Gemini SDK at module import asyncio
import time. The
Gemini adapter loads the SDK lazily, and other providers are only registered
when the user has configured them, so an unconfigured provider can never be
selected by accident.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

CAP_TEXT = "text"
CAP_TOOLS = "tools"
CAP_CODE = "code"
CAP_REASONING = "reasoning"
CAP_PLANNING = "planning"
CAP_VISION = "vision"
CAP_LONG_CONTEXT = "long_context"
CAP_FAST = "fast"
CAP_CHEAP = "cheap"

ALL_CAPABILITIES = frozenset(
    {
        CAP_TEXT,
        CAP_TOOLS,
        CAP_CODE,
        CAP_REASONING,
        CAP_PLANNING,
        CAP_VISION,
        CAP_LONG_CONTEXT,
        CAP_FAST,
        CAP_CHEAP,
    }
)


@dataclass(frozen=True)
class ModelSpec:
    """Honest, provider-independent model metadata used by the router."""

    id: str
    provider: str = "gemini"
    display_name: str = ""
    capabilities: frozenset[str] = frozenset({CAP_TEXT})
    context_limit: int = 0
    output_limit: int = 0
    supports_tools: bool = False
    supports_images: bool = False
    supports_thinking: bool = False
    speed: str = "medium"          # fast | medium | slow
    cost_tier: str = "medium"      # free | low | medium | high
    notes: str = ""
    available: bool = True

    @property
    def label(self) -> str:
        return self.display_name or self.id

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            "capabilities": sorted(self.capabilities),
            "context_limit": self.context_limit,
            "output_limit": self.output_limit,
            "supports_tools": self.supports_tools,
            "supports_images": self.supports_images,
            "supports_thinking": self.supports_thinking,
            "speed": self.speed,
            "cost_tier": self.cost_tier,
            "available": self.available,
            "notes": self.notes,
        }


def spec_from_model_info(info: Any, *, provider: str = "gemini") -> ModelSpec:
    """Derive router metadata from the existing `ModelInfo` catalogue."""
    model_id = str(getattr(info, "id", "") or "")
    capabilities: set[str] = set()
    if getattr(info, "is_chat", False):
        capabilities |= {CAP_TEXT, CAP_TOOLS}
    if getattr(info, "is_image", False):
        capabilities |= {CAP_VISION}
    if getattr(info, "is_embedding", False):
        capabilities = set()
    if getattr(info, "is_deep_research", False):
        capabilities |= {CAP_TEXT, CAP_PLANNING, CAP_REASONING, CAP_LONG_CONTEXT}
    if getattr(info, "thinking", False):
        capabilities |= {CAP_REASONING}
    tier = str(getattr(info, "tier", "") or "")
    if tier == "flash-lite":
        capabilities |= {CAP_FAST, CAP_CHEAP}
    if tier in ("pro", "max", "omni", "argon", "carbon", "hydrogen"):
        capabilities |= {CAP_REASONING, CAP_PLANNING}
    if "code" in model_id or "coder" in model_id:
        capabilities |= {CAP_CODE}
    context_limit = int(getattr(info, "input_limit", 0) or 0)
    if context_limit >= 500_000:
        capabilities.add(CAP_LONG_CONTEXT)
    cost = "low" if tier in ("flash-lite", "flash") else "high" if tier in ("pro", "max") else "medium"
    speed = "fast" if tier == "flash-lite" else "slow" if tier in ("pro", "max") else "medium"
    return ModelSpec(
        id=model_id,
        provider=provider,
        display_name=str(getattr(info, "name", "") or model_id),
        capabilities=frozenset(capabilities or {CAP_TEXT}),
        context_limit=context_limit,
        output_limit=int(getattr(info, "output_limit", 0) or 0),
        supports_tools=CAP_TOOLS in capabilities,
        supports_images=CAP_VISION in capabilities,
        supports_thinking=bool(getattr(info, "thinking", False)),
        speed=speed,
        cost_tier=cost,
        notes=str(getattr(info, "description", "") or "")[:220],
    )


# ══════════════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════════════
class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[str, ModelSpec] = {}
        self._providers: dict[str, "ProviderAdapter"] = {}

    def register(self, spec: ModelSpec) -> None:
        if spec.id:
            self._models[spec.id] = spec

    def register_many(self, specs: list[ModelSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def register_provider(self, adapter: "ProviderAdapter") -> None:
        self._providers[adapter.name] = adapter

    def provider(self, name: str) -> "ProviderAdapter | None":
        return self._providers.get(name)

    def providers(self) -> list["ProviderAdapter"]:
        return sorted(self._providers.values(), key=lambda adapter: adapter.name)

    def sync_catalogue(self, models: list[Any], *, provider: str = "gemini") -> None:
        """Replace one provider's catalogue while keeping other providers."""
        self._models = {
            key: value for key, value in self._models.items() if value.provider != provider
        }
        for info in models:
            spec = spec_from_model_info(info, provider=provider)
            if spec.id:
                self.register(spec)

    def get(self, model_id: str) -> ModelSpec | None:
        return self._models.get(model_id or "")

    def all(self) -> list[ModelSpec]:
        return sorted(self._models.values(), key=lambda spec: (spec.provider, spec.id))

    def chat_models(self, *, provider: str = "") -> list[ModelSpec]:
        result = [
            spec
            for spec in self._models.values()
            if spec.available and spec.has(CAP_TEXT) and (not provider or spec.provider == provider)
        ]
        return sorted(result, key=lambda spec: (spec.provider, spec.id))

    def configured_providers(self) -> list[str]:
        return sorted(
            {
                spec.provider
                for spec in self._models.values()
                if spec.available and spec.provider != "gemini"
            }
            | {
                name
                for name, adapter in self._providers.items()
                if adapter.available() and name != "gemini"
            }
        )


# ══════════════════════════════════════════════════════════════════════
# Provider adapters
# ══════════════════════════════════════════════════════════════════════
@runtime_checkable
class ProviderAdapter(Protocol):
    name: str

    def available(self) -> bool: ...

    def models(self) -> list[ModelSpec]: ...

    async def complete(
        self,
        prompt: str,
        model: str,
        *,
        system: str = "",
        temperature: float = 0.1,
        json_mode: bool = False,
        timeout: float = 120.0,
    ) -> str: ...


class GeminiProviderAdapter:
    """Text completion through the existing `GeminiService`."""

    name = "gemini"

    def __init__(self, service: Any) -> None:
        self.service = service

    def available(self) -> bool:
        return bool(getattr(self.service, "has_key", False))

    def models(self) -> list[ModelSpec]:
        return []

    async def complete(
        self,
        prompt: str,
        model: str,
        *,
        system: str = "",
        temperature: float = 0.1,
        json_mode: bool = False,
        timeout: float = 120.0,
    ) -> str:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system or None,
            temperature=max(0.0, min(2.0, temperature)),
            max_output_tokens=4096,
            response_mime_type="application/json" if json_mode else "text/plain",
        )
        response = await asyncio.wait_for(
            self.service.client().aio.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                config=config,
            ),
            timeout=max(1.0, float(timeout or 120.0)),
        )
        return str(getattr(response, "text", "") or "")


class OpenAICompatibleAdapter:
    """Text/planning-only adapter for an explicitly configured endpoint.

    It deliberately does *not* claim tool-calling, vision, or streaming parity
    with Gemini. The router marks its models `supports_tools=False`, and the
    team worker refuses to run tool tasks on them.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        name: str = "openai-compatible",
        models: list[ModelSpec] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.name = name
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self._models = models or []
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    def models(self) -> list[ModelSpec]:
        return [spec for spec in self._models if self.available()]

    async def complete(
        self,
        prompt: str,
        model: str,
        *,
        system: str = "",
        temperature: float = 0.1,
        json_mode: bool = False,
        timeout: float = 120.0,
    ) -> str:
        if not self.available():
            raise RuntimeError(f"Provider {self.name!r} is not configured")
        import httpx

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"{self.name} returned no choices")
        return str((choices[0].get("message") or {}).get("content") or "")


# ══════════════════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════════════════
ROLE_REQUIREMENTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # role: (required, preferred)
    "architect": (frozenset({CAP_TEXT, CAP_REASONING, CAP_PLANNING}), frozenset({CAP_LONG_CONTEXT, CAP_TOOLS})),
    "researcher": (frozenset({CAP_TEXT}), frozenset({CAP_LONG_CONTEXT, CAP_FAST})),
    "planner": (frozenset({CAP_TEXT, CAP_PLANNING}), frozenset({CAP_REASONING, CAP_LONG_CONTEXT})),
    "developer": (frozenset({CAP_TEXT, CAP_TOOLS}), frozenset({CAP_CODE, CAP_LONG_CONTEXT, CAP_REASONING})),
    "debugger": (frozenset({CAP_TEXT, CAP_TOOLS, CAP_REASONING}), frozenset({CAP_CODE})),
    "tester": (frozenset({CAP_TEXT, CAP_TOOLS}), frozenset({CAP_CODE, CAP_FAST})),
    "security": (frozenset({CAP_TEXT, CAP_REASONING}), frozenset({CAP_TOOLS, CAP_LONG_CONTEXT})),
    "writer": (frozenset({CAP_TEXT}), frozenset({CAP_LONG_CONTEXT, CAP_FAST})),
}

ROLE_PREFERRED_SPEED: dict[str, str] = {
    "researcher": "fast",
    "tester": "fast",
    "writer": "fast",
    "planner": "medium",
    "developer": "medium",
    "debugger": "medium",
    "security": "slow",
    "architect": "slow",
}

COST_SCORE = {"free": 3, "low": 2, "medium": 1, "high": 0}
SPEED_SCORE = {"fast": 2, "medium": 1, "slow": 0}


class ModelRouter:
    """Deterministic capability routing with per-role overrides and cooldowns."""

    def __init__(
        self,
        registry: ModelRegistry,
        preferences: dict[str, str] | None = None,
        *,
        failure_cooldown: float = 90.0,
        clock=time.time,
    ) -> None:
        self.registry = registry
        self.preferences = dict(preferences or {})
        self.failure_cooldown = failure_cooldown
        self.clock = clock
        self._cooldowns: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self.last_routing: dict[str, str] = {}

    # ── health ────────────────────────────────────────────────────
    def mark_failed(self, model_id: str, reason: str = "", seconds: float | None = None) -> None:
        self._failures[model_id] = self._failures.get(model_id, 0) + 1
        self._cooldowns[model_id] = self.clock() + (
            self.failure_cooldown if seconds is None else max(0.0, seconds)
        )

    def mark_ok(self, model_id: str) -> None:
        self._cooldowns.pop(model_id, None)
        self._failures.pop(model_id, None)

    def cooling(self, model_id: str) -> bool:
        until = self._cooldowns.get(model_id, 0.0)
        return until > self.clock()

    def health(self) -> dict[str, dict[str, Any]]:
        return {
            model_id: {
                "failures": self._failures.get(model_id, 0),
                "cooling": self.cooling(model_id),
            }
            for model_id in set(self._failures) | set(self._cooldowns)
        }

    # ── routing ───────────────────────────────────────────────────
    def candidates(
        self,
        role: str,
        *,
        require: frozenset[str] | set[str] = frozenset(),
        prefer: frozenset[str] | set[str] = frozenset(),
        exclude: set[str] | None = None,
        strict: bool = True,
    ) -> list[tuple[int, ModelSpec]]:
        required_role, preferred_role = ROLE_REQUIREMENTS.get(
            role, (frozenset({CAP_TEXT}), frozenset())
        )
        if strict:
            required = set(required_role) | set(require)
        else:
            # The catalogue may not describe a model's planning/reasoning
            # capability. Relax the role requirements, but never the explicit
            # requirements (e.g. tools) and never below plain text chat.
            required = set(require) | {CAP_TEXT}
        preferred = set(preferred_role) | set(prefer)
        excluded = set(exclude or ())
        scored: list[tuple[int, ModelSpec]] = []
        for spec in self.registry.chat_models():
            if spec.id in excluded or self.cooling(spec.id):
                continue
            if not required.issubset(spec.capabilities):
                continue
            score = 0
            score += 4 * len(required)
            score += 2 * len(preferred & spec.capabilities)
            if self.preferences.get(role) == spec.id:
                score += 8
            if spec.speed == ROLE_PREFERRED_SPEED.get(role):
                score += 2
            score += SPEED_SCORE.get(spec.speed, 1)
            score += COST_SCORE.get(spec.cost_tier, 1)
            if spec.context_limit:
                score += min(3, spec.context_limit // 400_000)
            scored.append((score, spec))
        scored.sort(key=lambda item: (-item[0], item[1].context_limit, item[1].id))
        return scored

    def route(
        self,
        role: str,
        *,
        require: frozenset[str] | set[str] = frozenset(),
        prefer: frozenset[str] | set[str] = frozenset(),
        exclude: set[str] | None = None,
    ) -> ModelSpec | None:
        preferred_id = self.preferences.get(role, "")
        preferred_spec = self.registry.get(preferred_id) if preferred_id else None
        if (
            preferred_spec is not None
            and preferred_spec.available
            and not self.cooling(preferred_spec.id)
            and preferred_spec.id not in set(exclude or ())
        ):
            required_role, _ = ROLE_REQUIREMENTS.get(role, (frozenset({CAP_TEXT}), frozenset()))
            if set(required_role).issubset(preferred_spec.capabilities) and set(require).issubset(
                preferred_spec.capabilities
            ):
                self.last_routing[role] = preferred_spec.id
                return preferred_spec
        candidates = self.candidates(role, require=require, prefer=prefer, exclude=exclude)
        if not candidates:
            candidates = self.candidates(
                role, require=require, prefer=prefer, exclude=exclude, strict=False
            )
        chosen = candidates[0][1] if candidates else None
        if chosen is not None:
            self.last_routing[role] = chosen.id
        return chosen

    def fallbacks(self, model_id: str, role: str = "", limit: int = 3) -> list[ModelSpec]:
        candidates = self.candidates(role or "developer", exclude={model_id})
        if not candidates:
            candidates = self.candidates(role or "developer", exclude={model_id}, strict=False)
        return [spec for _, spec in candidates[: max(0, limit)]]

    def assignment_table(self) -> dict[str, str]:
        table: dict[str, str] = {}
        for role in ROLE_REQUIREMENTS:
            spec = self.route(role)
            if spec is not None:
                table[role] = spec.id
        return table


def build_registry(models: list[Any], *, service: Any = None) -> ModelRegistry:
    registry = ModelRegistry()
    registry.sync_catalogue(models, provider="gemini")
    if service is not None:
        registry.register_provider(GeminiProviderAdapter(service))
    return registry
