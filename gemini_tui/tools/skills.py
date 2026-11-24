"""Skill discovery (opencode / claude compatible SKILL.md folders)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import CONFIG_DIR
from .registry import ToolContext, ToolError, ToolRegistry, prop, schema


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path
    body: str
    source: str
    directory: str = ""
    tools: tuple[str, ...] = ()

    @property
    def words(self) -> int:
        return len(self.body.split())

    @property
    def tokens(self) -> int:
        return max(1, len(self.body) // 4)

    @property
    def folder(self) -> str:
        return self.path.parent.name


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    meta: dict[str, Any] = {}
    end = None
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() in ("---", "..."):
            end = index
            break
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            value = value.strip()
            if value.startswith(("[", '"', "'")):
                value = value.strip("[]\"'")
            meta[key.strip().lower()] = value
    if end is None:
        return {}, text
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


def _skill_dirs(workspace: str) -> list[tuple[str, Path]]:
    home = Path.home()
    directories: list[tuple[str, Path]] = [
        ("gemini-tui", CONFIG_DIR / "skills"),
        ("opencode", home / ".config" / "opencode" / "skills"),
        ("claude", home / ".claude" / "skills"),
        ("opencode", home / ".opencode" / "skill"),
        ("project", Path(workspace) / ".opencode" / "skills"),
        ("project", Path(workspace) / ".claude" / "skills"),
        ("project", Path(workspace) / "skills"),
    ]
    seen: set[Path] = set()
    result: list[tuple[str, Path]] = []
    for source, path in directories:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        result.append((source, resolved))
    return result


def _iter_skill_files(root: Path) -> list[Path]:
    found: list[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth > 3:
                dirnames[:] = []
                continue
            dirnames[:] = [name for name in dirnames if not name.startswith(".") and name != "node_modules"]
            lowered = {name.lower() for name in filenames}
            if "skill.md" in lowered:
                for name in filenames:
                    if name.lower() == "skill.md":
                        found.append(Path(dirpath) / name)
                dirnames[:] = []  # a skill folder does not contain nested skills
    except OSError:
        pass
    return found


def discover_skills(workspace: str) -> list[SkillInfo]:
    skills: dict[str, SkillInfo] = {}
    for source, root in _skill_dirs(workspace):
        for path in _iter_skill_files(root):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = _parse_frontmatter(text)
            name = str(meta.get("name") or path.parent.name).strip()
            if not name:
                continue
            tools = tuple(
                item.strip().strip('"').strip("'")
                for item in re.split(r"[,\s]+", str(meta.get("allowed-tools") or meta.get("allowed_tools") or ""))
                if item.strip().strip('"').strip("'")
            )
            info = SkillInfo(
                name=name,
                description=str(meta.get("description") or "").strip(),
                path=path,
                body=body,
                source=source,
                directory=str(path.parent),
                tools=tools,
            )
            existing = skills.get(name)
            if existing is None or info.source in ("gemini-tui", "project"):
                skills[name] = info
    return sorted(skills.values(), key=lambda skill: (skill.source != "project", skill.name.lower()))


class SkillLibrary:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.skills: list[SkillInfo] = []
        self.reload()

    def reload(self) -> None:
        self.skills = discover_skills(self.workspace)

    def get(self, name: str) -> SkillInfo | None:
        needle = (name or "").strip().lower()
        if not needle:
            return None
        for skill in self.skills:
            if skill.name.lower() == needle or skill.folder.lower() == needle:
                return skill
        for skill in self.skills:
            if needle in skill.name.lower() or needle in skill.description.lower():
                return skill
        return None

    def catalog(self, limit: int = 60) -> str:
        if not self.skills:
            return ""
        lines = []
        for skill in self.skills[:limit]:
            description = " ".join(skill.description.split())[:170]
            lines.append(f"- {skill.name}: {description}")
        return "\n".join(lines)

    def register_tools(self, registry: ToolRegistry) -> None:
        library = self

        async def skill_load(args: dict[str, Any], ctx: ToolContext) -> str:
            name = str(args.get("name") or "").strip()
            skill = library.get(name)
            if skill is None:
                available = ", ".join(item.name for item in library.skills[:40]) or "(none)"
                raise ToolError(f"Unknown skill {name!r}. Available: {available}")
            body = skill.body.strip()
            if len(body) > 60_000:
                body = body[:60_000] + "\n\n… skill truncated"
            return (
                f"# Skill loaded: {skill.name}\n"
                f"Source: {skill.path}\n"
                f"Follow these instructions for the current task.\n\n"
                f"{body}"
            )

        async def skill_list(args: dict[str, Any], ctx: ToolContext) -> str:
            if not library.skills:
                return "No skills installed. Add folders with a SKILL.md to ~/.config/gemini-tui/skills."
            return "\n".join(
                f"{skill.name:<28} {skill.source:<10} {skill.words:>6} words  {skill.description[:100]}"
                for skill in library.skills
            )

        registry.add(
            _make_spec(
                "skill_load",
                "Load a skill's full instructions by name. Skills contain specialised workflows "
                "(documents, video, design, MCP building, …). Load one when the task matches its "
                "description, before writing code.",
                schema({"name": prop("string", "Skill name or folder")}, ["name"]),
                skill_load,
                "skills",
                "◈",
            )
        )
        registry.add(
            _make_spec(
                "skill_list",
                "List installed skills with their source and description.",
                schema({}, []),
                skill_list,
                "skills",
                "◈",
            )
        )

    def prompt_block(self) -> str:
        if not self.skills:
            return ""
        return (
            "Available skills (progressive disclosure — call skill_load(name) to read the full "
            "workflow before you start a matching task):\n"
            + self.catalog()
        )


def _make_spec(name, description, parameters, handler, category, icon):
    from .registry import ToolSpec

    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        category=category,
        icon=icon,
        source="skills",
    )
