//! Native forms (`render_spec`, issue 428) in their chat card: the engine's
//! form as rows the card draws (iOS `RenderSpecFormController`), and what
//! the card's controls do.
//!
//! Slint draws no recursive tree, so the form's elements become a flat list
//! of rows: a vertical stack's children one after another, a card's children
//! in its box (its title first), a horizontal stack of words side by side.
//! A horizontal stack holding a control lays its controls out one under
//! another, as iOS does at large text sizes. Each card keeps one row model
//! for as long as the window runs, changed in place, so a text field being
//! typed in is never rebuilt under the keyboard.

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::rc::Rc;

use clarp_core::render_spec::{Accepted, Answer, Element, Form, Gap, Kind, TextSource, Values, footnote};
use clarp_engine::{Change, FormState, FormView};
use slint::{ComponentHandle, Model, ModelRc, VecModel};

use crate::{App, AppWindow, ArtifactBridge, ArtifactItem, ArtifactOption, FormLeaf, FormRow};

/// A row as built, compared before the model is touched.
#[derive(Debug, Clone, PartialEq, Default)]
struct Leaf {
    id: String,
    kind: &'static str,
    text: String,
    level: i32,
    placeholder: String,
    multiline: bool,
    key: String,
    value: String,
    on: bool,
    options: Vec<(String, String)>,
    chosen: i32,
    required: bool,
    note: String,
}

#[derive(Debug, Clone, PartialEq, Default)]
struct Row {
    leaf: Leaf,
    cells: Vec<Leaf>,
    in_card: bool,
    card_top: bool,
    card_bottom: bool,
}

struct Shown {
    model: Rc<VecModel<FormRow>>,
    rows: Vec<Row>,
}

thread_local! {
    /// Each form card's rows, by artifact.
    static SHOWN: RefCell<HashMap<String, Shown>> = RefCell::default();
}

/// What the leaves need besides the form: the values shown and the gaps.
struct Context<'a> {
    form: &'a Form,
    values: &'a Values,
    read_only: bool,
    short: BTreeMap<String, usize>,
}

impl Context<'_> {
    fn text(&self, key: &str) -> String {
        match self.values.get(key) {
            Some(Answer::Str(text)) => text.clone(),
            Some(other) if self.read_only => other.words(),
            _ => String::new(),
        }
    }

    fn leaf(&self, element: &Element) -> Leaf {
        let mut leaf = Leaf { id: element.id.clone(), chosen: -1, ..Leaf::default() };
        match &element.kind {
            Kind::Heading { text, level } => {
                leaf.kind = "heading";
                leaf.text = text.clone();
                leaf.level = i32::from(*level);
            }
            Kind::Text(TextSource::Literal(text)) => {
                leaf.kind = "text";
                leaf.text = text.clone();
            }
            Kind::Text(TextSource::State(key)) => {
                leaf.kind = "text";
                leaf.text = self.values.get(key).map(Answer::words).unwrap_or_default();
            }
            Kind::TextField { label, placeholder, multiline, key } => {
                leaf.kind = "field";
                leaf.text = label.clone();
                leaf.placeholder = placeholder.clone().unwrap_or_default();
                leaf.multiline = *multiline;
                leaf.key = key.clone();
                leaf.value = self.text(key);
                leaf.required = self.form.required.contains(key);
                if let Some(min) = self.short.get(key) {
                    leaf.note = format!("At least {min} characters.");
                }
            }
            Kind::Toggle { label, key } => {
                leaf.kind = "toggle";
                leaf.text = label.clone();
                leaf.key = key.clone();
                leaf.on = self.values.get(key) == Some(&Answer::Bool(true));
            }
            Kind::Choice { label, options, key } => {
                leaf.kind = "choice";
                leaf.text = label.clone();
                leaf.key = key.clone();
                leaf.required = self.form.required.contains(key);
                leaf.options = options.iter().map(|o| (o.value.clone(), o.label.clone())).collect();
                let chosen = self.values.get(key);
                leaf.chosen = options.iter().position(|o| chosen == Some(&Answer::Str(o.value.clone()))).map_or(-1, |i| i as i32);
            }
            Kind::Divider => leaf.kind = "divider",
            Kind::Card { title } => {
                leaf.kind = "card";
                leaf.text = title.clone().unwrap_or_default();
            }
            Kind::Stack { .. } => {}
        }
        leaf
    }

    /// The element's rows, in order.
    fn rows(&self, element: &Element, in_card: bool, out: &mut Vec<Row>) {
        match &element.kind {
            Kind::Stack { horizontal } => {
                // Words side by side; controls one under another.
                let words = element.children.len() >= 2
                    && element.children.iter().all(|c| matches!(c.kind, Kind::Heading { .. } | Kind::Text(_) | Kind::Divider));
                if *horizontal && words {
                    let cells = element.children.iter().map(|c| self.leaf(c)).collect();
                    out.push(Row { cells, in_card, ..Row::default() });
                } else {
                    for child in &element.children {
                        self.rows(child, in_card, out);
                    }
                }
            }
            Kind::Card { title } => {
                let mut inside = Vec::new();
                if title.as_deref().is_some_and(|t| !t.is_empty()) || element.children.is_empty() {
                    inside.push(Row { leaf: self.leaf(element), in_card: true, ..Row::default() });
                }
                for child in &element.children {
                    self.rows(child, true, &mut inside);
                }
                if let Some(first) = inside.first_mut() {
                    first.card_top = true;
                }
                if let Some(last) = inside.last_mut() {
                    last.card_bottom = true;
                }
                out.extend(inside);
            }
            _ => out.push(Row { leaf: self.leaf(element), in_card, ..Row::default() }),
        }
    }
}

