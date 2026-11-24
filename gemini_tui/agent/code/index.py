"""Workspace index: languages, symbols, imports and lexical search."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ...config import CACHE_DIR

INDEX_VERSION = 1
MAX_FILE_BYTES = 400_000
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    ".turbo", ".idea", ".vs", "target", ".gradle", ".cache", "site-packages",
    "coverage", ".terraform", "vendor", ".tox", ".dart_tool", "bin", "obj",
}
LANG_BY_SUFFIX = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".scala": "scala",
    ".sh": "shell", ".ps1": "powershell", ".lua": "lua",
    ".html": "html", ".css": "css", ".scss": "css", ".sql": "sql",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".md": "markdown", ".txt": "text",
}
CODE_LANGS = {
    "python", "typescript", "javascript", "go", "rust", "java", "kotlin",
    "csharp", "c", "cpp", "ruby", "php", "swift", "scala", "shell", "powershell", "lua",
}
ENTRY_NAMES = {
    "main.py", "__main__.py", "app.py", "server.py", "tui.py", "manage.py",
    "index.ts", "index.js", "main.ts", "main.js", "app.ts", "app.tsx",
    "main.go", "lib.rs", "main.rs", "Program.cs",
}

_RE_IMPORT_JS = re.compile(r"""(?:from\s+|require\(\s*)['"]([^'"]+)['"]""")
_RE_IMPORT_PY_FALLBACK = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)
_RE_SYMBOLS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "javascript": (
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M)),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.M)),
        ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)", re.M)),
        ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)", re.M)),
        ("enum", re.compile(r"^\s*(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)", re.M)),
    ),
    "go": (
        ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.M)),
        ("struct", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+struct", re.M)),
        ("interface", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+interface", re.M)),
    ),
    "rust": (
        ("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)", re.M)),
        ("struct", re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)", re.M)),
        ("enum", re.compile(r"^\s*(?:pub\s+)?enum\s+([A-Za-z_]\w*)", re.M)),
        ("trait", re.compile(r"^\s*(?:pub\s+)?trait\s+([A-Za-z_]\w*)", re.M)),
    ),
    "java": (
        ("class", re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|abstract\s+|final\s+)*class\s+([A-Za-z_]\w*)", re.M)),
        ("interface", re.compile(r"^\s*(?:public\s+)?interface\s+([A-Za-z_]\w*)", re.M)),
        ("method", re.compile(r"^\s*(?:public|private|protected)\s+[\w<>\[\],\s]+\s+([A-Za-z_]\w*)\s*\(", re.M)),
    ),
    "kotlin": (
        ("class", re.compile(r"^\s*(?:data\s+|sealed\s+|abstract\s+|open\s+)*class\s+([A-Za-z_]\w*)", re.M)),
        ("function", re.compile(r"^\s*(?:private\s+|internal\s+|public\s+)?fun\s+([A-Za-z_]\w*)", re.M)),
    ),
    "csharp": (
        ("class", re.compile(r"^\s*(?:public\s+|internal\s+|private\s+|abstract\s+|sealed\s+|static\s+|partial\s+)*class\s+([A-Za-z_]\w*)", re.M)),
        ("interface", re.compile(r"^\s*(?:public\s+)?interface\s+([A-Za-z_]\w*)", re.M)),
        ("method", re.compile(r"^\s*(?:public|private|protected|internal)\s+[\w<>\[\],\s]+\s+([A-Za-z_]\w*)\s*\(", re.M)),
        ("record", re.compile(r"^\s*(?:public\s+)?record\s+([A-Za-z_]\w*)", re.M)),
    ),
    "ruby": (
        ("class", re.compile(r"^\s*class\s+([A-Za-z_]\w*)", re.M)),
        ("module", re.compile(r"^\s*module\s+([A-Za-z_]\w*)", re.M)),
        ("method", re.compile(r"^\s*def\s+([A-Za-z_]\w*[!?=]?)", re.M)),
    ),
    "php": (
        ("class", re.compile(r"^\s*(?:abstract\s+|final\s+)*class\s+([A-Za-z_]\w*)", re.M)),
        ("function", re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+([A-Za-z_]\w*)", re.M)),
    ),
    "swift": (
        ("class", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|final\s+)*(?:class|struct|enum|protocol)\s+([A-Za-z_]\w*)", re.M)),
        ("function", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+)?func\s+([A-Za-z_]\w*)", re.M)),
    ),
}
# languages that share the JS/TS parser
_RE_SYMBOLS["typescript"] = _RE_SYMBOLS["javascript"]
_RE_SYMBOLS["c"] = (
    ("function", re.compile(r"^\s*(?:static\s+|inline\s+|extern\s+)*[A-Za-z_][\w\s\*]*\s+([A-Za-z_]\w*)\s*\([^;]*$", re.M)),
    ("struct", re.compile(r"^\s*(?:typedef\s+)?struct\s+([A-Za-z_]\w*)", re.M)),
)
_RE_SYMBOLS["cpp"] = _RE_SYMBOLS["c"] + (
    ("class", re.compile(r"^\s*(?:template\s*<[^>]*>\s*)?class\s+([A-Za-z_]\w*)", re.M)),
    ("namespace", re.compile(r"^\s*namespace\s+([A-Za-z_]\w*)", re.M)),
)


def language_for(path: Path) -> str:
    return LANG_BY_SUFFIX.get(path.suffix.lower(), "")


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(2048)
    except OSError:
        return True


def _read_text(path: Path, limit: int = MAX_FILE_BYTES) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _first_doc(lines: list[str], start: int, end: int) -> str:
    for line in lines[start : min(end, start + 8)]:
        cleaned = line.strip().strip("\"'").strip()
        if not cleaned or cleaned in ("{", "}", "};", "pass", "..."):
            continue
        if cleaned.startswith(("#", "//", "/*", "*", "--")):
            return cleaned.lstrip("#/ *-")[:160]
        break
    return ""


@dataclass
class CodeSymbol:
    name: str
    kind: str
    line: int
    end_line: int = 0
    parent: str = ""
    doc: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "line": self.line,
            "end_line": self.end_line,
            "parent": self.parent,
            "doc": self.doc,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CodeSymbol":
        return cls(
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "symbol")),
            line=int(data.get("line", 0) or 0),
            end_line=int(data.get("end_line", 0) or 0),
            parent=str(data.get("parent", "")),
            doc=str(data.get("doc", "")),
        )


@dataclass
class FileEntry:
    path: str
    lang: str
    size: int
    lines: int
    mtime: float
    symbols: list[CodeSymbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "lang": self.lang,
            "size": self.size,
            "lines": self.lines,
            "mtime": self.mtime,
            "symbols": [symbol.to_dict() for symbol in self.symbols],
            "imports": list(self.imports),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileEntry":
        return cls(
            path=str(data.get("path", "")),
            lang=str(data.get("lang", "")),
            size=int(data.get("size", 0) or 0),
            lines=int(data.get("lines", 0) or 0),
            mtime=float(data.get("mtime", 0) or 0),
            symbols=[CodeSymbol.from_dict(item) for item in data.get("symbols", [])],
            imports=[str(item) for item in data.get("imports", [])][:60],
            summary=str(data.get("summary", "")),
        )


@dataclass
class SearchHit:
    path: str
    line: int
    score: float
    kind: str
    snippet: str
    symbol: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "score": self.score,
            "kind": self.kind,
            "snippet": self.snippet,
            "symbol": self.symbol,
        }


def _python_symbols(text: str) -> tuple[list[CodeSymbol], list[str]]:
    symbols: list[CodeSymbol] = []
    imports: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        symbols, imports = _regex_symbols(text, "python")
        return symbols, imports
    lines = text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif node.module:
                imports.append(node.module)
    def visit(node: ast.AST, parent: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async function" if isinstance(child, ast.AsyncFunctionDef) else ("method" if parent else "function")
                doc = (ast.get_docstring(child) or "").strip().splitlines()
                symbols.append(
                    CodeSymbol(
                        name=child.name,
                        kind=kind,
                        line=child.lineno,
                        end_line=getattr(child, "end_lineno", 0) or 0,
                        parent=parent,
                        doc=(doc[0][:160] if doc else _first_doc(lines, child.lineno, getattr(child, "end_lineno", child.lineno + 3))),
                    )
                )
                visit(child, f"{parent}.{child.name}" if parent else child.name)
            elif isinstance(child, ast.ClassDef):
                doc = (ast.get_docstring(child) or "").strip().splitlines()
                symbols.append(
                    CodeSymbol(
                        name=child.name,
                        kind="class",
                        line=child.lineno,
                        end_line=getattr(child, "end_lineno", 0) or 0,
                        parent=parent,
                        doc=(doc[0][:160] if doc else _first_doc(lines, child.lineno, getattr(child, "end_lineno", child.lineno + 4))),
                    )
                )
                visit(child, f"{parent}.{child.name}" if parent else child.name)
            else:
                visit(child, parent)

    visit(tree)
    return symbols, sorted(set(imports))


def _regex_symbols(text: str, lang: str) -> tuple[list[CodeSymbol], list[str]]:
    symbols: list[CodeSymbol] = []
    imports: list[str] = []
    for kind, pattern in _RE_SYMBOLS.get(lang, ()):
        for match in pattern.finditer(text):
            name = match.group(1)
            line = text.count("\n", 0, match.start()) + 1
            symbols.append(CodeSymbol(name=name, kind=kind, line=line))
    if lang in ("javascript", "typescript"):
        for match in _RE_IMPORT_JS.finditer(text):
            imports.append(match.group(1))
    elif lang == "python":
        for match in _RE_IMPORT_PY_FALLBACK.finditer(text):
            imports.append(match.group(1) or match.group(2))
    # de-duplicate by (name, line)
    seen: set[tuple[str, int]] = set()
    unique: list[CodeSymbol] = []
    for symbol in symbols:
        key = (symbol.qualified, symbol.line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(symbol)
    return unique, sorted(set(imports))


def _summarise(text: str, lang: str, lines: list[str]) -> str:
    if lang == "python":
        try:
            doc = ast.get_docstring(ast.parse(text)) or ""
            if doc.strip():
                return " ".join(doc.split())[:200]
        except SyntaxError:
            pass
    for line in lines[:12]:
        cleaned = line.strip()
        if not cleaned:
            continue
        if cleaned.startswith(("#", "//", "/*", "*", "--", '"""', "'''")):
            return cleaned.lstrip("#/ *-\"'")[:200]
        if lang in CODE_LANGS and cleaned.startswith(("import ", "from ", "use ", "package ")):
            continue
        return cleaned[:200]
    return ""


def build_index(
    root: str | Path,
    *,
    max_files: int = 5000,
    max_bytes: int = MAX_FILE_BYTES,
    include_text: bool = False,
) -> "CodeIndex":
    base = Path(root).expanduser()
    files: dict[str, FileEntry] = {}
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS and not name.startswith("."))
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            lang = language_for(path)
            if not lang:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > max_bytes or stat.st_size == 0:
                continue
            text = _read_text(path, max_bytes)
            if not text:
                continue
            lines = text.splitlines()
            if lang == "python":
                symbols, imports = _python_symbols(text)
            elif lang in CODE_LANGS:
                symbols, imports = _regex_symbols(text, lang)
            else:
                symbols, imports = [], []
            relative = path.relative_to(base).as_posix()
            files[relative] = FileEntry(
                path=relative,
                lang=lang,
                size=int(stat.st_size),
                lines=len(lines),
                mtime=float(stat.st_mtime),
                symbols=symbols[:400],
                imports=imports[:80],
                summary=_summarise(text, lang, lines),
            )
            total_bytes += stat.st_size
            if len(files) >= max_files:
                break
        if len(files) >= max_files:
            break
    return CodeIndex(root=str(base.resolve()), files=files, total_bytes=total_bytes)


