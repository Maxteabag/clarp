"""A transcription model's removal deletes only the folders its own id gives (R112).

remove() deleted the path stored in the model's registry record when it was the managed root or
anything below it: a wrong record removed every managed model, or another model's folder. Now
the stored path decides nothing about what is deleted: the folders the id gives are, when they
are real and directly inside the managed root, and the registry line goes. A stored path that
names anything else is said once in the log; it never blocks a removal, so a registered model
can always be removed. Refused, before any file or the registry changes: an id folder that is a
symbolic link or resolves outside the root. The cancel cleanup follows the same rule. All on a
scratch models directory with three models."""
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


@pytest.fixture
def logged(monkeypatch):
    lines = []
    from lib import log as log_module
    monkeypatch.setattr(log_module, "log", lambda event, detail="", *a, **k: lines.append((event, detail)))
    return lines


def _stored_shape(shape, root, own, tmp_path):
    if shape == "the root":
        return str(root.resolve())
    if shape == "a sibling":
        return str(tm._managed_root("faster-whisper:alpha").resolve())
    if shape == "deeper":
        return str((own / "inside").resolve())
    if shape == "outer":
        return str(tmp_path.resolve())
    if shape == "relative":
        return "faster-whisper--beta"
    if shape == "empty":
        return ""
    if shape == "missing":
        return None
    if shape == "another model's cache folder":
        other = root / "models--Systran--faster-whisper-alpha"
        other.mkdir()
        (other / "model.bin").write_text("x")
        return str(other.resolve())
    link = root / "beta-link"                                       # through a link to a sibling
    os.symlink(tm._managed_root("faster-whisper:alpha"), link)
    return str(link)


@pytest.mark.parametrize("shape", ["the root", "a sibling", "deeper", "outer", "relative", "empty",
                                   "missing", "a link out", "another model's cache folder"])
def test_whatever_the_stored_path_names_the_removal_deletes_only_its_own_folder(
        models, tmp_path, logged, shape):
    """The ruling of 20 Nov: the stored path decides nothing, and must not block a removal."""
    root, registry = models
    own = tm._managed_root("faster-whisper:beta")
    _set_stored(registry, "faster-whisper:beta", _stored_shape(shape, root, own, tmp_path))
    tm.remove("faster-whisper:beta", allow_active=True)
    assert not own.exists()                                          # its own folder only
    assert [r["id"] for r in json.loads(registry.read_text())] == ["faster-whisper:alpha", "faster-whisper:gamma"]
    for model_id in ("faster-whisper:alpha", "faster-whisper:gamma"):
        assert (tm._managed_root(model_id) / "model.bin").is_file()
    if shape == "another model's cache folder":
        assert (root / "models--Systran--faster-whisper-alpha" / "model.bin").is_file()
    assert logged.count(("transcriptionModelFolderRefused", "model=faster-whisper:beta reason=stored_path")) == 1


def test_a_stored_path_that_is_exactly_its_own_folder_logs_nothing(models, logged):
    tm.remove("faster-whisper:beta", allow_active=True)
    assert not [line for line in logged if line[0] == "transcriptionModelFolderRefused"]