fn form_leaf(leaf: &Leaf) -> FormLeaf {
    let options: Vec<ArtifactOption> =
        leaf.options.iter().map(|(value, label)| ArtifactOption { id: value.into(), label: label.into(), ..ArtifactOption::default() }).collect();
    FormLeaf {
        id: leaf.id.clone().into(),
        kind: leaf.kind.into(),
        text: leaf.text.clone().into(),
        level: leaf.level,
        placeholder: leaf.placeholder.clone().into(),
        multiline: leaf.multiline,
        key: leaf.key.clone().into(),
        value: leaf.value.clone().into(),
        on: leaf.on,
        options: ModelRc::new(VecModel::from(options)),
        chosen: leaf.chosen,
        required: leaf.required,
        note: leaf.note.clone().into(),
    }
}

fn form_row(row: &Row) -> FormRow {
    FormRow {
        leaf: form_leaf(&row.leaf),
        cells: ModelRc::new(VecModel::from(row.cells.iter().map(form_leaf).collect::<Vec<_>>())),
        in_card: row.in_card,
        card_top: row.card_top,
        card_bottom: row.card_bottom,
    }
}

/// The card's model, brought to `rows` in place: only rows that changed are
/// set, and the model is rebuilt only when the form's shape changed.
fn rows_model(id: &str, rows: Vec<Row>) -> ModelRc<FormRow> {
    SHOWN.with(|shown| {
        let mut shown = shown.borrow_mut();
        let entry = shown.entry(id.to_owned()).or_insert_with(|| Shown { model: Rc::new(VecModel::default()), rows: Vec::new() });
        let same_shape = entry.rows.len() == rows.len() && entry.rows.iter().zip(&rows).all(|(a, b)| a.leaf.id == b.leaf.id && a.cells.len() == b.cells.len());
        if !same_shape {
            entry.model.set_vec(rows.iter().map(form_row).collect::<Vec<_>>());
        } else {
            for (index, (old, new)) in entry.rows.iter().zip(&rows).enumerate() {
                if old != new {
                    entry.model.set_row_data(index, form_row(new));
                }
            }
        }
        entry.rows = rows;
        ModelRc::from(entry.model.clone())
    })
}

