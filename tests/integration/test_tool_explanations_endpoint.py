"""Use the existing authenticated local HTTP fixture; never invoke real Codex."""
import json
import urllib.error
import urllib.request

import pytest

from .test_avatar_settings_endpoint import running_server, _post, _get  # noqa: F401


def test_developer_mode_and_capability(running_server):
    _, info = _get(running_server, "/server-info")
    assert "tool_explanations" in info["capabilities"]["features"]
    _, response = _post(running_server, "/tool-explanations", {
        "session": "rachel-7b4b", "detail_level": 0,
        "items": [{"id": "1", "activity": {"command": "ls"}}],
    })
    assert response["items"] == [{"id": "1", "status": "disabled"}]


def test_endpoint_requires_authentication(running_server):
    request = urllib.request.Request(running_server + "/tool-explanations", data=json.dumps({}).encode(), headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 401


def test_unknown_session_and_bad_level(running_server):
    for payload, status in [
        ({"session": "missing", "detail_level": 0, "items": []}, 404),
        ({"session": "rachel-7b4b", "detail_level": True, "items": []}, 400),
    ]:
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(running_server, "/tool-explanations", payload)
        assert error.value.code == status
