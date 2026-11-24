#!/usr/bin/env python
"""Gemini Studio — launch the terminal dashboard.

Usage:
    python tui.py
    python tui.py --workspace . --model gemini-3.5-flash-lite
    python tui.py --no-splash --approval readonly
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gemini_tui.app import GeminiStudio
from gemini_tui.config import APP_TITLE, APP_VERSION, load_dotenv_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gemini-studio",
        description=f"{APP_TITLE} {APP_VERSION} — a local-first Gemini dashboard for the terminal.",
    )
    parser.add_argument(
        "-w",
        "--workspace",
        default=None,
        help="Directory the agent works in (default: current directory).",
    )
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help="Gemini model id to start with (default: gemini-3.5-flash-lite).",
    )
    parser.add_argument(
        "--no-splash",
        action="store_true",
        help="Skip the boot animation.",
    )
    parser.add_argument(
        "--approval",
        choices=["ask", "auto", "readonly"],
        default=None,
        help="How write tools are approved: ask (default), auto, readonly.",
    )
    parser.add_argument(
        "--no-animations",
        action="store_true",
        help="Disable ambient animation (useful over slow SSH links).",
    )
    parser.add_argument(
        "--computer",
        action="store_true",
        help="Enable desktop control at launch (input still asks for approval).",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Disable ambient memory injection into chat turns.",
    )
    parser.add_argument(
        "--no-audit",
        action="store_true",
        help="Disable the JSONL audit log for this run.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_TITLE} {APP_VERSION}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv_files()

    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    if not workspace.is_dir():
        print(f"error: workspace is not a directory: {workspace}", file=sys.stderr)
        return 2

    app = GeminiStudio(
        workspace=str(workspace),
        model=args.model,
        splash=not args.no_splash,
        computer=True if args.computer else None,
        memory=False if args.no_memory else None,
        audit=False if args.no_audit else None,
    )
    if args.approval:
        app.settings.approval_mode = args.approval
    if args.no_animations:
        app.settings.animations = False
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