fn answered_when(accepted: &Accepted) -> String {
    chrono::DateTime::from_timestamp_millis(accepted.accepted_at)
        .map(|at| at.with_timezone(&chrono::Local).format("%b %-d at %H:%M").to_string())
        .unwrap_or_default()
}

/// The card's form fields from the engine's form (None: not read yet).
pub fn spec_fields(item: &mut ArtifactItem, view: Option<FormView>) {
    let Some(view) = view else {
        item.spec_state = "loading".into();
        return;
    };
    let form = match &view.state {
        FormState::Loading => return item.spec_state = "loading".into(),
        FormState::NeedsNewer => return item.spec_state = "newer".into(),
        FormState::Unreadable => return item.spec_state = "unreadable".into(),
        FormState::Failed => return item.spec_state = "failed".into(),
        FormState::Form(form) => form,
    };
    let read_only = view.accepted.is_some();
    let values = view.accepted.as_ref().map_or(&view.values, |a| &a.state);
    let gaps = form.gaps(&view.values);
    let short = gaps.iter().filter_map(|g| if let Gap::TooShort(key, min) = g { Some((key.clone(), *min)) } else { None }).collect();
    let context = Context { form, values, read_only, short };
    let mut rows = Vec::new();
    context.rows(&form.root, false, &mut rows);
    let captions: BTreeMap<String, String> = rows
        .iter()
        .flat_map(|r| std::iter::once(&r.leaf).chain(&r.cells))
        .filter(|l| l.kind == "field")
        .map(|l| (l.key.clone(), l.text.clone()))
        .collect();
    item.spec_state = "form".into();
    item.spec_rows = rows_model(&item.id, rows);
    item.spec_send = form.submit_label.clone().into();
    item.spec_sending = view.sending;
    item.spec_read_only = read_only;
    item.spec_can_send = !read_only && !view.sending && !view.closed && gaps.is_empty();
    let when = view.accepted.as_ref().map(answered_when).unwrap_or_default();
    item.spec_footnote = footnote(form, &view.values, view.accepted.as_ref(), &view.note, view.closed, &captions, &when).into();
    if read_only {
        // An answered form is no longer an open question.
        item.badge = "Answered".into();
    }
}

/// A render_spec row the Host drew for this client: a list row's marker,
/// or the single read's catalog (an event's read replaces the list row).
/// Any other row of the kind is the plain row of a client without forms.
pub fn drawn(artifact: &serde_json::Value) -> bool {
    artifact.get("type").and_then(|t| t.as_str()) == Some("render_spec")
        && (artifact.get("content_omitted").is_some() || artifact.get("catalog").is_some())
}

/// Asks the engine to read every form among `artifacts` its card shows.
pub fn want(app: &App, artifacts: &[serde_json::Value]) {
    let mut engine = app.engine.borrow_mut();
    for artifact in artifacts.iter().filter(|a| drawn(a)) {
        let id = artifact.get("artifact_id").and_then(|v| v.as_str()).unwrap_or_default();
        let updated = artifact.get("updated_at").and_then(serde_json::Value::as_i64).unwrap_or(0);
        engine.want_render_spec(id, updated);
    }
}

fn with_engine(change: impl FnOnce(&mut clarp_engine::Engine)) {
    if let Some(app) = crate::app() {
        change(&mut app.engine.borrow_mut());
        app.refresh(&[Change::Updates]);
        crate::pump_now(&app);
    }
}

pub fn bind(window: &AppWindow) {
    let bridge = window.global::<ArtifactBridge>();
    bridge.on_form_text(|id, key, text| with_engine(|e| e.set_render_spec_value(&id, &key, Answer::Str(text.to_string()))));
    bridge.on_form_toggle(|id, key, on| with_engine(|e| e.set_render_spec_value(&id, &key, Answer::Bool(on))));
    bridge.on_form_choose(|id, key, value| with_engine(|e| e.set_render_spec_value(&id, &key, Answer::Str(value.to_string()))));
    bridge.on_form_send(|id| with_engine(|e| e.send_render_spec(&id)));
    bridge.on_form_retry(|id| with_engine(|e| e.retry_render_spec(&id)));
}

