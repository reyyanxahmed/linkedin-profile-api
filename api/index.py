"""Vercel serverless entrypoint.

Vercel's Python runtime imports this module and serves the ASGI app it exposes.
Everything else lives in `app/`; this file only adapts the deployment surface.

Two things differ from running `uvicorn app.main:app` locally, both forced by the
serverless environment:

1. The filesystem is read-only except /tmp, so the rotated-cookie store is pointed
   there. State survives within a warm instance and is lost on a cold start, which
   is a documented limitation rather than a bug — see the README.
2. There is no local Redis, so the cache falls back to in-memory (per instance).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The repo root holds the `app` package; Vercel's working directory is not
# guaranteed to be on sys.path.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Must be set before app.config is imported, since Settings reads the environment
# at construction time.
os.environ.setdefault("COOKIE_STATE_PATH", "/tmp/cookie_state.json")

from app.main import app  # noqa: E402  (path setup must run first)

__all__ = ["app"]
