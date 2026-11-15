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

/// A value in the one form two writers agree on: its fields in the order of their names,
/// nothing between them. What a digest of a value is taken of.
pub fn canonical(value: &Value) -> String {
    match value {
        Value::Array(items) => {
            let items: Vec<String> = items.iter().map(canonical).collect();
            format!("[{}]", items.join(","))
        }
        Value::Object(fields) => {
            let mut fields: Vec<(&String, &Value)> = fields.iter().collect();
            fields.sort_by(|a, b| a.0.cmp(b.0));
            let fields: Vec<String> = fields
                .into_iter()
                .map(|(key, value)| format!("{}:{}", Value::String(key.clone()), canonical(value)))
                .collect();
            format!("{{{}}}", fields.join(","))
        }
        plain => plain.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_value_has_one_form_for_a_digest() {
        assert_eq!(
            canonical(&json!({ "b": [1, { "z": null, "a": "x" }], "a": true })),
            r#"{"a":true,"b":[1,{"a":"x","z":null}]}"#
        );
    }

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