#[cfg(test)]
mod tests {
    use super::*;
    use clarp_core::render_spec::{Reading, read};

    fn release_check() -> serde_json::Value {
        serde_json::json!({
          "version": "v1", "catalog": "clarp.form", "catalog_version": 1,
          "spec": {"root": "root", "elements": {
            "root": {"type": "stack", "props": {}, "children": ["title", "verdict", "pair", "mixed", "echo"]},
            "title": {"type": "heading", "props": {"text": "Build", "level": 1}},
            "verdict": {"type": "card", "props": {"title": "Your call"}, "children": ["decision", "reason"]},
            "decision": {"type": "choice", "props": {"label": "What", "value": {"$bindState": "/decision"},
              "options": [{"value": "ship", "label": "Ship"}, {"value": "hold", "label": "Hold"}]}},
            "reason": {"type": "text_field", "props": {"label": "Reason", "value": {"$bindState": "/reason"}}},
            "pair": {"type": "stack", "props": {"axis": "horizontal"}, "children": ["left", "right"]},
            "left": {"type": "text", "props": {"text": "L"}},
            "right": {"type": "text", "props": {"text": "R"}},
            "mixed": {"type": "stack", "props": {"axis": "horizontal"}, "children": ["note", "notify"]},
            "note": {"type": "text", "props": {"text": "N"}},
            "notify": {"type": "toggle", "props": {"label": "Notify", "value": {"$bindState": "/notify"}}},
            "echo": {"type": "text", "props": {"text": {"$state": "/reason"}}}},
            "state": {}},
          "submit": {"intent": "call", "label": "Send"},
          "answer_schema": {"type": "object", "required": ["decision", "reason"], "properties": {
            "decision": {"type": "string", "enum": ["ship", "hold"]},
            "reason": {"type": "string", "minLength": 5}, "notify": {"type": "boolean"}}}
        })
    }

    fn rows(values: &Values, read_only: bool) -> Vec<Row> {
        let Reading::Form(form) = read(&release_check()) else { panic!("form") };
        let short = form.gaps(values).iter().filter_map(|g| if let Gap::TooShort(k, m) = g { Some((k.clone(), *m)) } else { None }).collect();
        let context = Context { form: &form, values, read_only, short };
        let mut out = Vec::new();
        context.rows(&form.root, false, &mut out);
        out
    }

    #[test]
    fn a_form_flattens_into_rows_in_reading_order() {
        let rows = rows(&Values::new(), false);
        let kinds: Vec<&str> = rows.iter().map(|r| if r.cells.is_empty() { r.leaf.kind } else { "cells" }).collect();
        assert_eq!(kinds, ["heading", "card", "choice", "field", "cells", "text", "toggle", "text"]);
        assert!(rows[1].card_top && rows[1].in_card && !rows[1].card_bottom);
        assert!(rows[3].card_bottom && rows[3].in_card);
        assert!(!rows[4].in_card);
        assert!(rows[2].leaf.required && rows[2].leaf.chosen == -1);
    }

    #[test]
    fn values_and_gaps_show_in_their_rows() {
        let values = Values::from([
            ("decision".to_owned(), Answer::Str("hold".into())),
            ("reason".to_owned(), Answer::Str("no".into())),
            ("notify".to_owned(), Answer::Bool(true)),
        ]);
        let rows = rows(&values, false);
        assert_eq!(rows[2].leaf.chosen, 1);
        assert_eq!(rows[3].leaf.value, "no");
        assert_eq!(rows[3].leaf.note, "At least 5 characters.");
        assert!(rows[6].leaf.on);
        assert_eq!(rows[7].leaf.text, "no", "a $state text shows the key's value");
    }
}
