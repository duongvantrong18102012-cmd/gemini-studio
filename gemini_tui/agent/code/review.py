"""Static review heuristics for Gemini Code.

These are deterministic checks, not an LLM opinion: every finding points at a
file and line, so the model (and the human) can verify it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z\-_]{30,}"),
    re.compile(r"sk-[0-9A-Za-z]{24,}"),
    re.compile(r"ghp_[0-9A-Za-z]{30,}"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]([^'\"]{12,})['\"]"),
)
DANGEROUS_PATTERNS = (
    ("python", re.compile(r"\beval\s*\("), "high", "eval() executes arbitrary input"),
    ("any", re.compile(r"\bexec\s*\("), "high", "exec() executes arbitrary input"),
    ("any", re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"), "high", "shell=True with interpolated data can inject commands"),
    ("any", re.compile(r"\bos\.system\s*\("), "medium", "os.system() has no argument separation"),
    ("js", re.compile(r"\.innerHTML\s*="), "medium", "innerHTML assignment can enable XSS"),
    ("any", re.compile(r"except\s*:\s*(?:#.*)?$"), "medium", "bare except hides failures"),
    ("any", re.compile(r"except\s+Exception\s*:\s*pass"), "medium", "swallowed exception"),
    ("any", re.compile(r"(?i)\bTODO\b|\bFIXME\b|\bHACK\b"), "info", "unresolved TODO/FIXME"),
)
JS_LANGS = {"javascript", "typescript", "html"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".exe", ".dll", ".pyc"}


@dataclass
class ReviewFinding:
    path: str
    line: int
    severity: str
    rule: str
    message: str
    excerpt: str = ""

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{self.severity.upper():<6}] {location}  {self.rule}: {self.message}"


def _walk(root: Path, limit: int = 400) -> list[Path]:
    import os

    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in skip]
        for name in sorted(filenames):
            if len(files) >= limit:
                return files
            path = Path(dirpath) / name
            if path.suffix.lower() in SKIP_SUFFIX:
                continue
            try:
                if path.stat().st_size > 400_000:
                    continue
            except OSError:
                continue
            files.append(path)
    return files


def _scan_line(path: str, line_no: int, line: str, lang: str) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    stripped = line.strip()
    if len(line) > 180:
        findings.append(
            ReviewFinding(path, line_no, "low", "long-line", f"line is {len(line)} characters", stripped[:120])
        )
    for pattern in SECRET_PATTERNS:
        match = pattern.search(line)
        if match:
            findings.append(
                ReviewFinding(path, line_no, "high", "possible-secret", "looks like a committed credential", stripped[:120])
            )
            break
    for scope, pattern, severity, message in DANGEROUS_PATTERNS:
        if scope == "python" and lang != "python":
            continue
        if scope == "js" and lang not in JS_LANGS:
            continue
        if pattern.search(line):
            findings.append(
                ReviewFinding(path, line_no, severity, pattern.pattern[:24], message, stripped[:120])
            )
    return findings


def review_files(
    root: str | Path,
    paths: Iterable[str] | None = None,
    *,
    glob: str = "",
    limit: int = 120,
    max_files: int = 200,
) -> list[ReviewFinding]:
    base = Path(root)
    selected: list[Path] = []
    if paths:
        for item in paths:
            candidate = (base / item).expanduser()
            if candidate.is_dir():
                selected.extend(_walk(candidate, max_files))
            elif candidate.is_file():
                selected.append(candidate)
    elif glob:
        selected = [path for path in _walk(base, max_files) if fnmatch.fnmatch(path.name, glob)]
    else:
        selected = _walk(base, max_files)

    findings: list[ReviewFinding] = []
    for path in selected[:max_files]:
        lang = {
            ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
            ".ts": "typescript", ".tsx": "typescript", ".html": "html",
        }.get(path.suffix.lower(), "any")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        display = path.relative_to(base).as_posix() if path.is_relative_to(base) else str(path)
        for index, line in enumerate(text.splitlines(), 1):
            findings.extend(_scan_line(display, index, line, lang))
            if len(findings) >= limit:
                return findings
    return findings


def review_diff(root: str | Path, diff: str, limit: int = 120) -> list[ReviewFinding]:
    """Review only the added lines of a unified diff (git diff / git diff --cached)."""
    findings: list[ReviewFinding] = []
    path = ""
    line_no = 0
    for raw in (diff or "").splitlines():
        if raw.startswith("+++ b/"):
            path = raw[6:].strip()
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            line_no = int(match.group(1)) if match else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            suffix = Path(path).suffix.lower()
            lang = {
                ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
                ".ts": "typescript", ".tsx": "typescript", ".html": "html",
            }.get(suffix, "any")
            findings.extend(_scan_line(path or "diff", line_no, raw[1:], lang))
            line_no += 1
            if len(findings) >= limit:
                break
        elif not raw.startswith("-") and line_no:
            line_no += 1
    return findings


def detect_test_command(root: str | Path) -> tuple[str, str]:
    """Return (command, source). Empty command means no runner was detected."""
    base = Path(root)
    if (base / "pyproject.toml").is_file() or (base / "pytest.ini").is_file() or (base / "tox.ini").is_file():
        return "python -m pytest -q", "pytest config"
    if (base / "setup.cfg").is_file():
        try:
            text = (base / "setup.cfg").read_text(encoding="utf-8", errors="replace")
            if "[tool:pytest]" in text:
                return "python -m pytest -q", "setup.cfg"
        except OSError:
            pass
    package_json = base / "package.json"
    if package_json.is_file():
        import json

        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {}
            if "test" in scripts and "no test specified" not in str(scripts["test"]).lower():
                return "npm test", "package.json"
        except (OSError, ValueError):
            pass
    if (base / "Cargo.toml").is_file():
        return "cargo test", "Cargo.toml"
    if (base / "go.mod").is_file():
        return "go test ./...", "go.mod"
    if any(base.glob("*.csproj")) or any(base.glob("*.sln")):
        return "dotnet test", ".NET solution"
    if (base / "Makefile").is_file():
        try:
            text = (base / "Makefile").read_text(encoding="utf-8", errors="replace")
            if re.search(r"^test\s*:", text, re.M):
                return "make test", "Makefile"
        except OSError:
            pass
    if (base / "composer.json").is_file():
        return "composer test", "composer.json"
    if (base / "gradlew").is_file() or (base / "gradlew.bat").is_file():
        return "gradlew test", "Gradle wrapper"
    if (base / "pom.xml").is_file():
        return "mvn test", "pom.xml"
    return "", ""


def format_findings(findings: list[ReviewFinding], limit: int = 60) -> str:
    if not findings:
        return "No heuristic findings (this is not a proof the code is correct)."
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    header = " · ".join(f"{severity} {count}" for severity, count in sorted(counts.items()))
    body = "\n".join(finding.render() for finding in findings[:limit])
    return f"{len(findings)} finding(s) · {header}\n{body}"
