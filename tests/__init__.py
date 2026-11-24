"""Test bootstrap: isolate all persisted state and never touch the network.

`config.py` computes its paths at import time, so the environment is set here
before any `gemini_tui` module is imported by a test module.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="gemini-tui-tests-"))
os.environ["GEMINI_TUI_HOME"] = str(HOME)
os.environ["GEMINI_TUI_TESTING"] = "1"
for _key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY", "GEMINI_TUI_API_KEY"):
    os.environ.pop(_key, None)
