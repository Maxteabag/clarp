//! The Teams surface (TeamsPanel.qml): the team list (grouped under parent
//! teams or flat), the selected team's members and messages, and the
//! create / edit / add member / delete dialogs.

use std::cell::{Cell, RefCell};
use std::collections::HashSet;
use std::rc::Rc;
use std::time::Duration;

use clarp_core::json::{self, Object};
use clarp_engine::Change;
use serde_json::Value;
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

use crate::{App, AppWindow, Palette, TeamMemberView, TeamMessageView, TeamView, commands, pump_now};

thread_local! {
    static GROUPED: Cell<bool> = const { Cell::new(true) };
    /// Agent ids behind the edit dialog's leader choices ("" is no leader).
    static LEADERS: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
    /// Agent ids behind the add-member dialog's choices.
    static AGENTS: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
    /// Reloads the selected team's messages every 10 s while it shows.
    static POLL: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
}

fn text(object: &Object, key: &str) -> String {
    json::js_string(object.get(key))
}

fn members_of(team: &Object) -> Vec<String> {
    json::array(team, "member_agent_ids").iter().filter_map(Value::as_str).map(str::to_owned).collect()
}

/// `#rgb`, `#rrggbb` or `#rrggbbaa`; None for anything else.
fn hex_colour(value: &str) -> Option<slint::Color> {
    let hex = value.strip_prefix('#')?;
    let digits: Vec<u8> = hex.chars().map(|c| c.to_digit(16).map(|d| d as u8)).collect::<Option<_>>()?;
    let pair = |i: usize| digits[i] * 16 + digits[i + 1];
    match digits.len() {
        3 => Some(slint::Color::from_rgb_u8(digits[0] * 17, digits[1] * 17, digits[2] * 17)),
        6 => Some(slint::Color::from_rgb_u8(pair(0), pair(2), pair(4))),
        8 => Some(slint::Color::from_argb_u8(pair(6), pair(0), pair(2), pair(4))),
        _ => None,
    }
}

/// TeamsPanel.qml `orderedTeams`: children under their parent when grouped;
/// a team in a malformed cycle still shows, at the top level.
pub fn ordered(teams: &[Value], grouped: bool) -> Vec<(Object, usize)> {
    let source: Vec<&Object> = teams.iter().filter_map(Value::as_object).collect();
    if !grouped {
        return source.into_iter().map(|t| (t.clone(), 0)).collect();
    }
    fn append<'a>(team: &'a Object, depth: usize, source: &[&'a Object], seen: &mut HashSet<String>, out: &mut Vec<(Object, usize)>) {
        let key = text(team, "team_id");
        if !seen.insert(key.clone()) {
            return;
        }
        out.push((team.clone(), depth));
        for child in source.iter().filter(|c| text(c, "parent_team_id") == key) {
            append(child, depth + 1, source, seen, out);
        }
    }
    let (mut out, mut seen) = (Vec::new(), HashSet::new());
    for team in &source {
        let parent = text(team, "parent_team_id");
        if parent.is_empty() || !source.iter().any(|t| text(t, "team_id") == parent) {
            append(team, 0, &source, &mut seen, &mut out);
        }
    }
    for team in &source {
        append(team, 0, &source, &mut seen, &mut out);
    }
    out
}

fn model<T: Clone + 'static>(rows: Vec<T>) -> ModelRc<T> {
    ModelRc::new(VecModel::from(rows))
}

