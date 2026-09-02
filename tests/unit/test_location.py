from __future__ import annotations

import pytest

from lib import location


def test_location_round_trip_replaces_previous_fix():
    location.set_location("arnold", 59.9139, 10.7522, 18.5, ts=100)
    location.set_location("arnold", -27.5949, -48.5482, 7.0, ts=200)

    assert location.get_location("arnold") == {
        "lat": -27.5949,
        "lng": -48.5482,
        "accuracy": 7.0,
        "ts": 200,
    }


@pytest.mark.parametrize(
    ("lat", "lng", "accuracy"),
    [
        (91, 0, None),
        (-91, 0, None),
        (0, 181, None),
        (0, -181, None),
        (0, 0, -1),
        (float("nan"), 0, None),
    ],
)
def test_location_rejects_invalid_coordinates(lat, lng, accuracy):
    with pytest.raises(ValueError):
        location.set_location("arnold", lat, lng, accuracy)


def test_location_requires_session():
    with pytest.raises(ValueError, match="session required"):
        location.set_location(" ", 59.9, 10.7)
    assert location.get_location("") is None
