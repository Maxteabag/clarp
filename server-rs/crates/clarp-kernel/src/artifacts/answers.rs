//! What a form's answers may be: the part of JSON Schema this Host reads.
//!
//! A form says what an answer is with a JSON Schema (draft 2020-12). The Python Host asks a
//! library whether the schema is one and whether answers fit it, and tells the user the
//! library's own sentence when they do not. This Host reads the words of JSON Schema that
//! forms are written with ([`READ`]) and says the same sentence the library says first: it
//! asks a schema's words in the order they are written, and an answer's parts in the order
//! the schema names them.
//!
//! A word the library reads and this Host does not is refused by name when the form is made
//! ([`not_read`]): a form is never taken whose answers this Host would judge otherwise than
//! the Python Host. A word that means nothing to the library means nothing here.
//!
//! One thing the library itself leaves open: of several answers nobody asked for that do
//! not fit `additionalProperties`, it names whichever its set gives first. This Host names
//! the first as the answers were sent.

use std::cmp::Ordering;

use serde_json::{Map, Number, Value};

use super::{Refused, refuse};
use crate::python::repr;

/// The words that say what fits, which this Host reads.
pub const READ: [&str; 13] = [
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
];

/// Words the Python Host's library reads, or holds a schema to, which this Host does not.
const NOT_READ: [&str; 36] = [
    "$anchor",
    "$defs",
    "$dynamicAnchor",
    "$dynamicRef",
    "$id",
    "$recursiveAnchor",
    "$recursiveRef",
    "$ref",
    "$vocabulary",
    "allOf",
    "anyOf",
    "contains",
    "contentSchema",
    "definitions",
    "dependencies",
    "dependentRequired",
    "dependentSchemas",
    "else",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "if",
    "maxContains",
    "maxProperties",
    "minContains",
    "minProperties",
    "multipleOf",
    "not",
    "oneOf",
    "pattern",
    "patternProperties",
    "prefixItems",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
    "uniqueItems",
];

/// The kinds of value a schema names.
const TYPES: [&str; 7] = [
    "array", "boolean", "integer", "null", "number", "object", "string",
];

/// What is said of a schema with a word this Host does not read.
pub fn not_read(word: &str) -> String {
    format!(
        "answer_schema says `{word}`, which this Host does not read yet (the Rust Host). It \
         reads {} and {}",
        READ[..READ.len() - 1].join(", "),
        READ[READ.len() - 1]
    )
}

/// A whole number that is not negative, as a schema writes a length: `2`, and `2.0` too.
fn count(value: &Value) -> Option<f64> {
    let number = value.as_f64()?;
    (value.is_number() && number >= 0.0 && number.fract() == 0.0).then_some(number)
}

fn all_different(values: &[Value]) -> bool {
    values
        .iter()
        .enumerate()
        .all(|(place, value)| !values[..place].iter().any(|before| equal(before, value)))
}

/// Whether this is a schema, as far as the words go that this Host reads: what the library
/// asks of each of them (the schema of schemas of draft 2020-12).
fn is_schema(schema: &Value) -> bool {
    let words = match schema {
        Value::Bool(_) => return true,
        Value::Object(words) => words,
        _ => return false,
    };
    let a_type = |name: &Value| name.as_str().is_some_and(|name| TYPES.contains(&name));
    words.iter().all(|(word, value)| match word.as_str() {
        "type" => match value {
            Value::Array(names) => {
                !names.is_empty() && names.iter().all(a_type) && all_different(names)
            }
            name => a_type(name),
        },
        "properties" => value
            .as_object()
            .is_some_and(|schemas| schemas.values().all(is_schema)),
        "required" => value
            .as_array()
            .is_some_and(|names| names.iter().all(Value::is_string) && all_different(names)),
        "additionalProperties" | "items" => is_schema(value),
        "enum" | "examples" => value.is_array(),
        "minLength" | "maxLength" | "minItems" | "maxItems" => count(value).is_some(),
        "minimum" | "maximum" => value.is_number(),
        "title" | "description" | "$comment" | "format" | "$schema" | "contentMediaType"
        | "contentEncoding" => value.is_string(),
        "deprecated" | "readOnly" | "writeOnly" => value.is_boolean(),
        // `const` and `default` are any value; any other word is not asked about.
        _ => true,
    })
}

