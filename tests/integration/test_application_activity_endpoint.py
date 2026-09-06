import json
import uuid
import urllib.request
import urllib.error
import pytest
from lib import db
from .test_avatar_settings_endpoint import running_server, _post  # noqa: F401


def test_activity_reports_require_auth_and_are_sequence_fenced(running_server):
    body = dict(instance_id=str(uuid.uuid4()), sequence=1, foreground=True, input_age_ms=0, sent_at_ms=db.now_ms())
    request = urllib.request.Request(running_server + "/application-activity", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 401
    status, result = _post(running_server, "/application-activity", body)
    assert status == 200 and result["accepted"]
    assert not _post(running_server, "/application-activity", body)[1]["accepted"]
    assert _post(running_server, "/application-activity", {**body, "sequence": 2, "foreground": False})[1]["accepted"]
