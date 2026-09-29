//! Lenient field access matching the C++ client's QJsonValue helpers: a field
//! of the wrong type reads as its empty value instead of failing the row.

use serde_json::{Map, Value};

pub type Object = Map<String, Value>;

pub fn string(object: &Object, key: &str) -> String {
    object.get(key).and_then(Value::as_str).unwrap_or_default().to_owned()
}

/// QJsonValue::toInteger: any number, truncated only when it is integral.
pub fn integer(object: &Object, key: &str) -> i64 {
    match object.get(key) {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().filter(|f| f.fract() == 0.0).map(|f| f as i64))
            .unwrap_or(0),
        _ => 0,
    }
}

pub fn boolean(object: &Object, key: &str) -> bool {
    object.get(key).and_then(Value::as_bool).unwrap_or(false)
}

pub fn array(object: &Object, key: &str) -> Vec<Value> {
    object.get(key).and_then(Value::as_array).cloned().unwrap_or_default()
}

pub fn object(object: &Object, key: &str) -> Object {
    object.get(key).and_then(Value::as_object).cloned().unwrap_or_default()
}

/// JavaScript truthiness for the reducers ported from static/lib.
pub fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0 && !f.is_nan()),
        Some(Value::String(s)) => !s.is_empty(),
        Some(_) => true,
    }
}

/// `Number(x) || 0` from the JavaScript reducers.
pub fn js_number(value: Option<&Value>) -> f64 {
    let n = match value {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(Value::String(s)) => {
            let t = s.trim();
            if t.is_empty() { 0.0 } else { t.parse().unwrap_or(f64::NAN) }
        }
        Some(Value::Bool(b)) => f64::from(u8::from(*b)),
        _ => 0.0,
    };
    if n.is_nan() { 0.0 } else { n }
}

/// `String(x || '')` from the JavaScript reducers.
pub fn js_string(value: Option<&Value>) -> String {
    if !truthy(value) {
        return String::new();
    }
    match value {
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
        None => String::new(),
    }
}
