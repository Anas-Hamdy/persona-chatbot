"""
Storage for "personalized vs generic" reply comparisons (see
chatbot_engine.ChatEngine.compare()) - the quantitative evidence that the
implicit profile actually changes the model's output, not just a claim.

Each record is one comparison run: the user's message, the personalized
reply, the generic (unconditioned) reply, both system prompts used, and a
word-overlap divergence score between the two replies (see app.py's
_text_divergence()). /admin/stats aggregates these into a headline
"average divergence" metric and a recent-comparisons table.

This is intentionally a separate, much smaller store than ProfileStore
(profiler.py) - a comparison record is a one-off measurement, not
continuously-updated per-user state, and mixing the two would make the
profile JSON grow unboundedly every time someone runs a comparison.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional


class ComparisonLog:
    """Storage backend interface. Both methods are `async def` for the
    same reason ProfileStore's are (see profiler.py's docstring): the
    Cloudflare KV-backed implementation's calls are Promise-based and must
    be awaited, so a single async interface is used everywhere rather than
    special-casing one backend."""

    async def append(self, record: dict) -> None:
        raise NotImplementedError

    async def list_all(self, limit: int = 200) -> list[dict]:
        raise NotImplementedError


class FileComparisonLog(ComparisonLog):
    """Default backend: one JSON file per comparison under `storage_dir`.
    Mirrors profiler.FileProfileStore - fine for `python app.py` / a
    single-process gunicorn deployment, not usable on Cloudflare Workers
    (no persistent local filesystem there)."""

    def __init__(self, storage_dir: str | Path = "comparisons"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    async def append(self, record: dict) -> None:
        record = dict(record)
        record.setdefault("timestamp", time.time())
        # Sortable-by-name filename (zero-padded timestamp) so list_all()
        # can page through files in recency order without opening each one
        # first just to read its timestamp.
        filename = f"{int(record['timestamp'] * 1000):015d}_{uuid.uuid4().hex[:8]}.json"
        (self.storage_dir / filename).write_text(json.dumps(record, indent=2))

    async def list_all(self, limit: int = 200) -> list[dict]:
        files = sorted(self.storage_dir.glob("*.json"), reverse=True)[:limit]
        records = []
        for f in files:
            try:
                records.append(json.loads(f.read_text()))
            except Exception:
                continue
        return records
