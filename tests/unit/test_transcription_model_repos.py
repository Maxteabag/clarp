"""Which HuggingFace repo actually holds each faster-whisper conversion.

Systran publishes most of them but never built a turbo one, so the catalogue
offered `large-v3-turbo` while its download always failed with "Repository
Not Found". A catalogue entry that cannot install is worse than no entry.
"""
from __future__ import annotations

from lib.transcription_models import _faster_whisper_repo


def test_turbo_points_at_a_repo_that_exists():
    assert _faster_whisper_repo("large-v3-turbo") == (
        "deepdml/faster-whisper-large-v3-turbo-ct2")


def test_every_other_model_keeps_the_systran_default():
    for model in ("small.en", "base.en", "medium", "large-v3"):
        assert _faster_whisper_repo(model) == f"Systran/faster-whisper-{model}"


def test_every_catalogued_faster_whisper_model_resolves_to_a_repo():
    from lib.transcription_models import catalog_status
    for item in catalog_status():
        if item.get("provider") == "faster-whisper":
            assert _faster_whisper_repo(item["model"]).count("/") == 1
