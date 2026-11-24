"""Plugin loading.

Two kinds of plugins are surfaced:

* native Python plugins in ``~/.config/gemini-tui/plugins/*.py`` which can add
  tools and commands to this TUI, and
* external opencode / claude plugins already installed on the machine, which are
  detected and can be delegated to through the ``opencode_run`` bridge.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..animations import truncate
from ..config import CONFIG_DIR
from .mcp_client import _read_json, _strip_jsonc
from .registry import ToolRegistry, ToolSpec


@dataclass
class PluginInfo:
    name: str
    source: str = "native"           # native | opencode | claude
    kind: str = "python"             # python | javascript | typescript | package
    path: str = ""
    version: str = ""
    description: str = ""
    status: str = "loaded"           # loaded | error | external
    error: str = ""
    tools: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)

    @property
    def runtime(self) -> str:
        return "gemini-tui" if self.source == "native" else "opencode"


class PluginAPI:
    """The object handed to a plugin's ``setup(api)`` function."""

    def __init__(self, plugin: PluginInfo, registry: ToolRegistry, commands: Any, app: Any = None) -> None:
        self.plugin = plugin
        self.registry = registry
        self.commands = commands
        self.app = app

    # ── tools ─────────────────────────────────────────────────────
    def tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
        *,
        dangerous: bool = False,
        risk: str = "",
        icon: str = "◆",
    ) -> Callable:
        def decorator(func: Callable) -> Callable:
            existing = self.registry.get(name)
            if existing is not None and existing.source == "builtin":
                raise ValueError(f"Plugin {self.plugin.name!r} cannot override builtin tool {name!r}")
            resolved_risk = risk or ("execute" if dangerous else "write")
            self.registry.add(
                ToolSpec(
                    name=name,
                    description=description,
                    parameters=parameters or {"type": "object", "properties": {}},
                    handler=func,
                    dangerous=dangerous or resolved_risk in ("write", "execute", "computer"),
                    risk=resolved_risk,
                    category=f"plugin · {self.plugin.name}",
                    icon=icon,
                    source=f"plugin:{self.plugin.name}",
                )
            )
            self.plugin.tools.append(name)
            return func

        return decorator

    # ── commands ──────────────────────────────────────────────────
    def command(self, name: str, description: str, usage: str = "") -> Callable:
        def decorator(func: Callable) -> Callable:
            if self.commands is not None and hasattr(self.commands, "register_external"):
                self.commands.register_external(
                    name=name,
                    description=description,
                    usage=usage,
                    handler=func,
                    source=f"plugin:{self.plugin.name}",
                )
            self.plugin.commands.append(name)
            return func

        return decorator

    # ── misc ──────────────────────────────────────────────────────
    def notify(self, message: str, severity: str = "info") -> None:
        if self.app is not None and hasattr(self.app, "notify_user"):
            try:
                self.app.notify_user(message, severity)
            except Exception:
                pass

    @property
    def config_dir(self) -> Path:
        return CONFIG_DIR


