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


def test_release_is_validated_and_fences_late_requests(running_server):
    payload={"session":"rachel-7b4b", "detail_level":3, "items":[], "release":["released-view"]}
    assert _post(running_server,"/tool-explanations",payload)[1]['items']==[]
    payload.pop('release')
    payload['items']=[{'id':'1','demand_id':'released-view','activity':{'command':'ls'}}]
    assert _post(running_server,"/tool-explanations",payload)[1]['items'][0]['status']=='cancelled'
    payload['release']="invalid"
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(running_server,"/tool-explanations",payload)
    assert error.value.code==400
