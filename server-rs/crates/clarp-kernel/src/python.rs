//! Text as the Python Host writes it, where its exact form is part of what an agent reads.

use serde_json::Value;

/// A value as Python's `json.dumps` writes it: a space after every comma and colon, and
/// letters of every alphabet as themselves.
pub fn dumps(value: &Value) -> String {
    match value {
        Value::Array(items) => {
            let items: Vec<String> = items.iter().map(dumps).collect();
            format!("[{}]", items.join(", "))
        }
        Value::Object(fields) => {
            let fields: Vec<String> = fields
                .iter()
                .map(|(key, value)| format!("{}: {}", Value::String(key.clone()), dumps(value)))
                .collect();
            format!("{{{}}}", fields.join(", "))
        }
        plain => plain.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_value_is_written_with_pythons_spaces() {
        assert_eq!(dumps(&json!(null)), "null");
        assert_eq!(
            dumps(&json!({ "key": "build-1", "n": [1, 2.5], "ok": true, "né": "ü" })),
            r#"{"key": "build-1", "n": [1, 2.5], "ok": true, "né": "ü"}"#
        );
        assert_eq!(
            dumps(&json!([["notes.md", 3], {}])),
            r#"[["notes.md", 3], {}]"#
        );
    }
}
