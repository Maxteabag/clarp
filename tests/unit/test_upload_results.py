from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import upload_results  # noqa: E402


def test_same_upload_id_and_content_returns_original_path(tmp_path):
    first = upload_results.store(
        session_dir=tmp_path, upload_id="share-123:0", name="report.pdf",
        content_type="application/pdf", blob=b"pdf bytes")
    second = upload_results.store(
        session_dir=tmp_path, upload_id="share-123:0", name="report.pdf",
        content_type="application/pdf", blob=b"pdf bytes")

    assert second == first
    assert first.read_bytes() == b"pdf bytes"
    assert len(list(tmp_path.glob("u-*-report.pdf"))) == 1


@pytest.mark.parametrize("changed", ["blob", "name", "type"])
def test_upload_id_collision_is_rejected(tmp_path, changed):
    upload_results.store(
        session_dir=tmp_path, upload_id="share-123:0", name="report.pdf",
        content_type="application/pdf", blob=b"pdf bytes")

    with pytest.raises(upload_results.UploadIDCollisionError):
        upload_results.store(
            session_dir=tmp_path, upload_id="share-123:0",
            name="other.pdf" if changed == "name" else "report.pdf",
            content_type="text/plain" if changed == "type" else "application/pdf",
            blob=b"different" if changed == "blob" else b"pdf bytes")


def test_retry_recovers_blob_written_before_record(tmp_path):
    upload_id = "share-recover:0"
    key = __import__("hashlib").sha256(upload_id.encode()).hexdigest()
    recovered = tmp_path / f"u-{key[:24]}-report.pdf"
    recovered.write_bytes(b"pdf bytes")

    result = upload_results.store(
        session_dir=tmp_path, upload_id=upload_id, name="report.pdf",
        content_type="application/pdf", blob=b"pdf bytes")

    assert result == recovered
    record = json.loads(next((tmp_path / ".upload-records").glob("*.json")).read_text())
    assert record["upload_id"] == upload_id


def test_upload_id_cannot_move_to_another_session(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    upload_results.store(
        session_dir=first, record_root=tmp_path, upload_id="share-123:0",
        name="report.pdf", content_type="application/pdf", blob=b"pdf bytes")
    with pytest.raises(upload_results.UploadIDCollisionError):
        upload_results.store(
            session_dir=second, record_root=tmp_path, upload_id="share-123:0",
            name="report.pdf", content_type="application/pdf", blob=b"pdf bytes")


@pytest.mark.parametrize("value", ["", "has space", "x" * 161, "../escape"])
def test_invalid_upload_id_is_rejected(value):
    with pytest.raises(ValueError):
        upload_results.normalize_upload_id(value)