/// The first word of a schema, or of a schema within it, that this Host does not read.
fn unread(schema: &Value) -> Option<&'static str> {
    let words = schema.as_object()?;
    words.iter().find_map(|(word, value)| {
        if let Some(known) = NOT_READ.iter().find(|known| **known == word.as_str()) {
            return Some(*known);
        }
        match word.as_str() {
            "properties" => value.as_object()?.values().find_map(unread),
            "additionalProperties" | "items" => unread(value),
            _ => None,
        }
    })
}

/// Refuses what is not a schema, and a schema with a word this Host does not read.
pub fn check(schema: &Value) -> Result<(), Refused> {
    if !is_schema(schema) {
        return refuse("invalid answer schema");
    }
    match unread(schema) {
        Some(word) => refuse(not_read(word)),
        None => Ok(()),
    }
}

/// A number as a whole one, when it is one: `2`, and `2.0` too.
fn whole(number: &Number) -> Option<i128> {
    if let Some(whole) = number.as_i64() {
        return Some(i128::from(whole));
    }
    if let Some(whole) = number.as_u64() {
        return Some(i128::from(whole));
    }
    let fraction = number.as_f64()?;
    // Every fraction this large is a whole number, and none is beyond what this holds.
    (fraction.fract() == 0.0 && fraction.abs() < 1e38).then_some(fraction as i128)
}

/// Which of two numbers is the greater, as numbers: a whole number and a fraction are
/// compared for what they are, not for how they are written.
fn compared(one: &Number, two: &Number) -> Ordering {
    match (whole(one), whole(two)) {
        (Some(one), Some(two)) => one.cmp(&two),
        _ => {
            let (one, two) = (one.as_f64().unwrap_or(0.0), two.as_f64().unwrap_or(0.0));
            one.partial_cmp(&two).unwrap_or(Ordering::Equal)
        }
    }
}

/// Whether two values are the same to the library: `1` and `1.0` are, `true` and `1` are
/// not, and lists and objects are when their parts are.
fn equal(one: &Value, two: &Value) -> bool {
    match (one, two) {
        (Value::Number(one), Value::Number(two)) => compared(one, two) == Ordering::Equal,
        (Value::Array(one), Value::Array(two)) => {
            one.len() == two.len() && one.iter().zip(two).all(|(one, two)| equal(one, two))
        }
        (Value::Object(one), Value::Object(two)) => {
            one.len() == two.len()
                && one
                    .iter()
                    .all(|(key, value)| two.get(key).is_some_and(|other| equal(value, other)))
        }
        (one, two) => one == two,
    }
}

fn is_type(instance: &Value, name: &str) -> bool {
    match name {
        "array" => instance.is_array(),
        "boolean" => instance.is_boolean(),
        "integer" => instance.as_number().is_some_and(|number| {
            number.is_i64() || number.is_u64() || number.as_f64().is_some_and(|n| n.fract() == 0.0)
        }),
        "null" => instance.is_null(),
        "number" => instance.is_number(),
        "object" => instance.is_object(),
        "string" => instance.is_string(),
        _ => false,
    }
}

/// How long a text or a list is against a length a schema names.
fn length(count: usize, limit: &Value) -> Option<Ordering> {
    let limit = limit.as_number()?;
    Some(compared(&Number::from(count), limit))
}

