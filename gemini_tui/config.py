"""Configuration, paths and persisted settings for Gemini Studio TUI."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "gemini-tui"
APP_TITLE = "Gemini Studio"
APP_VERSION = "1.0.0"
DEFAULT_MODEL = "gemini-3.5-flash-lite"

ENV_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY")


def config_dir() -> Path:
    override = os.environ.get("GEMINI_TUI_HOME")
    if override:
        path = Path(override).expanduser()
    elif os.name == "nt":
        # Keep it next to other agent configs on Windows (~/.config/...)
        path = Path.home() / ".config" / APP_NAME
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_DIR = config_dir()
SESSIONS_DIR = CONFIG_DIR / "sessions"
CACHE_DIR = CONFIG_DIR / "cache"
PLUGINS_DIR = CONFIG_DIR / "plugins"
SKILLS_DIR = CONFIG_DIR / "skills"
LOGS_DIR = CONFIG_DIR / "logs"
COWORK_DIR = CONFIG_DIR / "cowork"
TEAM_DIR = CONFIG_DIR / "team"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
MCP_FILE = CONFIG_DIR / "mcp.json"
MODELS_CACHE = CACHE_DIR / "models.json"

for _directory in (SESSIONS_DIR, CACHE_DIR, PLUGINS_DIR, SKILLS_DIR, LOGS_DIR, COWORK_DIR, TEAM_DIR):
    _directory.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════
# Dotenv loading
# ══════════════════════════════════════════════════════════════════════
def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    except OSError:
        pass
    return values


def dotenv_candidates() -> list[Path]:
    candidates = [
        CONFIG_DIR / ".env",
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    # Convenience: reuse a key already configured for the local gemini-helper MCP.
    candidates.append(Path.home() / "Documents" / "gemini-helper-mcp" / ".env")
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path.expanduser()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


_WORKSPACE_DOTENV_ALLOWLIST = frozenset(ENV_KEYS + ("GEMINI_TUI_API_KEY",))


def load_dotenv_files() -> list[Path]:
    loaded: list[Path] = []
    trusted_roots = (
        CONFIG_DIR.resolve(),
        (Path.home() / "Documents" / "gemini-helper-mcp").resolve(),
    )
    for path in dotenv_candidates():
        try:
            is_trusted = any(path.resolve().is_relative_to(root) for root in trusted_roots)
        except OSError:
            is_trusted = False
        for key, value in _parse_dotenv(path).items():
            if not is_trusted and key not in _WORKSPACE_DOTENV_ALLOWLIST:
                continue
            if value and not os.environ.get(key):
                os.environ[key] = value
        loaded.append(path)
    return loaded


def get_api_key() -> str | None:
    for key in ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    stored = os.environ.get("GEMINI_TUI_API_KEY", "").strip()
    return stored or None


# ══════════════════════════════════════════════════════════════════════
# Settings
# ══════════════════════════════════════════════════════════════════════
@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    thinking_level: str = "low"          # minimal | low | medium | high
    show_thoughts: bool = True
    temperature: float = 0.8
    max_output_tokens: int = 8192
    system_prompt: str = ""
    workspace: str = field(default_factory=lambda: str(Path.cwd()))
    approval_mode: str = "ask"           # ask | auto | readonly
    animations: bool = True
    animation_fps: int = 12
    auto_connect_mcp: bool = False
    active_mcp: list[str] = field(default_factory=list)
    last_session: str = ""
    context_window_warn: float = 0.85

    # ── agent infrastructure ──────────────────────────────────────
    context_budget_tokens: int = 120_000
    auto_memory: bool = True
    auto_context: bool = True
    audit_enabled: bool = True
    code_index_max_files: int = 5000
    tool_policies: dict[str, str] = field(default_factory=dict)
    denied_tools: list[str] = field(default_factory=list)

    # ── cowork ────────────────────────────────────────────────────
    cowork_max_steps: int = 8
    cowork_step_tool_steps: int = 10

    # ── agent team ────────────────────────────────────────────────
    team_auto: bool = True               # suggest /team for complex prompts
    team_max_parallel: int = 2
    team_task_timeout: int = 300
    team_max_retries: int = 2
    team_verify: bool = True
    team_model_roles: dict[str, str] = field(default_factory=dict)
    agent_permissions: dict[str, str] = field(default_factory=dict)   # role -> ceiling risk
    provider_endpoints: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ── computer use (opt-in) ─────────────────────────────────────
    computer_enabled: bool = False
    computer_approval: str = "ask"          # ask | auto | readonly
    computer_model: str = ""
    computer_vision_width: int = 1024
    computer_max_steps: int = 10
    computer_rate_limit: int = 90
    computer_keep_screenshots: bool = True
    trusted_workspaces: list[str] = field(default_factory=list)
    allow_outside_workspace: bool = False

    def is_workspace_trusted(self, workspace: str | Path) -> bool:
        if os.environ.get("GEMINI_TUI_TRUST_WORKSPACE", "").strip() == "1":
            return True
        try:
            target = str(Path(workspace).expanduser().resolve())
        except OSError:
            return False
        for entry in self.trusted_workspaces:
            try:
                if str(Path(entry).expanduser().resolve()) == target:
                    return True
            except OSError:
                continue
        return False

    # ── persistence ────────────────────────────────────────────────
    @classmethod
    def load(cls) -> "Settings":
        settings = cls()
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return settings
        if not isinstance(data, dict):
            return settings
        settings.merge(**data)
        return settings

    def save(self) -> None:
        payload = asdict(self)
        payload.pop("api_key", None)
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=SETTINGS_FILE.parent,
                prefix=".settings-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(json.dumps(payload, indent=2, ensure_ascii=False))
                tmp_path = Path(handle.name)
            tmp_path.replace(SETTINGS_FILE)
        except OSError:
            pass

    def merge(self, **values: Any) -> None:
        valid_fields = {f.name for f in dataclasses.fields(self)}
        defaults = type(self)()
        for key, value in values.items():
            if key not in valid_fields:
                continue
            default = getattr(defaults, key)
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    continue
            elif isinstance(default, int):
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
            elif isinstance(default, float):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                value = float(value)
            elif isinstance(default, str):
                if not isinstance(value, str):
                    continue
            elif isinstance(default, list):
                if not isinstance(value, list):
                    continue
            elif isinstance(default, dict):
                if not isinstance(value, dict):
                    continue
            if key == "approval_mode" and value not in ("ask", "auto", "readonly"):
                continue
            if key == "computer_approval" and value not in ("ask", "auto", "readonly"):
                continue
            if key == "thinking_level" and value not in ("minimal", "low", "medium", "high"):
                continue
            setattr(self, key, value)


DEFAULT_SYSTEM_PROMPT = """You are Gemini Studio, a senior engineering agent running inside a terminal UI on Windows.

