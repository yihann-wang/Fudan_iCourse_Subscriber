"""Offline regression runs never put video receipts in the user's app state."""

import pytest


@pytest.fixture(autouse=True)
def isolated_video_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ICOURSE_STATE_DIR", str(tmp_path / "app-state"))
    monkeypatch.setenv("ICOURSE_HASH_CACHE_DIR", str(tmp_path / "hash-cache"))
