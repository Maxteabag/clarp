import importlib


def _module(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLARP_CONFIG_DIR", str(tmp_path / "clarp"))
    from lib import deployment, server_peers
    importlib.reload(deployment)
    return importlib.reload(server_peers)


def test_peer_registry_does_not_expose_token(monkeypatch, tmp_path):
    peers = _module(monkeypatch, tmp_path)
    assert peers.add("work", "https://work.example", "secret") == {
        "name": "work", "url": "https://work.example", "enabled": True,
    }
    assert peers.list_public() == [{
        "name": "work", "url": "https://work.example", "enabled": True,
    }]
    assert "secret" not in str(peers.list_public())
    peers.remove("work")
    assert peers.list_public() == []
