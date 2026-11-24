"""Slash-command registry with fuzzy search (used by the palette too)."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Command:
    name: str
    description: str
    usage: str = ""
    category: str = "general"
    handler: Callable[..., Any] = None  # type: ignore[assignment]
    aliases: tuple[str, ...] = ()
    source: str = "builtin"
    hidden: bool = False

    @property
    def label(self) -> str:
        return f"/{self.name}"

    @property
    def hint(self) -> str:
        return self.usage or f"/{self.name}"

    async def run(self, app: Any, argument: str) -> Any:
        result = self.handler(app, argument)
        if inspect.isawaitable(result):
            return await result
        return result


def _score(query: str, text: str) -> int:
    """Cheap fuzzy score; higher is better, 0 means no match."""
    query = query.lower().strip()
    text = text.lower()
    if not query:
        return 1
    if text == query:
        return 100
    if text.startswith(query):
        return 80
    if query in text:
        return 60
    # subsequence
    index = 0
    hits = 0
    for char in text:
        if index < len(query) and char == query[index]:
            index += 1
            hits += 1
    if index == len(query):
        return 20 + hits
    return 0


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    # ── registration ──────────────────────────────────────────────
    def add(self, command: Command) -> None:
        self._commands[command.name.lower()] = command

    def register(
        self,
        name: str,
        description: str,
        *,
        usage: str = "",
        category: str = "general",
        aliases: tuple[str, ...] = (),
        hidden: bool = False,
    ) -> Callable:
        def decorator(func: Callable) -> Callable:
            self.add(
                Command(
                    name=name,
                    description=description,
                    usage=usage,
                    category=category,
                    handler=func,
                    aliases=aliases,
                    hidden=hidden,
                )
            )
            return func

        return decorator

    def register_external(
        self,
        name: str,
        description: str,
        usage: str,
        handler: Callable,
        source: str,
    ) -> None:
        self.add(
            Command(
                name=name,
                description=description,
                usage=usage,
                category="plugin",
                handler=handler,
                source=source,
            )
        )

    def remove_source(self, source: str) -> None:
        for key in [key for key, command in self._commands.items() if command.source == source]:
            self._commands.pop(key, None)

    # ── lookup ────────────────────────────────────────────────────
    def get(self, name: str) -> Command | None:
        key = (name or "").strip().lstrip("/").lower()
        command = self._commands.get(key)
        if command:
            return command
        for candidate in self._commands.values():
            if key in candidate.aliases:
                return candidate
        return None

    def all(self, include_hidden: bool = False) -> list[Command]:
        commands = [command for command in self._commands.values() if include_hidden or not command.hidden]
        commands.sort(key=lambda command: (command.category, command.name))
        return commands

    def search(self, query: str, limit: int = 40) -> list[Command]:
        query = (query or "").strip().lstrip("/")
        scored: list[tuple[int, Command]] = []
        for command in self._commands.values():
            if command.hidden:
                continue
            best = max(
                _score(query, command.name),
                _score(query, command.description) - 10,
                max((_score(query, alias) for alias in command.aliases), default=0),
            )
            if best:
                scored.append((best, command))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [command for _, command in scored[:limit]]

    def categories(self) -> dict[str, list[Command]]:
        grouped: dict[str, list[Command]] = {}
        for command in self.all():
            grouped.setdefault(command.category, []).append(command)
        return grouped


def parse_slash(text: str) -> tuple[str, str] | None:
    """Split '/cmd rest' into (cmd, rest); None if not a slash command."""
    stripped = text.strip()
    if not stripped.startswith("/") or len(stripped) < 2:
        return None
    body = stripped[1:]
    for index, char in enumerate(body):
        if char.isspace():
            return body[:index], body[index:].strip()
    return body, ""
