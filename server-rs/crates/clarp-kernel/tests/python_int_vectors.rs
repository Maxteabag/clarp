//! Decimal integer text, recorded with the Python Host's interpreter.
#![allow(clippy::unwrap_used)]
use serde::Deserialize;

#[derive(Deserialize)]
struct Case {
    prefix: String,
    unit: String,
    count: usize,
    suffix: String,
    expected: Option<String>,
}

#[test]
fn python_integer_text_matches_the_reference_interpreter() {
    let cases: Vec<Case> =
        serde_json::from_str(include_str!("fixtures/python-int-vectors.json")).unwrap();
    for case in &cases {
        let input = format!(
            "{}{}{}",
            case.prefix,
            case.unit.repeat(case.count),
            case.suffix
        );
        assert_eq!(
            clarp_kernel::python::int(&input).map(|n| n.to_string()),
            case.expected,
            "prefix {:?}, unit {:?}, count {}, suffix {:?}",
            case.prefix,
            case.unit,
            case.count,
            case.suffix
        );
    }
    assert_eq!(cases.len(), 610);
}