pub fn show(app: &App, window: &AppWindow) {
    let engine = app.engine.borrow();
    let selected = engine.selected_team_id().to_owned();
    let faint = window.global::<Palette>().get_faint();
    let grouped = GROUPED.with(Cell::get);
    let rows: Vec<TeamView> = ordered(engine.teams(), grouped)
        .into_iter()
        .map(|(team, depth)| {
            let id = text(&team, "team_id");
            let name = text(&team, "name");
            let latest = text(&team, "latest_message");
            let subtitle = format!(
                "{}{}",
                if text(&team, "parent_team_id").is_empty() { "" } else { "Subteam · " },
                if latest.is_empty() { format!("{} members", members_of(&team).len()) } else { latest }
            );
            TeamView {
                selected: id == selected,
                initial: name.chars().next().map_or_else(|| "?".to_owned(), |c| c.to_uppercase().to_string()).into(),
                name: if name.is_empty() { "Team".into() } else { name.into() },
                colour: hex_colour(&text(&team, "color")).unwrap_or(faint),
                depth: depth as i32,
                unread: json::js_number(team.get("unread_count")) as i32,
                subtitle: subtitle.into(),
                id: id.into(),
            }
        })
        .collect();
    let team = engine.team(&selected).cloned();
    let (title, subtitle, members, nudging) = match &team {
        Some(team) => {
            let members = members_of(team);
            let leader = text(team, "leader_agent_id");
            let subtitle = format!(
                "{} members{}",
                members.len(),
                if leader.is_empty() { String::new() } else { format!("  ·  leader {}", engine.agent_name_by_id(&leader)) }
            );
            let views = members.iter().map(|id| TeamMemberView { id: id.into(), name: engine.agent_name_by_id(id).into() }).collect();
            (first_or(&text(team, "name"), "Team"), subtitle, views, json::truthy(team.get("nudge_enabled")))
        }
        None => ("Select a team".to_owned(), String::new(), Vec::new(), false),
    };
    let messages: Vec<TeamMessageView> = engine
        .team_messages()
        .iter()
        .filter_map(Value::as_object)
        .map(|m| TeamMessageView {
            source: first_or(&text(m, "source_name"), "Agent").into(),
            text: text(m, "text").into(),
            session: text(m, "source_session").into(),
        })
        .collect();
    window.set_team_list(model(rows));
    window.set_team_selected(if team.is_some() { SharedString::from(selected) } else { SharedString::new() });
    window.set_team_title(title.into());
    window.set_team_subtitle(subtitle.into());
    window.set_team_members(model(members));
    window.set_team_nudging(nudging);
    window.set_team_messages(model(messages));
    window.set_teams_loading(engine.teams_loading());
    window.set_teams_error(engine.teams_error().into());
    window.set_teams_grouped(grouped);
}

fn first_or(value: &str, fallback: &str) -> String {
    if value.is_empty() { fallback.to_owned() } else { value.to_owned() }
}

pub fn refresh(app: &App, window: &AppWindow, changes: &[Change]) {
    if changes.iter().any(|c| matches!(c, Change::Teams | Change::Roster)) {
        show(app, window);
    }
}

/// Ctrl+3 or the rail: shows the surface and loads the teams.
pub fn open(app: &App, window: &AppWindow) {
    window.set_surface("teams".into());
    app.engine.borrow_mut().load_teams();
    window.invoke_focus_teams();
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, Duration::from_secs(10), || {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        if window.get_surface() != "teams" {
            POLL.with(|p| p.borrow_mut().take());
            return;
        }
        let selected = app.engine.borrow().selected_team_id().to_owned();
        if !selected.is_empty() {
            app.engine.borrow_mut().select_team(&selected);
            pump_now(&app);
        }
    });
    POLL.with(|p| *p.borrow_mut() = Some(timer));
}

/// The panel's buttons (`team-action`).
pub fn action(app: &Rc<App>, window: &AppWindow, action: &str, argument: &str) {
    let selected = app.engine.borrow().selected_team_id().to_owned();
    match action {
        "select" => app.engine.borrow_mut().select_team(argument),
        "refresh" if selected.is_empty() => app.engine.borrow_mut().load_teams(),
        "refresh" => app.engine.borrow_mut().select_team(&selected),
        "grouping" => {
            GROUPED.with(|g| g.set(!g.get()));
            show(app, window);
        }
        "create" => {
            commands::open_overlay(app, window, "team-create");
            window.invoke_open_team_create();
        }
        "edit" => open_edit(app, window),
        "nudging" => {
            let enabled = app.engine.borrow().team(&selected).is_some_and(|t| json::truthy(t.get("nudge_enabled")));
            app.engine.borrow_mut().set_team_nudging(&selected, !enabled);
        }
        "delete" if !selected.is_empty() => commands::open_overlay(app, window, "team-delete"),
        "add-member" if !selected.is_empty() => {
            let choices = app.engine.borrow().team_agent_choices();
            let text = |c: &Value, key: &str| c.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
            AGENTS.with(|a| *a.borrow_mut() = choices.iter().map(|c| text(c, "id")).collect());
            window.set_team_agents(model(choices.iter().map(|c| SharedString::from(text(c, "name"))).collect()));
            window.set_team_agent_index(0);
            commands::open_overlay(app, window, "team-member");
        }
        "remove-member" => app.engine.borrow_mut().remove_team_member(&selected, argument),
        other => eprintln!("clarp-slint: no team action {other}"),
    }
    pump_now(app);
}

