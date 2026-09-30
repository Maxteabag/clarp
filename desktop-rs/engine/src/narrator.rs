//! Plain-English tools (the Qt app's `ToolNarrator` over
//! `clarp_core::narrator::Narrator`): the Host explains tool activity at a
//! chosen detail level; the transcript reads explanations cache-only while
//! presenting, and asks for those it shows.

use std::cell::RefCell;
use std::time::Duration;

use clarp_core::json::Object;
use clarp_core::narrator::{DEBOUNCE_MS, DETAIL_LEVELS, Effect, Narrator, POLL_MS, TIMEOUT_MS};
use serde_json::Value;

use crate::{Change, Engine, Message};

/// A narrator timer that came due.
#[derive(Debug)]
pub(crate) enum NarratorTimer {
    Debounce,
    Poll(String),
    Timeout(u64),
}

pub(crate) fn new(settings: &clarp_core::settings::Settings) -> RefCell<Narrator> {
    let mut narrator = Narrator::new();
    // Effects need the engine; the level only fixes what later requests ask.
    let _ = narrator.set_detail_level(settings.integer("experiments/toolDetailLevel", 0) as i32);
    RefCell::new(narrator)
}

impl Engine {
    pub fn narrator_enabled(&self) -> bool {
        self.narrator.borrow().enabled()
    }
    pub fn narrator_unavailable(&self) -> bool {
        self.narrator.borrow().unavailable()
    }
    pub fn narrator_status(&self) -> String {
        self.narrator.borrow().status()
    }
    pub fn narrator_detail_level(&self) -> i32 {
        self.narrator.borrow().detail_level()
    }
    pub fn narrator_detail_levels() -> &'static [&'static str] {
        &DETAIL_LEVELS
    }
    pub fn narrator_level_description(&self) -> &'static str {
        self.narrator.borrow().level_description()
    }

    /// The experiment on or off (the switcher's "plain-English tools").
    pub fn set_narrator_enabled(&mut self, enabled: bool) {
        let effects = self.narrator.borrow_mut().set_enabled(enabled);
        self.narrate(effects);
    }

    /// How plainly tools are explained; kept in settings.
    pub fn set_narrator_detail_level(&mut self, level: i32) {
        let effects = self.narrator.borrow_mut().set_detail_level(level);
        self.narrate(effects);
    }

    /// The cached explanation of an activity ("" until the Host has one).
    pub fn explanation_for(&self, session: &str, activity: &Object) -> String {
        self.narrator.borrow_mut().explanation(&with_session(session, activity))
    }

    /// The Host could not explain this activity.
    pub fn explanation_failed(&self, session: &str, activity: &Object) -> bool {
        self.narrator.borrow_mut().failed(&with_session(session, activity))
    }

    /// Asks for an activity's explanation (one the reader opened).
    pub fn request_explanation(&mut self, session: &str, activity: &Object) {
        let effects = self.narrator.borrow_mut().request(&with_session(session, activity));
        self.narrate(effects);
    }

    /// A shown activity keeps its explanation wanted; `owner` names the view.
    pub fn acquire_explanation_view(&mut self, owner: u64, session: &str, activity: &Object) {
        let effects = self.narrator.borrow_mut().acquire_view(owner, &with_session(session, activity));
        self.narrate(effects);
    }

    pub fn release_explanation_view(&mut self, owner: u64) {
        let effects = self.narrator.borrow_mut().release_view(owner);
        self.narrate(effects);
    }

    /// Presents `session` with the narrator's cached explanations (never
    /// asking the Host: presenting must not create demand).
    pub(crate) fn present_with_explanations(&mut self, session: &str) -> Vec<clarp_core::presentation::PresentedRow> {
        let Some(conversation) = self.conversations.get(session) else { return Vec::new() };
        let explaining = {
            let narrator = self.narrator.borrow();
            narrator.enabled() && !narrator.unavailable()
        };
        let narrator = &self.narrator;
        let lookup = |activity: &Object| -> String { narrator.borrow_mut().explanation(&with_session(session, activity)) };
        let lookup = explaining.then_some(&lookup as &dyn Fn(&Object) -> String);
        clarp_core::presentation::present(conversation.rows(), &mut self.presentation, lookup).rows
    }

    pub(crate) fn narrator_json(&mut self, tag: &str, object: &Object) -> bool {
        if tag == "explanation-release" {
            return true;
        }
        if !self.narrator.borrow().is_own_tag(tag) {
            return false;
        }
        let effects = self.narrator.borrow_mut().handle_reply(tag, object);
        self.narrate(effects);
        true
    }

    pub(crate) fn narrator_failure(&mut self, tag: &str, status: u16) -> bool {
        if tag == "explanation-release" {
            return true;
        }
        if !self.narrator.borrow().is_own_tag(tag) {
            return false;
        }
        let effects = self.narrator.borrow_mut().handle_failure(tag, status);
        self.narrate(effects);
        true
    }

    pub(crate) fn narrator_due(&mut self, timer: NarratorTimer) {
        let effects = {
            let mut narrator = self.narrator.borrow_mut();
            match timer {
                NarratorTimer::Debounce => narrator.start_batch(),
                NarratorTimer::Poll(tag) => narrator.poll_for(&tag),
                NarratorTimer::Timeout(generation) => narrator.timed_out(generation),
            }
        };
        self.narrate(effects);
    }

    pub(crate) fn reset_narrator(&mut self) {
        let effects = self.narrator.borrow_mut().reset();
        self.narrate(effects);
    }

    fn narrate(&mut self, effects: Vec<Effect>) {
        for effect in effects {
            match effect {
                Effect::HostRequest { tag, body } => self.api.post_json(&tag, "/tool-explanations", body, None),
                Effect::Debounce => self.after(Duration::from_millis(DEBOUNCE_MS), Message::Narrator(NarratorTimer::Debounce)),
                Effect::Poll { tag } => self.after(Duration::from_millis(POLL_MS), Message::Narrator(NarratorTimer::Poll(tag))),
                Effect::Timeout { generation } => {
                    self.after(Duration::from_millis(TIMEOUT_MS), Message::Narrator(NarratorTimer::Timeout(generation)))
                }
                Effect::Changed | Effect::EnabledChanged => self.changes.push(Change::Narrator),
                Effect::DetailLevelChanged => {
                    let level = self.narrator.borrow().detail_level();
                    self.settings.set("experiments/toolDetailLevel", i64::from(level));
                    if level > 0 {
                        self.settings.set("experiments/toolLastTranslationLevel", i64::from(level));
                    }
                    self.changes.push(Change::Narrator);
                }
            }
        }
    }
}

fn with_session(session: &str, activity: &Object) -> Object {
    let mut activity = activity.clone();
    activity.insert("_session".into(), Value::from(session));
    activity
}
