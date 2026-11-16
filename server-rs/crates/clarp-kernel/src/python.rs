//! Text as the Python Host writes it, where its exact form is part of what an agent reads.

use std::fmt::Write as _;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Number, Value};

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

/// A fraction as Python writes one (`repr(float)`): the fewest digits that are this number
/// and no other, written out between a ten-thousandth and ten thousand million million, and
/// with an exponent of at least two digits and its sign outside of that.
pub fn float(number: f64) -> String {
    if number.is_nan() {
        return "nan".to_string();
    }
    if number.is_infinite() {
        return if number > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    // The fewest digits, as `d.ddde<exponent>`.
    let written = format!("{:e}", number.abs());
    let (mantissa, exponent) = written.split_once('e').unwrap_or((&written, "0"));
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let exponent: i32 = exponent.parse().unwrap_or(0);
    // Where the point is, counted from before the first digit.
    let point = exponent + 1;
    let sign = if number.is_sign_negative() { "-" } else { "" };
    if number == 0.0 {
        return format!("{sign}0.0");
    }
    if -4 < point && point <= 16 {
        let zeros = |count: i32| "0".repeat(usize::try_from(count).unwrap_or(0));
        let before = usize::try_from(point).unwrap_or(0);
        let body = if point <= 0 {
            format!("0.{}{digits}", zeros(-point))
        } else if before >= digits.len() {
            format!("{digits}{}.0", "0".repeat(before - digits.len()))
        } else {
            let (whole, part) = digits.split_at(before);
            format!("{whole}.{part}")
        };
        return format!("{sign}{body}");
    }
    let (first, rest) = digits.split_at(1);
    let rest = if rest.is_empty() {
        String::new()
    } else {
        format!(".{rest}")
    };
    let to = point - 1;
    let mark = if to < 0 { '-' } else { '+' };
    format!("{sign}{first}{rest}e{mark}{:02}", to.unsigned_abs())
}

/// A number as Python writes one: a whole number as it is, a fraction as [`float`] does.
pub fn number(number: &Number) -> String {
    match number.as_f64() {
        Some(fraction) if number.is_f64() => float(fraction),
        _ => number.to_string(),
    }
}

// What Python does not print as itself (`str.isprintable`): the signs Unicode calls Other or
// Separator, the space excepted.
static UNPRINTED: LazyLock<Regex> = LazyLock::new(|| {
    // Written here, not read from anywhere: one that does not compile is a mistake in this
    // file, which the tests of this module would not get past.
    #[allow(clippy::expect_used)]
    Regex::new(r"^[\p{C}\p{Z}]$").expect("a pattern of this module compiles")
});

fn printed(sign: char) -> bool {
    let mut buffer = [0u8; 4];
    sign == ' ' || !UNPRINTED.is_match(sign.encode_utf8(&mut buffer))
}

/// A text as Python shows one (`repr(str)`): between single quotes, or double ones when it
/// has a single quote and no double one; a sign that is not printed as its number.
pub fn text(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut shown = String::with_capacity(text.len() + 2);
    shown.push(quote);
    for sign in text.chars() {
        match sign {
            '\\' => shown.push_str("\\\\"),
            '\t' => shown.push_str("\\t"),
            '\n' => shown.push_str("\\n"),
            '\r' => shown.push_str("\\r"),
            sign if sign == quote => {
                shown.push('\\');
                shown.push(sign);
            }
            sign if printed(sign) => shown.push(sign),
            sign => {
                let code = u32::from(sign);
                let _ = match code {
                    0..=0xff => write!(shown, "\\x{code:02x}"),
                    0x100..=0xffff => write!(shown, "\\u{code:04x}"),
                    _ => write!(shown, "\\U{code:08x}"),
                };
            }
        }
    }
    shown.push(quote);
    shown
}

/// A value as Python shows one (`repr` of what `json.loads` made of it): how a sentence of
/// the Python Host names a value.
pub fn repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(written) => number(written),
        Value::String(written) => text(written),
        Value::Array(items) => {
            let items: Vec<String> = items.iter().map(repr).collect();
            format!("[{}]", items.join(", "))
        }
        Value::Object(fields) => {
            let fields: Vec<String> = fields
                .iter()
                .map(|(key, value)| format!("{}: {}", text(key), repr(value)))
                .collect();
            format!("{{{}}}", fields.join(", "))
        }
    }
}

