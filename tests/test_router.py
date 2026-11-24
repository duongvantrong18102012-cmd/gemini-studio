"""Model metadata, capability routing, preferences, cooldowns and fallbacks."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from gemini_tui.orchestration.models import (
    CAP_CODE,
    CAP_FAST,
    CAP_LONG_CONTEXT,
    CAP_PLANNING,
    CAP_REASONING,
    CAP_TEXT,
    CAP_TOOLS,
    CAP_VISION,
    GeminiProviderAdapter,
    ModelRegistry,
    ModelRouter,
    ModelSpec,
    OpenAICompatibleAdapter,
    build_registry,
    spec_from_model_info,
)


def spec(model_id: str, capabilities=(), *, text: bool = True, **kwargs) -> ModelSpec:
    caps = set(capabilities)
    if text:
        caps.add(CAP_TEXT)
    return ModelSpec(id=model_id, capabilities=frozenset(caps), **kwargs)


def registry() -> ModelRegistry:
    result = ModelRegistry()
    result.register(
        spec(
            "fast",
            {CAP_FAST},
            display_name="Fast",
            speed="fast",
            cost_tier="low",
            context_limit=128_000,
        )
    )
    result.register(
        spec(
            "coder",
            {CAP_TOOLS, CAP_CODE},
            display_name="Coder",
            speed="medium",
            cost_tier="low",
            context_limit=200_000,
            supports_tools=True,
        )
    )
    result.register(
        spec(
            "reasoner",
            {CAP_TOOLS, CAP_REASONING, CAP_PLANNING, CAP_LONG_CONTEXT},
            display_name="Reasoner",
            speed="slow",
            cost_tier="high",
            context_limit=1_000_000,
            supports_tools=True,
            supports_thinking=True,
        )
    )
    result.register(
        spec("image-only", {CAP_VISION}, text=False, speed="fast", cost_tier="low")
    )
    return result


class CatalogueTests(unittest.TestCase):
    def test_spec_from_model_info_keeps_honest_limits(self) -> None:
        info = SimpleNamespace(
            id="gemini-2.5-pro",
            name="Gemini 2.5 Pro",
            is_chat=True,
            is_image=False,
            is_embedding=False,
            is_deep_research=False,
            thinking=True,
            tier="pro",
            input_limit=1_048_576,
            output_limit=65_536,
            description="Reasoning model",
        )
        item = spec_from_model_info(info)
        self.assertEqual(item.id, "gemini-2.5-pro")
        self.assertTrue(item.supports_tools)
        self.assertTrue(item.supports_thinking)
        self.assertIn(CAP_REASONING, item.capabilities)
        self.assertIn(CAP_PLANNING, item.capabilities)
        self.assertIn(CAP_LONG_CONTEXT, item.capabilities)
        self.assertEqual(item.context_limit, 1_048_576)
        self.assertEqual(item.label, "Gemini 2.5 Pro")

    def test_image_only_model_is_not_chat_routable(self) -> None:
        reg = registry()
        self.assertNotIn("image-only", [item.id for item in reg.chat_models()])

    def test_sync_replaces_only_one_provider(self) -> None:
        reg = registry()
        reg.register(spec("other", provider="openai-compatible"))
        reg.sync_catalogue([], provider="gemini")
        self.assertIsNone(reg.get("fast"))
        self.assertIsNotNone(reg.get("other"))

    def test_build_registry_registers_gemini_adapter(self) -> None:
        service = SimpleNamespace(has_key=True)
        reg = build_registry([], service=service)
        adapter = reg.provider("gemini")
        self.assertIsInstance(adapter, GeminiProviderAdapter)
        self.assertTrue(adapter.available())
        self.assertEqual(reg.chat_models(), [])


class ProviderTests(unittest.TestCase):
    def test_openai_adapter_requires_both_endpoint_and_key(self) -> None:
        no_key = OpenAICompatibleAdapter("https://example.test/v1", "")
        self.assertFalse(no_key.available())
        self.assertEqual(no_key.models(), [])
        configured = OpenAICompatibleAdapter(
            "https://example.test/v1",
            "secret",
            name="local",
            models=[spec("local-model")],
        )
        self.assertTrue(configured.available())
        self.assertEqual([item.id for item in configured.models()], ["local-model"])

    def test_configured_providers_lists_only_available_adapters(self) -> None:
        reg = registry()
        unavailable = OpenAICompatibleAdapter("https://example.test/v1", "", name="offline")
        reg.register_provider(unavailable)
        self.assertEqual(reg.configured_providers(), [])
        reg.register_provider(
            OpenAICompatibleAdapter("https://example.test/v1", "key", name="local")
        )
        self.assertEqual(reg.configured_providers(), ["local"])


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = registry()
        self.now = [1000.0]
        self.router = ModelRouter(self.reg, clock=lambda: self.now[0])

    def test_roles_route_to_capable_models(self) -> None:
        self.assertEqual(self.router.route("developer").id, "coder")
        self.assertEqual(self.router.route("debugger").id, "reasoner")
        self.assertEqual(self.router.route("planner").id, "reasoner")
        self.assertEqual(self.router.route("security").id, "reasoner")
        self.assertEqual(self.router.route("researcher").id, "fast")
        self.assertEqual(self.router.route("writer").id, "fast")

    def test_explicit_tools_requirement_is_never_relaxed(self) -> None:
        reg = ModelRegistry()
        reg.register(spec("text-only", {CAP_FAST}))
        router = ModelRouter(reg)
        self.assertIsNotNone(router.route("researcher"))
        self.assertIsNone(router.route("developer", require={CAP_TOOLS}))
        self.assertIsNone(router.route("tester", require={CAP_TOOLS}))

    def test_role_preference_wins_when_capable(self) -> None:
        router = ModelRouter(self.reg, preferences={"developer": "reasoner"})
        self.assertEqual(router.route("developer").id, "reasoner")
        self.assertEqual(router.last_routing["developer"], "reasoner")

    def test_incompatible_preference_is_ignored(self) -> None:
        router = ModelRouter(self.reg, preferences={"developer": "fast"})
        self.assertEqual(router.route("developer").id, "coder")

    def test_cooldown_removes_model_until_expiry(self) -> None:
        self.router.mark_failed("coder", "API 500", seconds=30)
        self.assertTrue(self.router.cooling("coder"))
        self.assertEqual(self.router.route("developer").id, "reasoner")
        self.now[0] += 31
        self.assertFalse(self.router.cooling("coder"))
        self.assertEqual(self.router.route("developer").id, "coder")

    def test_mark_ok_clears_cooldown(self) -> None:
        self.router.mark_failed("coder", "transient", seconds=30)
        self.router.mark_ok("coder")
        self.assertFalse(self.router.cooling("coder"))
        self.assertEqual(self.router.health(), {})

    def test_fallbacks_exclude_the_failed_model_and_respect_tools(self) -> None:
        fallbacks = self.router.fallbacks("coder", role="developer")
        self.assertNotIn("coder", [item.id for item in fallbacks])
        self.assertEqual([item.id for item in fallbacks], ["reasoner"])
        self.assertTrue(all(item.supports_tools for item in fallbacks))

    def test_assignment_table_covers_every_role_when_models_allow(self) -> None:
        table = self.router.assignment_table()
        for role in (
            "architect",
            "researcher",
            "planner",
            "developer",
            "debugger",
            "tester",
            "security",
            "writer",
        ):
            self.assertIn(role, table)
            self.assertIsNotNone(self.reg.get(table[role]))


if __name__ == "__main__":
    unittest.main()
