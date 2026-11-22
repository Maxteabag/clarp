//! Rules that depend on more than one field of a clarp.form v2 component or form.

use std::collections::{BTreeMap, BTreeSet};

use super::Refusal;
use super::json::{self, J, Obj, index};
use super::validate::Checks;

fn array(value: Option<&J>) -> &[J] {
    match value {
        Some(J::Array(items)) => items,
        _ => &[],
    }
}

pub(super) fn component(
    checks: &Checks<'_>,
    rule: &str,
    props: &J,
    at: &str,
    buttons: &mut BTreeSet<String>,
) -> Result<(), Refusal> {
    match rule {
        "chart" => chart(props, at),
        "table" => table(checks, props, at),
        "button" => button(checks, props, at, buttons),
        _ => Ok(()),
    }
}

fn button(
    checks: &Checks<'_>,
    props: &J,
    at: &str,
    buttons: &mut BTreeSet<String>,
) -> Result<(), Refusal> {
    let Some(action) = props
        .get("action")
        .and_then(J::str)
        .filter(|action| checks.catalog.ident.matches(action))
    else {
        return Err(Refusal::new("prop", &format!("{at}/action")));
    };
    if !buttons.insert(action.to_string()) {
        return Err(Refusal::new(
            "button_action_duplicate",
            &format!("{at}/action"),
        ));
    }
    Ok(())
}

fn chart(props: &J, at: &str) -> Result<(), Refusal> {
    let series_at = format!("{at}/series");
    let series = array(props.get("series"));
    let kind = props.get("kind").and_then(J::str);
    if kind == Some("pie") && series.len() != 1 {
        return Err(Refusal::new("chart_pie", &series_at));
    }
    let total: usize = series
        .iter()
        .map(|one| array(one.get("points")).len())
        .sum();
    if total > 600 {
        return Err(Refusal::new("chart_points", &series_at));
    }
    let mut numeric_x = None;
    for (i, series) in series.iter().enumerate() {
        let at = format!("{}/points", index(&series_at, i));
        let points = array(series.get("points"));
        if kind == Some("bar") && points.len() > 60 {
            return Err(Refusal::new("chart_points", &at));
        }
        if kind == Some("pie") && points.len() > 12 {
            return Err(Refusal::new("chart_pie", &at));
        }
        let mut labels = BTreeSet::new();
        let mut numbers = BTreeSet::new();
        let mut previous = None;
        for (j, point) in points.iter().enumerate() {
            let point_at = index(&at, j);
            let x_at = format!("{point_at}/x");
            let x = point.get("x").unwrap_or(&J::Null);
            let numeric = match x {
                J::Int(_) => true,
                J::Text(text) if (1..=60).contains(&text.len()) => false,
                _ => return Err(Refusal::new("prop", &x_at)),
            };
            if *numeric_x.get_or_insert(numeric) != numeric {
                return Err(Refusal::new("chart_x_mixed", &x_at));
            }
            match x {
                J::Int(number) => {
                    if !numbers.insert(*number) {
                        return Err(Refusal::new("chart_x_duplicate", &x_at));
                    }
                    if previous.is_some_and(|last| last > *number) {
                        return Err(Refusal::new("chart_x_order", &x_at));
                    }
                    previous = Some(*number);
                }
                J::Text(text) => {
                    if !labels.insert(text.as_str().unwrap_or_default()) {
                        return Err(Refusal::new("chart_x_duplicate", &x_at));
                    }
                }
                _ => {}
            }
            if kind == Some("pie") && matches!(point.get("y"), Some(J::Int(y)) if *y < 0) {
                return Err(Refusal::new("chart_pie", &format!("{point_at}/y")));
            }
        }
    }
    Ok(())
}

fn table(checks: &Checks<'_>, props: &J, at: &str) -> Result<(), Refusal> {
    let columns = array(props.get("columns"));
    let rows = array(props.get("rows"));
    let rows_at = format!("{at}/rows");
    let mut keys = BTreeSet::new();
    for (i, column) in columns.iter().enumerate() {
        let key_at = format!("{at}/columns/{i}/key");
        let Some(key) = column
            .get("key")
            .and_then(J::str)
            .filter(|key| checks.catalog.ident.matches(key))
        else {
            return Err(Refusal::new("prop", &key_at));
        };
        if !keys.insert(key) {
            return Err(Refusal::new("prop", &key_at));
        }
    }
    if rows.len() * columns.len() > 1000 {
        return Err(Refusal::new("table_size", &rows_at));
    }
    for (i, row) in rows.iter().enumerate() {
        let Some(row) = row.object() else {
            continue;
        };
        let row_at = index(&rows_at, i);
        for key in json::ordered(row)? {
            if !key.as_str().is_some_and(|key| keys.contains(key)) {
                return Err(Refusal::new("unknown_key", &checks.step(&row_at, key)));
            }
        }
    }
    Ok(())
}

pub(super) fn button_rules(
    schema: &Obj,
    buttons: &BTreeSet<String>,
    bound: &BTreeMap<String, String>,
) -> Result<(), Refusal> {
    let wrong = || Refusal::new("action_schema", "/answer_schema/properties/action");
    let Some(rule) = schema
        .get("properties")
        .and_then(|props| props.get("action"))
        .and_then(J::object)
    else {
        return Err(wrong());
    };
    let values = array(rule.get("enum"));
    let required = array(schema.get("required"))
        .iter()
        .any(|key| key.str() == Some("action"));
    if rule.len() != 2
        || rule.get("type").and_then(J::str) != Some("string")
        || values.len() != buttons.len()
        || !values
            .iter()
            .all(|value| value.str().is_some_and(|action| buttons.contains(action)))
        || !required
    {
        return Err(wrong());
    }
    if let Some(path) = bound.get("action") {
        return Err(Refusal::new("action_bound", path));
    }
    Ok(())
}

/// The button labels by action, read from the already validated payload.
pub(super) fn labels(payload: &J) -> BTreeMap<&str, &str> {
    if payload.get("catalog_version") != Some(&J::Int(2)) {
        return BTreeMap::new();
    }
    payload
        .get("spec")
        .and_then(|spec| spec.get("elements"))
        .and_then(J::object)
        .into_iter()
        .flat_map(Obj::iter)
        .filter_map(|(_, node)| {
            if node.get("type")?.str()? != "button" {
                return None;
            }
            let props = node.get("props")?;
            Some((props.get("action")?.str()?, props.get("label")?.str()?))
        })
        .collect()
}