@dataclass
class CodeIndex:
    root: str
    files: dict[str, FileEntry] = field(default_factory=dict)
    built_at: float = field(default_factory=time.time)
    total_bytes: int = 0
    version: int = INDEX_VERSION

    # ── persistence ───────────────────────────────────────────────
    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha1(self.root.encode("utf-8", errors="replace")).hexdigest()[:10]
        return digest

    def cache_path(self, directory: Path | None = None) -> Path:
        return (directory or CACHE_DIR) / f"code-index-{self.fingerprint}.json"

    def save(self, path: Path | None = None) -> Path:
        target = path or self.cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "root": self.root,
            "built_at": self.built_at,
            "total_bytes": self.total_bytes,
            "files": [entry.to_dict() for entry in self.files.values()],
        }
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: Path) -> "CodeIndex | None":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
            return None
        files = {}
        for item in payload.get("files", []):
            entry = FileEntry.from_dict(item)
            files[entry.path] = entry
        return cls(
            root=str(payload.get("root", "")),
            files=files,
            built_at=float(payload.get("built_at", 0) or 0),
            total_bytes=int(payload.get("total_bytes", 0) or 0),
        )

    # ── queries ───────────────────────────────────────────────────
    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def symbol_count(self) -> int:
        return sum(len(entry.symbols) for entry in self.files.values())

    @property
    def line_count(self) -> int:
        return sum(entry.lines for entry in self.files.values())

    def languages(self) -> list[tuple[str, int, int]]:
        counts: dict[str, tuple[int, int]] = {}
        for entry in self.files.values():
            files, lines = counts.get(entry.lang, (0, 0))
            counts[entry.lang] = (files + 1, lines + entry.lines)
        return sorted(((lang, files, lines) for lang, (files, lines) in counts.items()), key=lambda row: -row[1])

    def summary(self, limit: int = 1600) -> str:
        if not self.files:
            return "Workspace index is empty."
        langs = ", ".join(f"{lang} {files}f/{lines}l" for lang, files, lines in self.languages()[:5])
        roots: dict[str, int] = {}
        for path in self.files:
            head = path.split("/", 1)[0] if "/" in path else "."
            roots[head] = roots.get(head, 0) + 1
        top_dirs = ", ".join(f"{name}/" for name, _ in sorted(roots.items(), key=lambda pair: -pair[1])[:6])
        entry_points = [path for path in self.files if Path(path).name in ENTRY_NAMES][:6]
        text = (
            f"Index: {self.file_count} files · {self.line_count:,} lines · {self.symbol_count:,} symbols · {langs}\n"
            f"Top directories: {top_dirs or '.'}\n"
        )
        if entry_points:
            text += "Entry points: " + ", ".join(entry_points) + "\n"
        return text[:limit]

    def outline(self, path: str, max_items: int = 120) -> str:
        key = path.replace("\\", "/").lstrip("./")
        entry = self.files.get(key)
        if entry is None:
            matches = [name for name in self.files if name.endswith(key)]
            if not matches:
                return f"{path} is not in the index (run code_index first)."
            entry = self.files[matches[0]]
        lines = [f"{entry.path} · {entry.lang} · {entry.lines} lines · {entry.size:,} bytes"]
        if entry.summary:
            lines.append(f"summary: {entry.summary}")
        if entry.imports:
            lines.append("imports: " + ", ".join(entry.imports[:24]))
        if entry.symbols:
            lines.append("")
            for symbol in entry.symbols[:max_items]:
                indent = "  " if symbol.parent else ""
                location = f"{symbol.line}-{symbol.end_line}" if symbol.end_line else str(symbol.line)
                doc = f" — {symbol.doc}" if symbol.doc else ""
                lines.append(f"{indent}{symbol.kind:<14} {symbol.qualified:<40} L{location}{doc}")
        else:
            lines.append("(no symbols parsed)")
        return "\n".join(lines)

    def search(self, query: str, limit: int = 20, *, content: bool = True) -> list[SearchHit]:
        query = (query or "").strip()
        if not query:
            return []
        terms = [term for term in re.findall(r"[\w\-]{2,}", query.lower()) if term]
        needle = query.lower()
        hits: list[SearchHit] = []
        seen: set[tuple[str, int]] = set()

        for entry in self.files.values():
            for symbol in entry.symbols:
                name = symbol.name.lower()
                qualified = symbol.qualified.lower()
                score = 0.0
                if name == needle or qualified == needle:
                    score += 120
                elif name.startswith(needle):
                    score += 80
                elif needle in qualified:
                    score += 50
                overlap = sum(1 for term in terms if term in qualified)
                score += overlap * 12
                if needle in entry.path.lower():
                    score += 18
                if score <= 0:
                    continue
                snippet = f"{symbol.kind} {symbol.qualified}" + (f" — {symbol.doc}" if symbol.doc else "")
                key = (entry.path, symbol.line)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(
                    SearchHit(entry.path, symbol.line, score, "symbol", snippet, symbol.qualified)
                )

        if content:
            root = Path(self.root)
            scanned = 0
            for entry in self.files.values():
                if entry.lang not in CODE_LANGS and entry.lang not in ("markdown", "yaml", "toml"):
                    continue
                scanned += 1
                if scanned > 900:
                    break
                text = _read_text(root / entry.path, 300_000)
                if not text:
                    continue
                lowered = text.lower()
                if needle not in lowered and not (terms and all(term in lowered for term in terms[:3])):
                    continue
                lines = text.splitlines()
                for index, line in enumerate(lines):
                    low = line.lower()
                    if needle in low or (terms and all(term in low for term in terms)):
                        score = 40 + (20 if needle in low else 0) + sum(6 for term in terms if term in low)
                        key = (entry.path, index + 1)
                        if key in seen:
                            continue
                        seen.add(key)
                        context = "\n".join(lines[max(0, index - 1) : index + 3])
                        hits.append(
                            SearchHit(entry.path, index + 1, score, "text", context[:500], "")
                        )

        hits.sort(key=lambda hit: (-hit.score, hit.path, hit.line))
        return hits[: max(1, limit)]

    def pack(self, paths: Iterable[str] | None = None, max_chars: int = 120_000) -> str:
        """Repomix-style packing of selected files for an LLM prompt."""
        selected = list(paths or self.files.keys())
        root = Path(self.root)
        chunks: list[str] = ["<repository>"]
        used = 0
        included = 0
        for relative in selected:
            entry = self.files.get(relative)
            if entry is None:
                continue
            text = _read_text(root / relative, 200_000)
            if not text:
                continue
            block = f'<file path="{relative}" language="{entry.lang}">\n{text}\n</file>'
            if used + len(block) > max_chars:
                chunks.append(f"<truncated files=\"{len(selected) - included}\" />")
                break
            chunks.append(block)
            used += len(block)
            included += 1
        chunks.append("</repository>")
        return "\n".join(chunks)


class CodeStore:
    """Load/build/cache a CodeIndex for one workspace."""

    def __init__(self, workspace: str, cache_dir: Path | None = None, *, max_files: int = 5000) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.cache_dir = cache_dir or CACHE_DIR
        self.max_files = max_files
        self._index: CodeIndex | None = None

    @property
    def index(self) -> CodeIndex | None:
        return self._index

    def load(self) -> CodeIndex | None:
        if self._index is not None:
            return self._index
        probe = CodeIndex(root=self.workspace)
        cached = CodeIndex.load(probe.cache_path(self.cache_dir))
        if cached and Path(cached.root) == Path(self.workspace):
            self._index = cached
        return self._index

    def ensure(self, force: bool = False, max_age: float = 1800.0) -> CodeIndex:
        if not force:
            cached = self.load()
            if cached is not None and (time.time() - cached.built_at) < max_age:
                return cached
        index = build_index(self.workspace, max_files=self.max_files)
        try:
            index.save(index.cache_path(self.cache_dir))
        except OSError:
            pass
        self._index = index
        return index

    def invalidate(self) -> None:
        self._index = None
        try:
            CodeIndex(root=self.workspace).cache_path(self.cache_dir).unlink()
        except OSError:
            pass