def _beta_is_a_link_elsewhere(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (real / name).write_text("x")
    own = tm._managed_root("faster-whisper:beta")
    import shutil
    shutil.rmtree(own)
    os.symlink(real, own)
    return real


def test_an_id_folder_that_is_a_link_is_refused_and_nothing_changes(models, tmp_path, logged):
    root, registry = models
    real = _beta_is_a_link_elsewhere(tmp_path)
    before_registry, before = registry.read_text(), _folders(root)
    with pytest.raises(ValueError, match="faster-whisper:beta") as refused:
        tm.remove("faster-whisper:beta", allow_active=True)
    assert registry.read_text() == before_registry and _folders(root) == before
    assert (real / "model.bin").is_file()                           # nothing deleted through the link
    assert str(real) not in str(refused.value) and str(root) not in str(refused.value)
    assert ("transcriptionModelFolderRefused", "model=faster-whisper:beta reason=link") in logged


def _rollback(model_id):
    """The cancel cleanup's own path, with the job claim stood in (it only calls the cleanup)."""
    from lib import background_jobs
    real = background_jobs.run_terminal_generation_cleanup
    background_jobs.run_terminal_generation_cleanup = lambda job_id, *, generation, cleanup, statuses: (True, cleanup())
    try:
        return tm.rollback_terminal_install(model_id, job_id="job", generation=1)
    finally:
        background_jobs.run_terminal_generation_cleanup = real


def test_a_cancel_with_no_record_removes_the_half_installed_id_folder(models):
    root, registry = models
    half = tm._managed_root("faster-whisper:delta")
    half.mkdir()
    (half / "model.bin.part").write_text("half")                    # a download stopped half way
    assert _rollback("faster-whisper:delta") is True
    assert not half.exists() and _folders(root) == ["faster-whisper--alpha", "faster-whisper--beta",
                                                    "faster-whisper--gamma"]


def test_a_cancel_with_no_record_refuses_an_id_folder_that_is_a_link_elsewhere(models, tmp_path, logged):
    """The reviewer's hole: with no record, main refused a folder outside the managed root and the
    first head followed the link and removed it."""
    root, registry = models
    real = tmp_path / "outside"
    real.mkdir()
    for name in ("a", "b", "c"):
        (real / name).write_text("x")
    os.symlink(real, tm._managed_root("faster-whisper:delta"))
    with pytest.raises(ValueError, match="faster-whisper:delta"):
        _rollback("faster-whisper:delta")
    assert sorted(p.name for p in real.iterdir()) == ["a", "b", "c"]
    assert tm._managed_root("faster-whisper:delta").is_symlink()
    assert ("transcriptionModelFolderRefused", "model=faster-whisper:delta reason=link") in logged


def test_a_cancel_with_a_record_follows_the_removals_rule(models, tmp_path):
    root, registry = models
    _set_stored(registry, "faster-whisper:beta", str(root.resolve()))  # a wrong stored path
    assert _rollback("faster-whisper:beta") is True
    assert not tm._managed_root("faster-whisper:beta").exists()
    assert _folders(root) == ["faster-whisper--alpha", "faster-whisper--gamma"]


def test_a_faster_whisper_model_kept_in_its_own_hugging_face_cache_folder_is_removed(models):
    """The other folder a faster-whisper id gives: its repo's cache folder under the managed root."""
    root, registry = models
    cache = root / "models--Systran--faster-whisper-beta"
    cache.mkdir()
    (cache / "model.bin").write_text("x")
    _set_stored(registry, "faster-whisper:beta", str(cache.resolve()))
    tm.remove("faster-whisper:beta", allow_active=True)
    assert not cache.exists() and (tm._managed_root("faster-whisper:alpha") / "model.bin").is_file()


def test_the_cache_folder_name_is_faster_whispers_alone(models, tmp_path):
    """Another provider's id gives no models--<repo> folder: one of that name is left."""
    root, registry = models
    # No faster-whisper id is registered for "delta": the folder below is no one's but by the name
    # a faster-whisper id of the same model would give it.
    folder = tm._managed_root("openai-whisper:delta")
    folder.mkdir()
    with open(folder / "model.pt", "wb") as weights:                # a model register() accepts
        weights.truncate(1_000_001)
    tm.register("openai-whisper:delta", str(folder / "model.pt"), managed_root=str(folder))
    cache = root / "models--Systran--faster-whisper-delta"
    cache.mkdir()
    (cache / "model.bin").write_text("x")
    tm.remove("openai-whisper:delta", allow_active=True)
    assert not folder.exists() and (cache / "model.bin").is_file()


def test_a_cache_folder_two_registered_ids_share_is_left_to_the_other(models, monkeypatch):
    root, registry = models
    monkeypatch.setitem(tm._FASTER_WHISPER_REPOS, "beta-alias", "Systran/faster-whisper-beta")
    folder = tm._managed_root("faster-whisper:beta-alias")
    folder.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (folder / name).write_text("x")
    tm.register("faster-whisper:beta-alias", str(folder), managed_root=str(folder))
    cache = root / "models--Systran--faster-whisper-beta"
    cache.mkdir()
    (cache / "model.bin").write_text("x")
    tm.remove("faster-whisper:beta", allow_active=True)
    assert (cache / "model.bin").is_file() and folder.is_dir()


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
