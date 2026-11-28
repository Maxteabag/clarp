//! Native forms (`render_spec`, issue 428) in their chat card: the engine's
//! form as rows the card draws (iOS `RenderSpecFormController`), and what
//! the card's controls do.
//!
//! Slint draws no recursive tree, so the form's elements become a flat list
//! of rows: a vertical stack's children one after another, a card's children
//! in its box (its title first), a horizontal stack of words side by side.
//! A horizontal stack holding a control lays its controls out one under
//! another, as iOS does at large text sizes; one of buttons keeps them side
//! by side. Catalog version 2's chart and table are drawn from
//! `form_chart` and the table's cells here, numbers in the device's decimal
//! separator; its buttons send the form (a question first, in the window's
//! `form-confirm` dialog). Each card keeps one row model
//! for as long as the window runs, changed in place, so a text field being
//! typed in is never rebuilt under the keyboard.

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::rc::Rc;

use clarp_core::render_spec::{Accepted, Answer, ButtonStyle, Cell, Element, Form, Gap, Kind, Table, TextSource, Values, answered_word, footnote};
use clarp_engine::{Change, FormState, FormView};
use slint::{ComponentHandle, Model, ModelRc, VecModel};

use crate::form_chart::Drawn;
use crate::{
    App, AppWindow, ArtifactBridge, ArtifactItem, ArtifactOption, FormChart, FormChartDot, FormChartKey, FormChartMark, FormChartTick, FormLeaf, FormRow, FormTable,
    FormTableCell, FormTableColumn, FormTableRow,
};

/// The overlay a button's question opens.
pub const CONFIRM: &str = "form-confirm";

/// The device's decimal separator, read once.
fn separator() -> char {
    thread_local! {
        static SEPARATOR: char = clarp_core::render_spec::device_decimal_separator();
    }
    SEPARATOR.with(|s| *s)
}

/// A table as drawn: columns (label, end-aligned, width in characters) and
/// rows of (words, flag), each with what a screen reader says.
#[derive(Debug, Clone, PartialEq, Default)]
struct TableLeaf {
    title: String,
    columns: Vec<(String, bool, i32)>,
    rows: Vec<(Vec<(String, i32)>, String)>,
}

/// A column is as wide as its longest words, from 3 to 32 characters.
fn table_leaf(table: &Table, separator: char) -> TableLeaf {
    let rows: Vec<(Vec<(String, i32)>, String)> = table
        .rows
        .iter()
        .map(|row| {
            let cells = row
                .iter()
                .zip(&table.columns)
                .map(|(cell, column)| match cell {
                    Cell::Flag(true) => ("✓".to_owned(), 1),
                    Cell::Flag(false) => ("–".to_owned(), 0),
                    other => (Table::words(other, column, separator), -1),
                })
                .collect();
            let words = row
                .iter()
                .zip(&table.columns)
                .filter_map(|(cell, column)| Some(Table::words(cell, column, separator)).filter(|w| !w.is_empty()).map(|w| format!("{}: {w}", column.label)))
                .collect::<Vec<_>>()
                .join(", ");
            (cells, words)
        })
        .collect();
    let columns = table
        .columns
        .iter()
        .enumerate()
        .map(|(index, column)| {
            let widest = rows.iter().map(|(cells, _)| cells[index].0.chars().count()).chain([column.label.chars().count()]).max().unwrap_or(0);
            (column.label.clone(), column.align_end, widest.clamp(3, 32) as i32)
        })
        .collect();
    TableLeaf { title: table.title.clone().unwrap_or_default(), columns, rows }
}

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
    chart: Drawn,
    table: TableLeaf,
    style: &'static str,
    confirm: String,
    enabled: bool,
    busy: bool,
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

impl Shown {
    fn new() -> Self {
        Shown { model: Rc::new(VecModel::default()), rows: Vec::new() }
    }
}

