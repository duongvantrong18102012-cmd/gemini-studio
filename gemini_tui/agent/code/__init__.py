"""Gemini Code: structural index of a workspace.

A compact, dependency-free index (Python AST + conservative regex parsers)
that lets the agent find symbols and files without reading the whole repo.
"""

from __future__ import annotations

from .index import CodeIndex, CodeStore, FileEntry, SearchHit, build_index
from .review import ReviewFinding, review_files
from .tools import register_code_tools

__all__ = [
    "CodeIndex",
    "CodeStore",
    "FileEntry",
    "SearchHit",
    "build_index",
    "ReviewFinding",
    "review_files",
    "register_code_tools",
]