Four working modes are available:
- Gemini Code: inspect the workspace with the code index, read before editing, make the smallest correct change, run tests.
- Gemini Cowork: for multi-step jobs, call cowork_run so a planner, scoped workers and a verifier handle it.
- Gemini Computer: observe with computer_status/computer_screenshot before acting; input tools require approval.
- Agent infrastructure: memory_*, skill_load, MCP tools and context_retrieve keep context small and persistent.

Operating rules:
- Be concise and concrete. Prefer doing over explaining, but state non-obvious tradeoffs briefly.
- Use tools instead of guessing. Never invent file contents, command output or API results.
- Always read a file before editing it, and verify changes with a real command or tool when possible.
- Dangerous tools (write/edit/shell/computer input) may require human approval; if denied, stop and explain.
- Prefer code_search/code_outline over reading whole files. Load a skill with skill_load when the task matches one.
- Do not type passwords or secrets into applications. Do not run destructive commands without an explicit request.
- Answers render as Markdown: short headings, fenced code blocks with language tags, tight bullet lists.
- Today's date is available through time_now when it matters."""


def system_prompt(settings: Settings, extra: str = "") -> str:
    custom = (settings.system_prompt or "").strip()
    base = custom or DEFAULT_SYSTEM_PROMPT
    extra = (extra or "").strip()
    if not extra:
        return base
    return f"{base}\n\n{extra}"


def ensure_layout() -> None:
    for directory in (SESSIONS_DIR, CACHE_DIR, PLUGINS_DIR, SKILLS_DIR, LOGS_DIR, COWORK_DIR, TEAM_DIR):
        directory.mkdir(parents=True, exist_ok=True)
