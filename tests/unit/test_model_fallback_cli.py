import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "fallback_cli", Path(__file__).parents[2] / "scripts/configure_model_fallbacks.py"
)
# The real CLI loads this sibling module; tests import it without installation.
import sys

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_preview_never_writes_and_repeated_apply_skips_matches():
    calls = []
    models = [{"backend": "agy", "model": "gemini-3.8-flash-low", "effort": ""}]

    def request(method, path, body=None):
        calls.append((method, path, body))
        return {"models": models if path.endswith("same") else [], "revision": 4}

    result = module.configure(request, ["new", "same"], models)
    assert result["applied"] is False and result["changed"] == 1
    assert all(c[0] == "GET" for c in calls)
    calls = []

    def apply(method, path, body=None):
        if method == "POST":
            calls.append(body)
            return {"models": models, "revision": 5}
        return request(method, path, body)

    result = module.configure(apply, ["new", "same"], models, apply=True)
    writes = [c for c in calls if isinstance(c, dict)]
    assert (
        len(writes) == 1
        and writes[0]["session"] == "new"
        and writes[0]["expected_revision"] == 4
    )