/// What one word of a schema finds wrong with an answer, if anything.
fn asked(
    word: &str,
    value: &Value,
    instance: &Value,
    words: &Map<String, Value>,
) -> Option<String> {
    let shown = || repr(instance);
    match word {
        "type" => {
            let names: Vec<&Value> = match value {
                Value::Array(names) => names.iter().collect(),
                name => vec![name],
            };
            let fits = |name: &&Value| name.as_str().is_some_and(|name| is_type(instance, name));
            if names.iter().any(fits) {
                return None;
            }
            let names: Vec<String> = names.into_iter().map(repr).collect();
            Some(format!("{} is not of type {}", shown(), names.join(", ")))
        }
        "properties" => {
            let (schemas, answers) = (value.as_object()?, instance.as_object()?);
            schemas
                .iter()
                .find_map(|(name, schema)| first_error(schema, answers.get(name)?))
        }
        "required" => {
            let answers = instance.as_object()?;
            let missing = value
                .as_array()?
                .iter()
                .find(|name| name.as_str().is_none_or(|name| !answers.contains_key(name)))?;
            Some(format!("{} is a required property", repr(missing)))
        }
        "additionalProperties" => {
            let answers = instance.as_object()?;
            let named = words.get("properties").and_then(Value::as_object);
            let mut extras: Vec<&String> = answers
                .keys()
                .filter(|name| !named.is_some_and(|named| named.contains_key(*name)))
                .collect();
            match value {
                Value::Object(_) => extras
                    .into_iter()
                    .find_map(|name| first_error(value, answers.get(name)?)),
                Value::Bool(false) if !extras.is_empty() => {
                    extras.sort();
                    let verb = if extras.len() == 1 { "was" } else { "were" };
                    let extras: Vec<String> = extras
                        .into_iter()
                        .map(|name| crate::python::text(name))
                        .collect();
                    Some(format!(
                        "Additional properties are not allowed ({} {verb} unexpected)",
                        extras.join(", ")
                    ))
                }
                _ => None,
            }
        }
        "items" => {
            let items = instance.as_array()?;
            match (value, items.as_slice()) {
                (_, []) => None,
                (Value::Bool(false), [one]) => Some(format!(
                    "Expected at most 0 items but found 1 extra: {}",
                    repr(one)
                )),
                (Value::Bool(false), many) => Some(format!(
                    "Expected at most 0 items but found {} extra: {}",
                    many.len(),
                    shown()
                )),
                (schema, items) => items.iter().find_map(|item| first_error(schema, item)),
            }
        }
        "enum" => {
            let among = value.as_array()?;
            (!among.iter().any(|each| equal(each, instance)))
                .then(|| format!("{} is not one of {}", shown(), repr(value)))
        }
        "const" => (!equal(instance, value)).then(|| format!("{} was expected", repr(value))),
        "minLength" | "minItems" => {
            let count = match (word, instance) {
                ("minLength", Value::String(text)) => text.chars().count(),
                ("minItems", Value::Array(items)) => items.len(),
                _ => return None,
            };
            if length(count, value)? != Ordering::Less {
                return None;
            }
            let one = length(1, value) == Some(Ordering::Equal);
            let said = if one {
                "should be non-empty"
            } else {
                "is too short"
            };
            Some(format!("{} {said}", shown()))
        }
        "maxLength" | "maxItems" => {
            let count = match (word, instance) {
                ("maxLength", Value::String(text)) => text.chars().count(),
                ("maxItems", Value::Array(items)) => items.len(),
                _ => return None,
            };
            if length(count, value)? != Ordering::Greater {
                return None;
            }
            let none = length(0, value) == Some(Ordering::Equal);
            let said = if none {
                "is expected to be empty"
            } else {
                "is too long"
            };
            Some(format!("{} {said}", shown()))
        }
        "minimum" | "maximum" => {
            let (number, limit) = (instance.as_number()?, value.as_number()?);
            let (beyond, said) = if word == "minimum" {
                (Ordering::Less, "less than the minimum")
            } else {
                (Ordering::Greater, "greater than the maximum")
            };
            (compared(number, limit) == beyond)
                .then(|| format!("{} is {said} of {}", shown(), repr(value)))
        }
        _ => None,
    }
}

/// The first thing a schema finds wrong with an answer, in the library's words: `None` when
/// the answer fits. The schema is one [`check`] took.
pub fn first_error(schema: &Value, instance: &Value) -> Option<String> {
    match schema {
        Value::Bool(false) => Some(format!("False schema does not allow {}", repr(instance))),
        Value::Object(words) => words
            .iter()
            .find_map(|(word, value)| asked(word, value, instance, words)),
        _ => None,
    }
}

/// What a user is told whose answers do not fit: the library's sentence, as far as its first
/// line and three hundred signs go.
pub fn unfit(schema: &Value, answers: &Value) -> Option<String> {
    let said = first_error(schema, answers)?;
    let line = said.split('\n').next().unwrap_or("");
    Some(line.chars().take(300).collect())
}
