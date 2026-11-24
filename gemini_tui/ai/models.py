"""Model catalogue helpers (metadata comes live from the Gemini API)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ..config import DEFAULT_MODEL, MODELS_CACHE

CURATED_FALLBACK: tuple[dict[str, Any], ...] = (
    {
        "name": "models/gemini-3.5-flash-lite",
        "display_name": "Gemini 3.5 Flash Lite",
        "description": "Low-latency workhorse. Default for this console.",
        "input_token_limit": 1048576,
        "output_token_limit": 65536,
        "supported_actions": ["generateContent", "countTokens"],
        "thinking": True,
    },
    {
        "name": "models/gemini-3.5-flash",
        "display_name": "Gemini 3.5 Flash",
        "description": "Balanced quality, speed and tool use.",
        "input_token_limit": 1048576,
        "output_token_limit": 65536,
        "supported_actions": ["generateContent", "countTokens"],
        "thinking": True,
    },
    {
        "name": "models/gemini-3.1-flash-lite",
        "display_name": "Gemini 3.1 Flash Lite",
        "description": "Previous lightweight generation.",
        "input_token_limit": 1048576,
        "output_token_limit": 65536,
        "supported_actions": ["generateContent", "countTokens"],
        "thinking": True,
    },
)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    description: str = ""
    input_limit: int = 0
    output_limit: int = 0
    thinking: bool = False
    actions: tuple[str, ...] = ()

    # ── classification ────────────────────────────────────────────
    @property
    def is_image(self) -> bool:
        return "image" in self.id or "imagen" in self.id or "nano-banana" in self.id

    @property
    def is_embedding(self) -> bool:
        return "embedding" in self.id

    @property
    def is_deep_research(self) -> bool:
        return "deep-research" in self.id

    @property
    def is_chat(self) -> bool:
        if self.is_embedding or self.is_image:
            return False
        return "generateContent" in self.actions or not self.actions

    @property
    def family(self) -> str:
        model = self.id
        if model.startswith("gemini-"):
            rest = model[len("gemini-") :]
            parts = rest.split("-")
            if parts[0][0].isdigit():
                return parts[0]
            return parts[0]
        if model.startswith("gemma"):
            return "gemma"
        if model.startswith("antigravity"):
            return "antigravity"
        return model.split("-")[0]

    @property
    def tier(self) -> str:
        model = self.id
        for tier in ("flash-lite", "flash", "pro", "omni", "argon", "carbon", "hydrogen", "max"):
            if tier in model:
                return tier
        if self.is_embedding:
            return "embedding"
        if self.is_image:
            return "image"
        if self.is_deep_research:
            return "research"
        return "model"

    @property
    def badge(self) -> str:
        label = self.tier.replace("-", " ").upper()
        return f"{self.family.upper()} · {label}"

    @property
    def context_label(self) -> str:
        if not self.input_limit:
            return "—"
        if self.input_limit >= 1_000_000:
            return f"{self.input_limit / 1_000_000:.0f}M ctx"
        return f"{self.input_limit / 1000:.0f}k ctx"

    @property
    def short_name(self) -> str:
        return self.name or self.id

    # ── parsing ───────────────────────────────────────────────────
    @classmethod
    def from_api(cls, model: Any) -> "ModelInfo":
        name = getattr(model, "name", "") or ""
        model_id = name.split("/")[-1] if name else getattr(model, "id", "")
        actions = tuple(getattr(model, "supported_actions", None) or ())
        return cls(
            id=model_id,
            name=getattr(model, "display_name", "") or model_id,
            description=getattr(model, "description", "") or "",
            input_limit=int(getattr(model, "input_token_limit", 0) or 0),
            output_limit=int(getattr(model, "output_token_limit", 0) or 0),
            thinking=bool(getattr(model, "thinking", False)),
            actions=actions,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelInfo":
        raw = str(data.get("name", "")).split("/")[-1]
        return cls(
            id=raw or str(data.get("id", "")),
            name=data.get("display_name") or data.get("name") or raw,
            description=data.get("description", "") or "",
            input_limit=int(data.get("input_token_limit") or 0),
            output_limit=int(data.get("output_token_limit") or 0),
            thinking=bool(data.get("thinking")),
            actions=tuple(data.get("supported_actions") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": f"models/{self.id}",
            "display_name": self.name,
            "description": self.description,
            "input_token_limit": self.input_limit,
            "output_token_limit": self.output_limit,
            "thinking": self.thinking,
            "supported_actions": list(self.actions),
        }


def sort_models(models: list[ModelInfo]) -> list[ModelInfo]:
    """Chat models first, then newest family, flash-lite before flash before pro."""

    def key(model: ModelInfo) -> tuple:
        family = model.family.replace(".", "")
        try:
            family_num = -float(family)
        except ValueError:
            family_num = 0.0
        tier_rank = {
            "flash-lite": 0,
            "flash": 1,
            "omni": 2,
            "pro": 3,
            "argon": 4,
            "carbon": 5,
            "hydrogen": 6,
        }.get(model.tier, 9)
        return (
            0 if model.id == DEFAULT_MODEL else 1,
            0 if model.is_chat else 1,
            family_num,
            tier_rank,
            model.id,
        )

    return sorted(models, key=key)


def load_cached_models() -> tuple[list[ModelInfo], float]:
    try:
        payload = json.loads(MODELS_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], 0.0
    models = [ModelInfo.from_dict(item) for item in payload.get("models", [])]
    return models, float(payload.get("fetched_at", 0))


def save_cached_models(models: list[ModelInfo]) -> None:
    payload = {"fetched_at": time.time(), "models": [m.to_dict() for m in models]}
    try:
        MODELS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        MODELS_CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def fallback_models() -> list[ModelInfo]:
    return [ModelInfo.from_dict(item) for item in CURATED_FALLBACK]


def find_model(models: list[ModelInfo], query: str) -> ModelInfo | None:
    query = (query or "").strip().lower()
    if not query:
        return None
    for model in models:
        if model.id.lower() == query:
            return model
    for model in models:
        if model.id.lower().endswith(query):
            return model
    for model in models:
        if query in model.id.lower() or query in model.name.lower():
            return model
    return None


def model_icon(model: ModelInfo) -> str:
    if model.is_embedding:
        return "⊞"
    if model.is_image:
        return "◈"
    if model.is_deep_research:
        return "◎"
    if model.tier == "flash-lite":
        return "⚡"
    if model.tier == "flash":
        return "✦"
    if model.tier == "pro":
        return "❖"
    return "◆"
