import uuid

from lib import server_identity
from lib.settings_store import get_text, set_text


def test_server_identity_is_persisted_and_stable(monkeypatch):
    monkeypatch.setattr(server_identity.socket, "gethostname", lambda: "workstation")

    first = server_identity.get_server_info()
    second = server_identity.get_server_info()

    assert first == second
    assert first["name"] == "workstation"
    assert str(uuid.UUID(first["server_id"])) == first["server_id"]


def test_server_identity_prefers_configured_name(monkeypatch):
    monkeypatch.setattr(server_identity.socket, "gethostname", lambda: "workstation")
    set_text("server_display_name", "Desk")

    assert get_text("server_display_name") == "Desk"
    assert server_identity.get_server_info()["name"] == "Desk"


def test_server_identity_reports_container_image_version(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_IMAGE_VERSION", "v1.4.2")
    monkeypatch.setenv("CLARP_WORKSPACE_ROOT", "/data/workspace")
    monkeypatch.setenv("CLARP_SHARE_DIR", str(tmp_path))

    info = server_identity.get_server_info()

    assert info["deployment_mode"] == "container"
    assert info["version"] == "v1.4.2"
    assert info["image_version"] == "v1.4.2"
    assert info["default_cwd"] == "/data/workspace"


def test_server_identity_reports_native_deployed_version(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "native")
    monkeypatch.delenv("CLARP_IMAGE_VERSION", raising=False)
    monkeypatch.setenv("CLARP_SHARE_DIR", str(tmp_path))
    (tmp_path / "DEPLOYED_VERSION").write_text("abc1234\n")

    info = server_identity.get_server_info()

    assert info["deployment_mode"] == "native"
    assert info["version"] == "abc1234"
    assert info["image_version"] == ""


def test_server_identity_advertises_app_compatibility(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_SHARE_DIR", str(tmp_path))

    info = server_identity.get_server_info()

    assert info["min_app_version"] == server_identity.MIN_APP_VERSION
    # A real release version, not the DEPLOYED_VERSION git SHA.
    assert info["clarp_version"].split(".")[0].isdigit()
    assert info["clarp_version"] == server_identity.clarp_version()


def test_clarp_version_reads_pyproject_in_checkout_and_release_layouts():
    candidates = server_identity._pyproject_candidates()
    # server/lib/ -> repo root (checkout) and lib/ -> release root (installed).
    assert [c.parent.name for c in candidates][:2] == ["server", server_identity._pyproject_candidates()[1].parent.name]
    assert any(c.is_file() for c in candidates)
    assert server_identity.clarp_version() and server_identity.clarp_version()[0].isdigit()
