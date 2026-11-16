//! Where the owner's phone is.
//!
//! The phone says where it is when its user shares it, by itself or because an agent asked
//! (`POST /location`); the agent's tools read the last place said for its session
//! (`GET /location`). One place for each session, the newest. The rules and their words are
//! the Python Host's (`server/lib/location.py`, `routes/ops.py`).

use serde_json::{Value, json};

use crate::python;

/// A place a phone said it is at.
#[derive(Debug, Clone, PartialEq)]
pub struct Fix {
    pub lat: f64,
    pub lng: f64,
    /// How far off it may be, in metres, when the phone said.
    pub accuracy: Option<f64>,
    /// When the Host was told, in milliseconds.
    pub ts: i64,
}

/// What is said when the two numbers of a place are not both there.
pub const NEEDED: &str = "lat/lng required";
/// What is said when no session is named.
pub const WHOSE: &str = "session required";

/// A number as Python's `float()` takes one from what JSON carries: a number, a text that
/// is one, a truth value. `Err` says what was given, where it was a text. (Python also
/// takes digits grouped with `_`, "1_0" for ten; no app writes a place that way, and here
/// it is no number.)
fn number(given: &Value) -> Result<f64, Option<String>> {
    match given {
        Value::Number(number) => number.as_f64().ok_or(None),
        Value::Bool(truth) => Ok(f64::from(u8::from(*truth))),
        Value::String(text) => python::strip(text)
            .parse::<f64>()
            .map_err(|_| Some(python::repr(given))),
        _ => Err(None),
    }
}

impl Fix {
    /// The place an app says its phone is at, from the body it sent, when it is one. The
    /// error is what the app is told (400).
    pub fn said(body: &Value, now_ms: i64) -> Result<Self, String> {
        let (Ok(lat), Ok(lng)) = (number(&body["lat"]), number(&body["lng"])) else {
            return Err(NEEDED.to_string());
        };
        let accuracy = match &body["accuracy"] {
            Value::Null => None,
            given => Some(number(given).map_err(|text| match text {
                Some(text) => format!("could not convert string to float: {text}"),
                None => "accuracy must be a number".to_string(),
            })?),
        };
        if !lat.is_finite() || !(-90.0..=90.0).contains(&lat) {
            return Err("latitude must be between -90 and 90".to_string());
        }
        if !lng.is_finite() || !(-180.0..=180.0).contains(&lng) {
            return Err("longitude must be between -180 and 180".to_string());
        }
        if accuracy.is_some_and(|accuracy| !accuracy.is_finite() || accuracy < 0.0) {
            return Err("accuracy must be non-negative".to_string());
        }
        Ok(Self {
            lat,
            lng,
            accuracy,
            ts: now_ms,
        })
    }

    /// As it is told to whoever asks for it.
    pub fn told(&self) -> Value {
        json!({ "lat": self.lat, "lng": self.lng, "accuracy": self.accuracy, "ts": self.ts })
    }

    /// As it is kept.
    pub fn kept(&self) -> Vec<u8> {
        self.told().to_string().into_bytes()
    }

    /// From what was kept.
    pub fn read(kept: &[u8]) -> Option<Self> {
        let kept: Value = serde_json::from_slice(kept).ok()?;
        Some(Self {
            lat: kept["lat"].as_f64()?,
            lng: kept["lng"].as_f64()?,
            accuracy: kept["accuracy"].as_f64(),
            ts: kept["ts"].as_i64()?,
        })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_place_is_taken_as_the_python_host_takes_it() {
        let fix = Fix::said(&json!({ "lat": 59, "lng": "10.75", "accuracy": 12.5 }), 7).unwrap();
        assert_eq!(
            fix.told().to_string(),
            r#"{"lat":59.0,"lng":10.75,"accuracy":12.5,"ts":7}"#
        );
        assert_eq!(Fix::read(&fix.kept()), Some(fix));
        // No accuracy is none; a truth value is a number, as Python's `float` has it.
        let bare = Fix::said(&json!({ "lat": true, "lng": " -180 " }), 9).unwrap();
        assert_eq!((bare.lat, bare.lng, bare.accuracy), (1.0, -180.0, None));
        assert_eq!(Fix::read(&bare.kept()), Some(bare));
        for (body, why) in [
            (json!({}), NEEDED),
            (json!({ "lat": 1 }), NEEDED),
            (json!({ "lat": null, "lng": 1 }), NEEDED),
            (json!({ "lat": "north", "lng": 1 }), NEEDED),
            (json!({ "lat": [1], "lng": 1 }), NEEDED),
            (
                json!({ "lat": 90.01, "lng": 1 }),
                "latitude must be between -90 and 90",
            ),
            (
                json!({ "lat": "nan", "lng": 1 }),
                "latitude must be between -90 and 90",
            ),
            (
                json!({ "lat": -91, "lng": 1 }),
                "latitude must be between -90 and 90",
            ),
            (
                json!({ "lat": 1, "lng": 180.5 }),
                "longitude must be between -180 and 180",
            ),
            (
                json!({ "lat": 1, "lng": "inf" }),
                "longitude must be between -180 and 180",
            ),
            (
                json!({ "lat": 1, "lng": 1, "accuracy": -1 }),
                "accuracy must be non-negative",
            ),
            (
                json!({ "lat": 1, "lng": 1, "accuracy": "inf" }),
                "accuracy must be non-negative",
            ),
            (
                json!({ "lat": 1, "lng": 1, "accuracy": "about ten" }),
                "could not convert string to float: 'about ten'",
            ),
        ] {
            assert_eq!(Fix::said(&body, 1), Err(why.to_string()), "{body}");
        }
        // The edges are places.
        assert!(Fix::said(&json!({ "lat": -90, "lng": 180, "accuracy": 0 }), 1).is_ok());
    }
}
