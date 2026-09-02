import io
import json

from lib import cartesia_voices


class _Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *_args): return False


def test_catalog_pages_english_voices_and_marks_occupied(monkeypatch, tmp_path):
    cartesia_voices._cache = None
    monkeypatch.setattr(
        cartesia_voices.config, "load",
        lambda: type("Cfg", (), {
            "tts_provider": "cartesia",
            "cartesia_key": lambda self: "secret",
            "cartesia_voice_for": lambda self, persona: "v1" if persona == "Mike" else "",
        })())
    pages = [
        {"data": [{"id": "v1", "name": "Skylar", "language": "en",
                   "preview_file_url": "https://example/one"}],
         "has_more": True, "next_page": "v1"},
        {"data": [{"id": "v2", "name": "Henry", "language": "en"}],
         "has_more": False, "next_page": None},
    ]
    monkeypatch.setattr(
        cartesia_voices.urllib.request, "urlopen",
        lambda request, timeout: _Response(json.dumps(pages.pop(0)).encode()))
    monkeypatch.setattr(cartesia_voices, "load_agents", lambda path: {
        "mike": {"name": "Mike", "voice_id": '{"cartesia":"v1"}'},
    })

    result = cartesia_voices.catalog(tmp_path / "agents.json")

    assert [voice["id"] for voice in result["voices"]] == ["v1", "v2"]
    assert result["voices"][0]["taken_by"] == "Mike"
    assert result["voices"][0]["selection_value"] == '{"cartesia":"v1"}'
