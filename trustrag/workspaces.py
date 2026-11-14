"""Per-user private workspaces: every user gets their own documents, index, cache and traces.

A user can never retrieve another user's documents because each workspace is a separate
TrustRAG instance rooted in its own directory (data/users/<user_id>/).
"""
from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path
from threading import Lock

from .config import ROOT, Settings, get_settings
from .service import TrustRAG


class WorkspaceManager:
    def __init__(self, base: Path | None = None, settings: Settings | None = None):
        self.base_settings = settings or get_settings()
        self.base = Path(base or self.base_settings.docs_dir.parent / "users")
        self._cache: dict[str, TrustRAG] = {}
        self._lock = Lock()

    def path(self, user_id: str) -> Path:
        if not user_id.isalnum():  # ids are uuid hex; refuse anything path-like
            raise ValueError("bad user id")
        return self.base / user_id

    def get(self, user_id: str) -> TrustRAG:
        with self._lock:
            if user_id not in self._cache:
                root = self.path(user_id)
                s = dataclasses.replace(
                    self.base_settings, docs_dir=root / "docs", index_dir=root / "index",
                    trace_file=root / "traces.jsonl", feedback_file=root / "feedback.jsonl")
                self._cache[user_id] = TrustRAG(s, auto_ingest=False)
            return self._cache[user_id]

    def sample_docs_dir(self) -> Path:
        return ROOT / "data" / "docs"

    def delete(self, user_id: str) -> None:
        with self._lock:
            self._cache.pop(user_id, None)
        shutil.rmtree(self.path(user_id), ignore_errors=True)