fn open_edit(app: &App, window: &AppWindow) {
    let engine = app.engine.borrow();
    let Some(team) = engine.team(engine.selected_team_id()).cloned() else { return };
    let members = members_of(&team);
    let mut choices = vec![(String::new(), "No leader".to_owned())];
    for choice in engine.team_agent_choices() {
        let id = choice.get("id").and_then(Value::as_str).unwrap_or_default().to_owned();
        if members.contains(&id) {
            choices.push((id, choice.get("name").and_then(Value::as_str).unwrap_or_default().to_owned()));
        }
    }
    drop(engine);
    let leader = text(&team, "leader_agent_id");
    let index = choices.iter().position(|(id, _)| *id == leader).unwrap_or(0);
    window.set_team_leaders(model(choices.iter().map(|(_, name)| SharedString::from(name.as_str())).collect()));
    window.set_team_leader_index(index as i32);
    LEADERS.with(|l| *l.borrow_mut() = choices.into_iter().map(|(id, _)| id).collect());
    commands::open_overlay(app, window, "team-edit");
    window.invoke_open_team_edit(text(&team, "name").into(), text(&team, "color").into());
}

pub fn created(app: &Rc<App>, window: &AppWindow, name: &str) {
    app.engine.borrow_mut().create_team(name, "");
    commands::close_overlay(app, window);
    pump_now(app);
}

pub fn saved(app: &Rc<App>, window: &AppWindow, name: &str, colour: &str, leader: i32) {
    let leader = LEADERS.with(|l| usize::try_from(leader).ok().and_then(|i| l.borrow().get(i).cloned())).unwrap_or_default();
    let selected = app.engine.borrow().selected_team_id().to_owned();
    app.engine.borrow_mut().update_team(&selected, name, colour, &leader);
    commands::close_overlay(app, window);
    pump_now(app);
}

pub fn member_added(app: &Rc<App>, window: &AppWindow, index: i32) {
    let agent = AGENTS.with(|a| usize::try_from(index).ok().and_then(|i| a.borrow().get(i).cloned()));
    if let Some(agent) = agent {
        let selected = app.engine.borrow().selected_team_id().to_owned();
        app.engine.borrow_mut().add_team_member(&selected, &agent);
    }
    commands::close_overlay(app, window);
    pump_now(app);
}

pub fn deleted(app: &Rc<App>, window: &AppWindow) {
    let selected = app.engine.borrow().selected_team_id().to_owned();
    app.engine.borrow_mut().delete_team(&selected);
    commands::close_overlay(app, window);
    pump_now(app);
}

#[cfg(test)]
mod tests {
    use super::{hex_colour, ordered};
    use serde_json::json;

    #[test]
    fn teams_group_under_their_parent_and_cycles_still_show() {
        let teams = vec![
            json!({"team_id": "c", "parent_team_id": "p"}),
            json!({"team_id": "p"}),
            json!({"team_id": "x", "parent_team_id": "y"}),
            json!({"team_id": "y", "parent_team_id": "x"}),
        ];
        let ids = |grouped| ordered(&teams, grouped).into_iter().map(|(t, d)| format!("{}{d}", t["team_id"].as_str().unwrap())).collect::<Vec<_>>();
        assert_eq!(ids(true), ["p0", "c1", "x0", "y1"]);
        assert_eq!(ids(false), ["c0", "p0", "x0", "y0"]);
        assert_eq!(hex_colour("#fff"), Some(slint::Color::from_rgb_u8(255, 255, 255)));
        assert_eq!(hex_colour("#102030"), Some(slint::Color::from_rgb_u8(16, 32, 48)));
        assert_eq!(hex_colour("teal"), None);
    }
}