/// A text between the quotes of JSON as Python writes it by default: nothing but the signs
/// of ASCII that are printed, every other sign as its number, two numbers for a sign beyond
/// the first sixty-five thousand.
fn ascii_text(text: &str, out: &mut String) {
    out.push('"');
    for sign in text.chars() {
        match sign {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(sign),
            sign => {
                let mut units = [0u16; 2];
                for unit in sign.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');
}

fn ascii_into(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(truth) => out.push_str(if *truth { "true" } else { "false" }),
        Value::Number(written) => out.push_str(&number(written)),
        Value::String(written) => ascii_text(written, out),
        Value::Array(items) => {
            out.push('[');
            for (place, item) in items.iter().enumerate() {
                if place > 0 {
                    out.push(',');
                }
                ascii_into(item, out);
            }
            out.push(']');
        }
        Value::Object(fields) => {
            let mut fields: Vec<(&String, &Value)> = fields.iter().collect();
            fields.sort_by(|a, b| a.0.cmp(b.0));
            out.push('{');
            for (place, (key, value)) in fields.into_iter().enumerate() {
                if place > 0 {
                    out.push(',');
                }
                ascii_text(key, out);
                out.push(':');
                ascii_into(value, out);
            }
            out.push('}');
        }
    }
}

/// A value as Python's `json.dumps(value, sort_keys=True, separators=(',', ':'))` writes it:
/// its fields in the order of their names, nothing between them, in ASCII. The form in which
/// answers are kept, weighed and shown to their agent.
pub fn sorted_ascii(value: &Value) -> String {
    let mut out = String::new();
    ascii_into(value, &mut out);
    out
}

/// A text as Python's `int()` reads it: the space around it left out, a sign, digits, and
/// an underscore only between two digits. `None`: Python says it is no whole number. A
/// number too long to count with is read as the largest there is.
pub fn int(text: &str) -> Option<i128> {
    let text = clarp_speech::listen::strip(text);
    let (negative, digits) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.strip_prefix('+').unwrap_or(text)),
    };
    let signs: Vec<char> = digits.chars().collect();
    let mut plain = String::with_capacity(signs.len());
    for (at, sign) in signs.iter().enumerate() {
        let digit = |place: Option<&char>| place.is_some_and(char::is_ascii_digit);
        match sign {
            '_' if at > 0 && digit(signs.get(at - 1)) && digit(signs.get(at + 1)) => {}
            sign if sign.is_ascii_digit() => plain.push(*sign),
            _ => return None,
        }
    }
    if plain.is_empty() {
        return None;
    }
    let number = plain.parse::<i128>().unwrap_or(i128::MAX);
    Some(if negative { -number } else { number })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_text_is_a_whole_number_when_python_says_so() {
        assert_eq!(int("41"), Some(41));
        assert_eq!(int(" \t+4_1\n"), Some(41));
        assert_eq!(int("-0"), Some(0));
        assert_eq!(int("-12"), Some(-12));
        assert_eq!(int("007"), Some(7));
        assert_eq!(int(&"9".repeat(60)), Some(i128::MAX));
        for no_number in [
            "", " ", "4 1", "41.0", "1e3", "4__1", "_41", "41_", "-", "+-1", "0x1f",
        ] {
            assert_eq!(int(no_number), None, "{no_number:?}");
        }
    }

    #[test]
    fn a_fraction_is_read_as_the_number_it_is_and_written_as_python_writes_it() {
        // Read from JSON and written again: the same digits, in Python's form.
        for written in [
            "9.999999900000001e-05",
            "0.30000000000000004",
            "1.7976931348623157e+308",
            "5e-324",
            "1e+16",
            "1234567890123456.0",
            "1.2345678901234568e+16",
            "0.0001",
            "1e-05",
            "-0.0",
            "2.5",
            "100.0",
        ] {
            let read: Value = serde_json::from_str(written).unwrap();
            assert_eq!(repr(&read), written);
        }
        assert_eq!(
            repr(&json!(18_446_744_073_709_551_615_u64)),
            "18446744073709551615"
        );
        assert_eq!(repr(&json!(-7)), "-7");
    }

    #[test]
    fn a_text_is_shown_between_the_quotes_python_chooses() {
        assert_eq!(text("plain"), "'plain'");
        assert_eq!(text("it's"), "\"it's\"");
        assert_eq!(text("say \"yes\""), "'say \"yes\"'");
        assert_eq!(text("both ' and \""), "'both \\' and \"'");
        assert_eq!(text("tab\there\n"), "'tab\\there\\n'");
        assert_eq!(text("nul\u{0}"), "'nul\\x00'");
        assert_eq!(text("no-break\u{a0}space"), "'no-break\\xa0space'");
        assert_eq!(text("zero\u{200b}width"), "'zero\\u200bwidth'");
        assert_eq!(text("blåbær 🎉"), "'blåbær 🎉'");
        assert_eq!(
            repr(&json!({ "a": [1, 2.0, null, true], "it's": "x" })),
            "{'a': [1, 2.0, None, True], \"it's\": 'x'}"
        );
        assert_eq!(
            sorted_ascii(&json!({ "b": "blåbær 🎉", "a": [1.0, 1e16], "\u{7f}": null })),
            r#"{"a":[1.0,1e+16],"b":"bl\u00e5b\u00e6r \ud83c\udf89","\u007f":null}"#
        );
    }

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
