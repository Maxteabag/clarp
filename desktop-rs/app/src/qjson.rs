//! serde_json → Qt JSON, so list roles hand QML real arrays and objects.

use cxx_qt_lib::{QJsonArray, QJsonObject, QJsonValue, QString};
use serde_json::Value;

pub fn to_qjson(value: &Value) -> QJsonValue {
    match value {
        Value::Null => QJsonValue::default(),
        Value::Bool(b) => QJsonValue::from(*b),
        Value::Number(n) => n
            .as_i64()
            .map_or_else(|| QJsonValue::from(n.as_f64().unwrap_or(0.0)), QJsonValue::from),
        Value::String(s) => QJsonValue::from(&QString::from(s.as_str())),
        Value::Array(items) => QJsonValue::from(&to_qjson_array(items)),
        Value::Object(map) => {
            let mut object = QJsonObject::default();
            for (key, item) in map {
                object.insert(&QString::from(key.as_str()), &to_qjson(item));
            }
            QJsonValue::from(&object)
        }
    }
}

pub fn to_qjson_array(items: &[Value]) -> QJsonArray {
    let mut array = QJsonArray::default();
    for item in items {
        array.append(&to_qjson(item));
    }
    array
}

/// Qt JSON → serde_json (numbers that are whole become integers).
pub fn from_qjson(value: &QJsonValue) -> Value {
    if value.is_bool() {
        Value::Bool(value.to_bool())
    } else if value.is_double() {
        let number = value.to_double();
        if number.fract() == 0.0 && number.abs() < 9.0e15 {
            Value::from(number as i64)
        } else {
            serde_json::Number::from_f64(number).map_or(Value::Null, Value::Number)
        }
    } else if value.is_string() {
        Value::String(value.to_string().to_string())
    } else if value.is_array() {
        Value::Array(value.to_array().iter().map(|item| from_qjson(&item)).collect())
    } else if value.is_object() {
        Value::Object(from_qjson_object(&value.to_object()))
    } else {
        Value::Null
    }
}

pub fn from_qjson_object(object: &QJsonObject) -> serde_json::Map<String, Value> {
    let keys = cxx_qt_lib::QList::<QString>::from(&object.keys());
    keys.iter().map(|key| (key.to_string(), from_qjson(&object.value(key)))).collect()
}
