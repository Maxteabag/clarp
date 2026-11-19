"""A transcription model's removal deletes only the folder its own id gives (R112).

remove() deleted the path stored in the model's registry record when it was the managed root or
anything below it: a wrong record removed every managed model, or another model's folder. Now the
stored path must be exactly the id's folder; anything else is refused before any file or the
registry changes. All on a scratch models directory with three models."""
from __future__ import annotations

import json
import os

import pytest

from lib import transcription_models as tm

IDS = ("faster-whisper:alpha", "faster-whisper:beta", "faster-whisper:gamma")


@pytest.fixture
def models(tmp_path, monkeypatch):
    root = tmp_path / "scratch-models"
    registry = tmp_path / "scratch-registry.json"
    monkeypatch.setattr(tm, "MANAGED_MODELS", root)
    monkeypatch.setattr(tm, "REGISTRY", registry)
    assert str(tm.MANAGED_MODELS).startswith(str(tmp_path))       # never a real models directory
    for model_id in IDS:
        folder = tm._managed_root(model_id)
        folder.mkdir(parents=True)
        for name in ("config.json", "model.bin", "tokenizer.json"):
            (folder / name).write_text("x")
        tm.register(model_id, str(folder), managed_root=str(folder))
    return root, registry


def _folders(root):
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _set_stored(registry, model_id, stored):
    records = json.loads(registry.read_text())
    for record in records:
        if record["id"] == model_id:
            record["_managed_root"] = stored
    registry.write_text(json.dumps(records))


def test_an_ordinary_removal_deletes_exactly_its_own_folder_and_registry_line(models):
    root, registry = models
    tm.remove("faster-whisper:beta", allow_active=True)
    assert _folders(root) == ["faster-whisper--alpha", "faster-whisper--gamma"]
    assert [r["id"] for r in json.loads(registry.read_text())] == ["faster-whisper:alpha", "faster-whisper:gamma"]


@pytest.mark.parametrize("shape", ["the root", "a sibling", "deeper", "outer", "relative", "empty",
                                   "a link out", "a link in"])
def test_a_stored_path_that_is_not_exactly_its_own_folder_is_refused_and_nothing_changes(
        models, tmp_path, shape):
    root, registry = models
    own = tm._managed_root("faster-whisper:beta")
    if shape == "the root":
        stored = str(root.resolve())
    elif shape == "a sibling":
        stored = str(tm._managed_root("faster-whisper:alpha").resolve())
    elif shape == "deeper":
        stored = str((own / "inside").resolve())
    elif shape == "outer":
        stored = str(tmp_path.resolve())
    elif shape == "relative":
        stored = "faster-whisper--beta"
    elif shape == "empty":
        stored = ""
    elif shape == "a link out":                                     # the stored path leaves through a link
        link = root / "beta-link"
        os.symlink(tm._managed_root("faster-whisper:alpha"), link)
        stored = str(link)
    else:                                                           # the id's folder itself is a link
        real = tmp_path / "elsewhere"
        real.mkdir()
        for name in ("config.json", "model.bin", "tokenizer.json"):
            (real / name).write_text("x")
        import shutil
        shutil.rmtree(own)
        os.symlink(real, own)
        stored = str(own)
    _set_stored(registry, "faster-whisper:beta", stored)
    before_registry = registry.read_text()
    before = _folders(root)
    with pytest.raises(ValueError, match="faster-whisper:beta"):
        tm.remove("faster-whisper:beta", allow_active=True)
    assert registry.read_text() == before_registry                  # the registry unchanged
    assert _folders(root) == before                                 # every model intact
    for model_id in ("faster-whisper:alpha", "faster-whisper:gamma"):
        assert (tm._managed_root(model_id) / "model.bin").is_file()
    if shape == "a link in":
        assert (tmp_path / "elsewhere" / "model.bin").is_file()     # nothing deleted through the link


def test_the_refusal_names_the_model_not_the_paths(models):
    root, registry = models
    _set_stored(registry, "faster-whisper:beta", str(root.resolve()))
    with pytest.raises(ValueError) as refused:
        tm.remove("faster-whisper:beta", allow_active=True)
    assert "faster-whisper:beta" in str(refused.value) and str(root) not in str(refused.value)


def test_a_faster_whisper_model_kept_in_its_own_hugging_face_cache_folder_is_removed(models):
    """The other folder a faster-whisper id gives: its repo's cache folder under the managed root."""
    root, registry = models
    cache = root / "models--Systran--faster-whisper-beta"
    cache.mkdir()
    (cache / "model.bin").write_text("x")
    _set_stored(registry, "faster-whisper:beta", str(cache.resolve()))
    tm.remove("faster-whisper:beta", allow_active=True)
    assert not cache.exists() and (tm._managed_root("faster-whisper:alpha") / "model.bin").is_file()


def test_another_models_hugging_face_cache_folder_is_refused(models):
    root, registry = models
    other = root / "models--Systran--faster-whisper-alpha"
    other.mkdir()
    (other / "model.bin").write_text("x")
    _set_stored(registry, "faster-whisper:beta", str(other.resolve()))
    with pytest.raises(ValueError, match="faster-whisper:beta"):
        tm.remove("faster-whisper:beta", allow_active=True)
    assert (other / "model.bin").is_file()


def test_what_is_deleted_is_the_folder_the_id_gives_never_the_stored_path(tmp_path, monkeypatch):
    """The managed root reached through a link: the stored path (the resolved one install
    records) and the id's folder name the same directory by different paths. The removal
    deletes the id's folder; the stored path is only compared with it."""
    real = tmp_path / "real-models"
    real.mkdir()
    root = tmp_path / "models-link"
    os.symlink(real, root)
    monkeypatch.setattr(tm, "MANAGED_MODELS", root)
    monkeypatch.setattr(tm, "REGISTRY", tmp_path / "scratch-registry.json")
    folder = tm._managed_root("faster-whisper:beta")
    folder.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (folder / name).write_text("x")
    tm.register("faster-whisper:beta", str(folder), managed_root=str(folder))
    stored = json.loads((tmp_path / "scratch-registry.json").read_text())[0]["_managed_root"]
    assert stored == str(real / "faster-whisper--beta")              # install stores it resolved
    deleted = []
    real_rmtree = tm.shutil.rmtree
    monkeypatch.setattr(tm.shutil, "rmtree", lambda path, *a, **k: (deleted.append(path), real_rmtree(path))[1])
    tm.remove("faster-whisper:beta", allow_active=True)
    assert deleted == [folder] and str(deleted[0]) != stored and not (real / "faster-whisper--beta").exists()