class PluginManager:
    def __init__(self, registry: ToolRegistry, commands: Any = None, workspace: str = "") -> None:
        self.registry = registry
        self.commands = commands
        self.workspace = workspace
        self.plugins: list[PluginInfo] = []
        self._modules: dict[str, Any] = {}

    def _is_workspace_trusted(self, app: Any = None) -> bool:
        settings = getattr(app, "settings", None) if app is not None else None
        if settings is not None and hasattr(settings, "is_workspace_trusted"):
            return bool(settings.is_workspace_trusted(self.workspace))
        import os
        return os.environ.get("GEMINI_TUI_TRUST_WORKSPACE", "").strip() == "1"

    # ── discovery ─────────────────────────────────────────────────
    def discover(self, app: Any = None) -> list[PluginInfo]:
        external = self._discover_external()
        native = self._discover_native(app)
        self.plugins = native + external
        return self.plugins

    def _discover_native(self, app: Any = None) -> list[PluginInfo]:
        results: list[PluginInfo] = []
        global_dir = CONFIG_DIR / "plugins"
        workspace_dir = Path(self.workspace) / ".gemini-tui" / "plugins" if self.workspace else None
        trusted = self._is_workspace_trusted(app)
        directories: list[tuple[Path, bool]] = [(global_dir, True)]
        if workspace_dir is not None:
            directories.append((workspace_dir, trusted))
        seen: set[str] = set()
        for directory, is_trusted in directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                key = path.stem.lower()
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    PluginInfo(
                        name=path.stem,
                        source="native",
                        kind="python",
                        path=str(path),
                        status="installed" if is_trusted else "untrusted",
                        description=(
                            "Native Python plugin"
                            if is_trusted
                            else "Workspace plugin blocked (untrusted workspace)"
                        ),
                    )
                )
        return results

    def _discover_external(self) -> list[PluginInfo]:
        home = Path.home()
        results: list[PluginInfo] = []
        seen: set[str] = set()

        plugin_dirs = [
            home / ".config" / "opencode" / "plugin",
            home / ".config" / "opencode" / "plugins",
            home / ".claude" / "plugins",
            Path(self.workspace) / ".opencode" / "plugin",
        ]
        for directory in plugin_dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.is_dir():
                    manifest = path / "package.json"
                    name = path.name
                    version = ""
                    description = ""
                    if manifest.is_file():
                        payload = _read_json(manifest)
                        name = str(payload.get("name") or name)
                        version = str(payload.get("version") or "")
                        description = truncate(str(payload.get("description") or ""), 120)
                    if name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    results.append(
                        PluginInfo(
                            name=name,
                            source="opencode",
                            kind="package",
                            path=str(path),
                            version=version,
                            description=description or "External plugin package",
                            status="external",
                        )
                    )
                elif path.suffix.lower() in (".js", ".ts", ".mjs", ".cjs"):
                    name = path.stem
                    if name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    results.append(
                        PluginInfo(
                            name=name,
                            source="opencode",
                            kind="typescript" if path.suffix.lower() == ".ts" else "javascript",
                            path=str(path),
                            status="external",
                            description="opencode plugin (use the opencode bridge)",
                        )
                    )

        # plugins listed in opencode.jsonc
        for config_path in (
            home / ".config" / "opencode" / "opencode.jsonc",
            home / ".config" / "opencode" / "opencode.json",
        ):
            if not config_path.is_file():
                continue
            try:
                payload = json.loads(_strip_jsonc(config_path.read_text(encoding="utf-8", errors="replace")))
            except Exception:
                continue
            for entry in payload.get("plugin") or []:
                name = str(entry).split("@")[0] if not str(entry).startswith("@") else "@".join(str(entry).split("@")[:2])
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                results.append(
                    PluginInfo(
                        name=name,
                        source="opencode",
                        kind="package",
                        path=str(config_path),
                        version=str(entry).replace(name, "").lstrip("@"),
                        status="external",
                        description="Declared in opencode config",
                    )
                )
        return results

    # ── loading ───────────────────────────────────────────────────
    def load_all(self, app: Any = None) -> list[PluginInfo]:
        self.discover(app)
        for plugin in self.plugins:
            if plugin.source != "native" or plugin.status == "untrusted":
                continue
            self._load_native(plugin, app)
        return self.plugins

    def _load_native(self, plugin: PluginInfo, app: Any) -> None:
        path = Path(plugin.path)
        module_name = f"gemini_tui_plugin_{plugin.name}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise RuntimeError("could not create import spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            meta = getattr(module, "PLUGIN", {}) or {}
            if isinstance(meta, dict):
                plugin.version = str(meta.get("version", "") or "")
                plugin.description = str(meta.get("description", "") or plugin.description)
                plugin.name = str(meta.get("name", plugin.name) or plugin.name)
            setup = getattr(module, "setup", None)
            if callable(setup):
                api = PluginAPI(plugin, self.registry, self.commands, app)
                setup(api)
            plugin.status = "loaded"
            self._modules[plugin.name] = module
        except Exception as error:
            plugin.status = "error"
            plugin.error = f"{type(error).__name__}: {error}"
            plugin.description = plugin.description or "Plugin failed to load"
            detail = traceback.format_exc(limit=3)
            try:
                log = CONFIG_DIR / "logs" / "plugins.log"
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(f"\n=== {plugin.name} ===\n{detail}\n")
            except OSError:
                pass

    def reload(self, app: Any = None) -> list[PluginInfo]:
        for plugin in self.plugins:
            if plugin.source == "native":
                self.registry.remove_source(f"plugin:{plugin.name}")
                if self.commands is not None and hasattr(self.commands, "remove_source"):
                    try:
                        self.commands.remove_source(f"plugin:{plugin.name}")
                    except Exception:
                        pass
        self._modules.clear()
        return self.load_all(app)

    @property
    def loaded_count(self) -> int:
        return sum(1 for plugin in self.plugins if plugin.status == "loaded")

    @property
    def external_count(self) -> int:
        return sum(1 for plugin in self.plugins if plugin.source != "native")