/// The card's one row model (made empty when the card asks first).
fn model_of(id: &str) -> ModelRc<FormRow> {
    SHOWN.with(|shown| ModelRc::from(shown.borrow_mut().entry(id.to_owned()).or_insert_with(Shown::new).model.clone()))
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
    /// The button the accepted answer chose.
    chosen: Option<String>,
    /// The button whose answer is being sent.
    sending: Option<String>,
    /// The buttons can be pressed: every required value is there and
    /// nothing was sent or is being sent.
    pressable: bool,
    separator: char,
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
                    // i18n: form card, note under a text field: its shortest answer; {} is the least number of characters
                    leaf.note = clarp_core::tr!("At least {} characters.", min);
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
            Kind::Chart(chart) => {
                leaf.kind = "chart";
                leaf.chart = crate::form_chart::draw(chart, self.separator);
            }
            Kind::Table(table) => {
                leaf.kind = "table";
                leaf.table = table_leaf(table, self.separator);
            }
            Kind::Button(button) => {
                leaf.kind = "button";
                leaf.text = button.label.clone();
                leaf.key = button.action.clone();
                leaf.style = match button.style {
                    ButtonStyle::Primary => "primary",
                    ButtonStyle::Secondary => "secondary",
                    ButtonStyle::Destructive => "destructive",
                };
                leaf.confirm = button.confirm.clone().unwrap_or_default();
                leaf.on = self.chosen.as_deref() == Some(button.action.as_str());
                leaf.busy = self.sending.as_deref() == Some(button.action.as_str());
                leaf.enabled = self.pressable;
            }
            Kind::Stack { .. } => {}
        }
        leaf
    }

    /// The element's rows, in order.
    fn rows(&self, element: &Element, in_card: bool, out: &mut Vec<Row>) {
        match &element.kind {
            Kind::Stack { horizontal } => {
                // Words side by side, buttons too; controls one under another.
                let words = element.children.len() >= 2
                    && (element.children.iter().all(|c| matches!(c.kind, Kind::Heading { .. } | Kind::Text(_) | Kind::Divider))
                        || element.children.iter().all(|c| matches!(c.kind, Kind::Button(_))));
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

fn model<T: Clone + 'static>(items: Vec<T>) -> ModelRc<T> {
    ModelRc::new(VecModel::from(items))
}

fn form_chart(chart: &Drawn) -> FormChart {
    FormChart {
        kind: chart.kind.into(),
        title: chart.title.clone().into(),
        marks: model(chart.marks.iter().map(|m| FormChartMark { commands: m.commands.clone().into(), color: m.color, fill: m.fill }).collect()),
        dots: model(chart.dots.iter().map(|d| FormChartDot { x: d.x, y: d.y, color: d.color }).collect()),
        x_ticks: model(chart.x_ticks.iter().map(|t| FormChartTick { at: t.at, text: t.text.clone().into() }).collect()),
        y_ticks: model(chart.y_ticks.iter().map(|t| FormChartTick { at: t.at, text: t.text.clone().into() }).collect()),
        keys: model(chart.keys.iter().map(|k| FormChartKey { label: k.label.clone().into(), color: k.color }).collect()),
        x_label: chart.x_label.clone().into(),
        y_label: chart.y_label.clone().into(),
        summary: chart.summary.clone().into(),
    }
}

fn form_table(table: &TableLeaf) -> FormTable {
    FormTable {
        title: table.title.clone().into(),
        columns: model(table.columns.iter().map(|(label, end, chars)| FormTableColumn { label: label.into(), end: *end, chars: *chars }).collect()),
        rows: model(
            table
                .rows
                .iter()
                .map(|(cells, words)| FormTableRow {
                    cells: model(cells.iter().map(|(text, flag)| FormTableCell { text: text.into(), flag: *flag }).collect()),
                    words: words.into(),
                })
                .collect(),
        ),
        chars: table.columns.iter().map(|c| c.2).sum(),
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
        chart: form_chart(&leaf.chart),
        table: form_table(&leaf.table),
        style: leaf.style.into(),
        confirm: leaf.confirm.clone().into(),
        enabled: leaf.enabled,
        busy: leaf.busy,
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
        let entry = shown.entry(id.to_owned()).or_insert_with(Shown::new);
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
/// `answered_label` is the row's (a Host with `render_spec_answered_button`
/// names the button an answered v2 form chose); the badge reads "Answered ·
/// <label>" from it, or from the form's own read on an older Host.
pub fn spec_fields(item: &mut ArtifactItem, view: Option<FormView>, answered_label: Option<&str>) {
    if answered_label.is_some_and(|l| !l.is_empty()) {
        item.badge = answered_word(answered_label).into();
    }
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
    let chosen = form.chosen_button(view.accepted.as_ref()).map(|b| b.action.clone());
    let context = Context {
        form,
        values,
        read_only,
        short,
        chosen,
        sending: view.pressed.clone().filter(|_| view.sending),
        pressable: !read_only && !view.sending && !view.closed && gaps.is_empty(),
        separator: separator(),
    };
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
        // An answered form is no longer an open question; a button form's
        // badge names the button (the read's answer, else the row's).
        let label = form.chosen_button(view.accepted.as_ref()).map(|b| b.label.as_str()).or(answered_label);
        item.badge = answered_word(label).into();
    }
}

/// A form's button pressed: its question first when it has one, else the
/// form is sent with its action.
fn press(id: &str, action: &str) {
    let Some(app) = crate::app() else { return };
    let confirm = {
        let engine = app.engine.borrow();
        let Some(view) = engine.render_spec(id) else { return };
        let FormState::Form(form) = &view.state else { return };
        // Only an open form's button asks or sends: once answered, while
        // one is sent, or with a required value missing, a press is nothing.
        if view.accepted.is_some() || view.sending || view.closed || !form.gaps(&view.values).is_empty() {
            return;
        }
        let Some(button) = form.buttons.iter().find(|b| b.action == action) else { return };
        button.confirm.clone().map(|question| (question, button.style == ButtonStyle::Destructive))
    };
    let Some((question, destructive)) = confirm else {
        return with_engine(|e| e.press_render_spec_button(id, action));
    };
    let Some(window) = crate::window() else { return };
    PENDING.with(|p| *p.borrow_mut() = Some((id.to_owned(), action.to_owned())));
    let bridge = window.global::<ArtifactBridge>();
    bridge.set_confirm_text(question.into());
    bridge.set_confirm_ok("OK".into());
    bridge.set_confirm_destructive(destructive);
    // Its field gives the keyboard back: Escape is the dialog's.
    bridge.set_editing(false);
    crate::commands::open_overlay(&app, &window, CONFIRM);
}

/// The question's answer: OK sends the form with the button's action.
fn confirmed(ok: bool) {
    let pending = PENDING.with(|p| p.borrow_mut().take());
    if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
        if *app.overlay.borrow() == CONFIRM {
            crate::commands::close_overlay(&app, &window);
        }
    }
    if let (true, Some((id, action))) = (ok, pending) {
        with_engine(|e| e.press_render_spec_button(&id, &action));
    }
}

thread_local! {
    /// The button whose question is open: (card, action).
    static PENDING: RefCell<Option<(String, String)>> = const { RefCell::new(None) };
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
    bridge.on_form_rows(|id| model_of(&id));
    bridge.on_form_retry(|id| with_engine(|e| e.retry_render_spec(&id)));
    bridge.on_form_press(|id, action| press(&id, &action));
    bridge.on_form_confirm_answer(confirmed);
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
        let context = Context { form: &form, values, read_only, short, chosen: None, sending: None, pressable: true, separator: ',' };
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

    fn deploy() -> Form {
        let payload: serde_json::Value = serde_json::from_str(include_str!("../../../tests/fixtures/render_spec/deploy-v2.json")).unwrap();
        let Reading::Form(form) = read(&payload) else { panic!("form") };
        *form
    }

    fn v2_rows(form: &Form, chosen: Option<&str>, sending: Option<&str>, pressable: bool) -> Vec<Row> {
        let values = Values::new();
        let context = Context {
            form,
            values: &values,
            read_only: chosen.is_some(),
            short: BTreeMap::new(),
            chosen: chosen.map(str::to_owned),
            sending: sending.map(str::to_owned),
            pressable,
            separator: ',',
        };
        let mut out = Vec::new();
        context.rows(&form.root, false, &mut out);
        out
    }

    #[test]
    fn a_v2_form_draws_its_chart_table_and_buttons_side_by_side() {
        let form = deploy();
        let rows = v2_rows(&form, None, None, true);
        let kinds: Vec<&str> = rows.iter().map(|r| if r.cells.is_empty() { r.leaf.kind } else { "cells" }).collect();
        assert_eq!(kinds, ["chart", "table", "field", "cells"]);
        assert_eq!(rows[0].leaf.chart.kind, "line");
        assert_eq!(rows[0].leaf.chart.keys.len(), 2, "two series, a legend");
        assert_eq!(rows[1].leaf.table.columns, [("Host".to_owned(), false, 4), ("Healthy".to_owned(), true, 7)]);
        assert_eq!(rows[1].leaf.table.rows[0], (vec![("one".to_owned(), -1), ("✓".to_owned(), 1)], "Host: one, Healthy: Yes".to_owned()));
        assert_eq!(rows[1].leaf.table.rows[1].0[1], ("–".to_owned(), 0), "false is a dash");
        let buttons = &rows[3].cells;
        assert_eq!(buttons.iter().map(|b| (b.kind, b.text.as_str(), b.key.as_str(), b.style)).collect::<Vec<_>>(),
            [("button", "Ship it", "ship", "primary"), ("button", "Hold", "hold", "destructive")]);
        assert!(buttons.iter().all(|b| b.enabled && !b.on && !b.busy));
        assert_eq!(buttons[1].confirm, "Hold the release?");
    }

    #[test]
    fn a_pressed_button_is_busy_and_the_chosen_one_marked_once_answered() {
        let form = deploy();
        let rows = v2_rows(&form, None, Some("ship"), false);
        assert!(rows[3].cells[0].busy && !rows[3].cells[1].busy);
        assert!(rows[3].cells.iter().all(|b| !b.enabled), "every button waits while one is sent");
        let rows = v2_rows(&form, Some("ship"), None, false);
        assert!(rows[3].cells[0].on && !rows[3].cells[1].on, "the answered form marks the chosen button");
        assert!(rows[3].cells.iter().all(|b| !b.enabled), "after one submission every button is off");
    }

    #[test]
    fn a_table_cell_shows_numbers_in_the_device_separator_without_grouping() {
        let table = Table {
            title: None,
            columns: vec![clarp_core::render_spec::Column { key: "v".into(), label: "Value".into(), align_end: true, decimals: 1 }],
            rows: vec![vec![Cell::Number(125)], vec![Cell::Number(125_000)], vec![Cell::Empty]],
        };
        let leaf = table_leaf(&table, ',');
        assert_eq!(leaf.rows.iter().map(|r| r.0[0].0.as_str()).collect::<Vec<_>>(), ["12,5", "12500,0", ""]);
        assert_eq!(leaf.rows[2].1, "", "an empty cell is left out of what a screen reader says");
        assert_eq!(leaf.columns[0], ("Value".to_owned(), true, 7));
    }
}
