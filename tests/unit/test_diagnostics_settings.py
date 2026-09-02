from lib import diagnostics_settings


def test_diagnostics_are_opt_in_by_default():
    from lib import settings_store
    settings_store.set_text(diagnostics_settings.KEY, "")
    diagnostics_settings.reset_for_tests()
    value = diagnostics_settings.get()
    assert value.enabled is False
    assert value.categories == frozenset()


def test_update_validates_and_persists_categories():
    value = diagnostics_settings.update({
        "enabled": True, "categories": ["requests", "database"],
    })
    diagnostics_settings.reset_for_tests()
    assert diagnostics_settings.get() == value
    assert diagnostics_settings.allows("requests") is True
    assert diagnostics_settings.allows("voice") is False


def test_unknown_category_is_rejected():
    try:
        diagnostics_settings.update({"enabled": True, "categories": ["secret"]})
    except ValueError as exc:
        assert "unknown diagnostic categories" in str(exc)
    else:
        raise AssertionError("unknown category accepted")


def test_cache_refreshes_cross_process_changes(monkeypatch):
    from lib import settings_store
    settings_store.set_text(
        diagnostics_settings.KEY,
        '{"enabled":true,"categories":["voice"]}')
    diagnostics_settings._CACHED = diagnostics_settings.Settings()
    diagnostics_settings._CACHED_AT = 10.0
    monkeypatch.setattr(diagnostics_settings.time, "monotonic", lambda: 13.0)
    assert diagnostics_settings.get().categories == frozenset({"voice"})


def test_special_client_events_have_independent_categories():
    assert diagnostics_settings.category_for(
        source="client", event="ios.performance.conversation-open") == "interactions"
    assert diagnostics_settings.category_for(
        source="client", event="ios.performance.device-resources") == "resources"
    assert diagnostics_settings.category_for(
        source="client", event="ios.performance.user-slow-report") == "feedback"
    diagnostics_settings.update({
        "enabled": True, "categories": ["feedback"],
    })
    assert diagnostics_settings.accepts_client_uploads() is True
    assert diagnostics_settings.allows_event(
        source="client", event="ios.performance.user-slow-report") is True
    assert diagnostics_settings.allows_event(
        source="client", event="ios.performance.frame-sample") is False
