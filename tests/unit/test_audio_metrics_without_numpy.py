"""Issue 83: audio metrics must degrade, not stop the server, without numpy."""
from lib import audio_metrics


def test_analyze_reports_unmeasured_and_never_flags_corruption(monkeypatch):
    monkeypatch.setattr(audio_metrics, "np", None)
    metrics = audio_metrics.analyze(b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav")
    assert metrics["unavailable"] == "numpy"
    assert metrics["bytes"] == 16
    assert audio_metrics.corruption_reasons(metrics, transcript="") == []


def test_module_imports_without_numpy(monkeypatch):
    import builtins, importlib, sys
    real_import = builtins.__import__

    def no_numpy(name, *args, **kwargs):
        if name == "numpy" or name.startswith("numpy."):
            raise ImportError("No module named 'numpy'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_numpy)
    monkeypatch.delitem(sys.modules, "lib.audio_metrics", raising=False)
    module = importlib.import_module("lib.audio_metrics")
    assert module.np is None
    monkeypatch.delitem(sys.modules, "lib.audio_metrics", raising=False)
