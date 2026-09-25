"""Characterization tests for lib.automation_settings (one durable bool)."""
from __future__ import annotations

from lib import automation_settings, settings_store


def test_default_is_off():
    assert automation_settings.get() == {"special_treatment": False}


def test_update_round_trip_and_return_shape():
    assert automation_settings.update(True) == {"special_treatment": True}
    assert automation_settings.get() == {"special_treatment": True}
    assert automation_settings.update(False) == {"special_treatment": False}
    assert automation_settings.get() == {"special_treatment": False}


def test_stored_under_documented_key_as_text():
    automation_settings.update(True)
    assert automation_settings.KEY == "automation_special_treatment"
    assert settings_store.get_text(automation_settings.KEY) == "true"


def test_truthy_non_bool_is_coerced():
    assert automation_settings.update(1)["special_treatment"] is True
    assert automation_settings.update("")["special_treatment"] is False
