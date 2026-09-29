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
