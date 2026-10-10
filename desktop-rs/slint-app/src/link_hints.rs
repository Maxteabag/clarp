//! Link hints (Vimium's): F in the chat, or Ctrl+L anywhere in it, numbers
//! every link and artifact card in the active pane's viewport, top to
//! bottom; typing the number opens a link the way a click does, and does a
//! card's primary action (O on it).
//!
//! Slint's rich text has no public link geometry, so the links are found
//! the way a pointer finds them: each rich text drawn in the viewport is
//! hit-tested on a fine grid with Slint's own `link_under_cursor` (shaped
//! once per text through a private layout cache). The badges float in a
//! layer over the window; nothing in the chat moves or re-lays out.
//!
//! Numbers: up to 9 links, one digit opens at once. With more, a digit
//! opens as soon as no other number starts with what was typed (with 12,
//! "5" opens 5 at once, "1" waits for "2" or Enter), Backspace takes a
//! digit back, Escape cancels. Any other key, a scroll or a moving chat
//! cancels too.

use std::cell::RefCell;
use std::pin::Pin;

use i_slint_core::accessibility::AccessibleStringProperty;
use i_slint_core::graphics::euclid;
use i_slint_core::item_rendering::RenderText;
use i_slint_core::item_tree::ItemRc;
use i_slint_core::items::StyledTextItem;
use i_slint_core::lengths::{LogicalSize, PhysicalPx, ScaleFactor};
use i_slint_core::textlayout::sharedparley::{TextLayoutCache, link_under_cursor};
use i_slint_core::window::WindowInner;
use slint::{ComponentHandle, ModelRc, VecModel};

use crate::{App, AppWindow, LinkBadge, LinkHints};

/// The probe grid, in logical pixels: finer than a letter across, a third
/// of a line down.
const STEP_X: f32 = 4.0;
const STEP_Y: f32 = 5.0;
/// Numbers beyond two digits are not worth typing.
const MOST: usize = 99;
/// How far a badge sits above its link's top.
const RAISE: f32 = 5.0;

/// One piece of a link, or an artifact card, on screen (window
/// coordinates).
#[derive(Debug, Clone, PartialEq)]
pub struct Piece {
    pub url: String,
    /// An artifact card's (or image block's) id; then `url` is empty.
    pub card: String,
    pub x: f32,
    pub y: f32,
    pub bottom: f32,
}

impl Piece {
    /// What its number opens: the link, or the card.
    fn target(&self) -> String {
        if self.card.is_empty() { self.url.clone() } else { format!("\u{1}card:{}", self.card) }
    }
}

#[derive(Default)]
struct State {
    /// The numbered targets (`Piece::target`): number n is `links[n - 1]`.
    links: Vec<String>,
    pieces: Vec<Piece>,
    typed: String,
    /// The pane and its chat's offset when the hints were drawn.
    pane: String,
    offset: f32,
    /// The chat's top edge: a badge never rises above it.
    top: f32,
}

thread_local! {
    static STATE: RefCell<Option<State>> = const { RefCell::new(None) };
}

pub fn active() -> bool {
    STATE.with(|s| s.borrow().is_some())
}

/// The window rectangle (x, y, width, height) of `item`.
fn rect(item: &ItemRc) -> (f32, f32, f32, f32) {
    let geometry = item.geometry();
    let origin = item.map_to_window(geometry.origin);
    (origin.x, origin.y, geometry.size.width, geometry.size.height)
}

fn visit(item: &ItemRc, found: &mut dyn FnMut(&ItemRc) -> bool) {
    if found(item) {
        return;
    }
    let mut child = item.first_child();
    while let Some(current) = child {
        visit(&current, found);
        child = current.next_sibling();
    }
}

/// The links drawn in the active pane's transcript, top to bottom, and the
/// transcript's top edge.
pub fn links_on_screen(window: &AppWindow, pane: &str) -> (Vec<Piece>, f32) {
    let inner = WindowInner::from_pub(window.window());
    let root = ItemRc::new_root(inner.component());
    let wanted = format!("chat:{pane}");
    let mut transcript = None;
    visit(&root, &mut |item| {
        if transcript.is_some() {
            return true;
        }
        if item.accessible_string_property(AccessibleStringProperty::Id).is_some_and(|id| id == wanted.as_str()) {
            transcript = Some(item.clone());
            return true;
        }
        false
    });
    let Some(transcript) = transcript else {
        eprintln!("clarp-slint: link hints found no chat for pane {pane}");
        return (Vec::new(), 0.0);
    };
    let viewport = rect(&transcript);
    let scale = ScaleFactor::new(window.window().scale_factor());
    let cache = TextLayoutCache::default();
    let mut pieces = Vec::new();
    visit(&transcript, &mut |item| {
        let Some(text) = item.downcast::<StyledTextItem>() else { return false };
        if item.is_visible() {
            pieces.extend(probe(item, text.as_pin_ref(), viewport, scale, window, &cache));
        }
        true
    });
    (order(pieces), viewport.1)
}

/// Hit-tests one rich text where it shows in `viewport`.
fn probe(item: &ItemRc, text: Pin<&StyledTextItem>, viewport: (f32, f32, f32, f32), scale: ScaleFactor, window: &AppWindow, cache: &TextLayoutCache) -> Vec<Piece> {
    let (x, y, width, height) = rect(item);
    let (left, top) = ((viewport.0 - x).max(0.0), (viewport.1 - y).max(0.0));
    let (right, bottom) = ((viewport.0 + viewport.2 - x).min(width), (viewport.1 + viewport.3 - y).min(height));
    if right <= left || bottom <= top {
        return Vec::new();
    }
    let render: Pin<&dyn RenderText> = text;
    let size = LogicalSize::new(width, height);
    let hit = |px: f32, py: f32| {
        let cursor = euclid::Point2D::<f32, PhysicalPx>::new(px * scale.get(), py * scale.get());
        link_under_cursor(scale, render, item, size, cursor, window.window(), Some(cache))
    };
    // Runs of one link along each probe row, joined down the rows into
    // boxes: a box is one line's piece of a link.
    let mut boxes: Vec<(String, f32, f32, f32, f32)> = Vec::new();
    let mut open: Vec<usize> = Vec::new();
    let mut py = top + STEP_Y / 2.0;
    while py < bottom {
        let mut runs: Vec<(String, f32, f32)> = Vec::new();
        let mut px = left + 1.0;
        while px < right {
            if let Some(url) = hit(px, py) {
                match runs.last_mut() {
                    Some(run) if run.0 == url && run.2 + STEP_X >= px - 0.01 => run.2 = px,
                    _ => runs.push((url, px, px)),
                }
            }
            px += STEP_X;
        }
        let mut still = Vec::new();
        for (url, x0, x1) in runs {
            let joined = open.iter().copied().find(|&i| boxes[i].0 == url && boxes[i].1 <= x1 && x0 <= boxes[i].2);
            match joined {
                Some(i) => {
                    let found = &mut boxes[i];
                    (found.1, found.2, found.4) = (found.1.min(x0), found.2.max(x1), py);
                    still.push(i);
                }
                None => {
                    boxes.push((url, x0, x1, py, py));
                    still.push(boxes.len() - 1);
                }
            }
        }
        open = still;
        py += STEP_Y;
    }
    // The badge goes on the link's first letter: walk back to its edges.
    boxes
        .into_iter()
        .map(|(url, x0, _, y0, y1)| {
            let mut start_x = x0;
            while start_x - 1.0 >= left && hit(start_x - 1.0, y0).as_deref() == Some(url.as_str()) && x0 - start_x < STEP_X {
                start_x -= 1.0;
            }
            let mut start_y = y0;
            while start_y - 1.0 >= top && hit(start_x, start_y - 1.0).as_deref() == Some(url.as_str()) && y0 - start_y < STEP_Y {
                start_y -= 1.0;
            }
            Piece { url, card: String::new(), x: x + start_x, y: y + start_y, bottom: y + y1 }
        })
        .collect()
}

/// Top to bottom, left to right; a link wrapped onto the next line keeps
/// one badge, on its first piece.
pub fn order(mut pieces: Vec<Piece>) -> Vec<Piece> {
    pieces.sort_by(|a, b| a.y.total_cmp(&b.y));
    // Lines: pieces whose tops lie within a probe row of the line's first.
    let mut lines: Vec<Vec<Piece>> = Vec::new();
    for piece in pieces {
        match lines.last_mut() {
            Some(line) if piece.y - line[0].y < STEP_Y => line.push(piece),
            _ => lines.push(vec![piece]),
        }
    }
    let mut kept: Vec<Piece> = Vec::new();
    for mut line in lines {
        line.sort_by(|a, b| a.x.total_cmp(&b.x));
        for piece in line {
            let continued = kept.iter_mut().rev().find(|k| k.target() == piece.target() && piece.y > k.y && piece.y - k.bottom < 2.0 * STEP_Y + 2.0);
            match continued {
                Some(first) => first.bottom = first.bottom.max(piece.bottom),
                None => kept.push(piece),
            }
        }
    }
    kept
}

/// The numbers for `pieces`: each link or card once, in the order it
/// first shows.
pub fn number(pieces: &[Piece]) -> Vec<String> {
    let mut links: Vec<String> = Vec::new();
    for piece in pieces {
        let target = piece.target();
        if !links.contains(&target) && links.len() < MOST {
            links.push(target);
        }
    }
    links
}

/// The artifact cards and images on screen in the open chat that a number
/// can act on, as pieces at their top-left corner (inside the chat's
/// viewport from `top`).
fn cards_on_screen(app: &App, top: f32) -> Vec<Piece> {
    crate::artifacts_view::on_screen(app)
        .into_iter()
        .filter(|id| id.starts_with("img:") || crate::artifacts_view::receipt_linked(app, id) || crate::artifacts_view::card_item(app, id).is_some_and(|c| !c.action.is_empty() || c.pending))
        .filter_map(|id| {
            let (x, y, _, height) = crate::artifacts_view::card_rect(app, &id)?;
            // Over the card's corner (its glyph), where it shows.
            let shown = y.max(top);
            (shown < y + height).then(|| Piece { url: String::new(), card: id, x: x + 6.0, y: shown + 4.0 + RAISE, bottom: shown + 24.0 })
        })
        .collect()
}

/// What `typed` does with `count` numbered links.
#[derive(Debug, PartialEq, Eq)]
pub enum Typed {
    /// Opens link n (1-based).
    Open(usize),
    /// A number that longer ones also start with: wait.
    Wait,
    /// Starts no number.
    Nothing,
}

pub fn resolve(typed: &str, count: usize, enter: bool) -> Typed {
    let starts = (1..=count).filter(|n| n.to_string().starts_with(typed)).count();
    match typed.parse::<usize>() {
        _ if typed.is_empty() || starts == 0 => Typed::Nothing,
        Ok(n) if (1..=count).contains(&n) && (enter || starts == 1) => Typed::Open(n),
        _ if enter => Typed::Nothing,
        _ => Typed::Wait,
    }
}

fn show(window: &AppWindow, state: &State) {
    let hints = window.global::<LinkHints>();
    let badges: Vec<LinkBadge> = state
        .pieces
        .iter()
        .filter_map(|piece| {
            let n = state.links.iter().position(|u| *u == piece.target())? + 1;
            let label = n.to_string();
            // Raised over the line's top so the link's letters still show
            // below it.
            let y = (piece.y - RAISE).max(state.top);
            Some(LinkBadge { dim: !label.starts_with(&state.typed), label: label.into(), url: piece.url.clone().into(), card: piece.card.clone().into(), x: piece.x, y })
        })
        .collect();
    hints.set_badges(ModelRc::new(VecModel::from(badges)));
    hints.set_typed(state.typed.clone().into());
    hints.set_active(true);
}

/// Numbers the links in the active pane's viewport. False when it shows
/// none (the bar says so briefly).
pub fn start(app: &App, window: &AppWindow) -> bool {
    let pane = app.active_id().to_string();
    let (mut pieces, top) = links_on_screen(window, &pane);
    pieces.extend(cards_on_screen(app, top));
    let pieces = order(pieces);
    let links = number(&pieces);
    let empty = links.is_empty();
    let state = State { links, pieces, typed: String::new(), offset: app.active_report().offset, pane, top };
    show(window, &state);
    STATE.with(|s| *s.borrow_mut() = Some(state));
    // Nothing to number: the bar says so for a moment, then the hints go.
    if empty {
        slint::Timer::single_shot(std::time::Duration::from_millis(1500), || {
            if let (Some(app), Some(window)) = (crate::app(), crate::window())
                && count() == 0
                && active()
            {
                cancel(&window);
                crate::commands::show_hints(&app, &window);
            }
        });
    }
    !empty
}

pub fn cancel(window: &AppWindow) {
    STATE.with(|s| s.borrow_mut().take());
    let hints = window.global::<LinkHints>();
    hints.set_active(false);
    hints.set_badges(ModelRc::default());
    hints.set_typed("".into());
}

/// How many links are numbered.
pub fn count() -> usize {
    STATE.with(|s| s.borrow().as_ref().map_or(0, |s| s.links.len()))
}

/// A key while the hints show (they take every key): true when it was used.
pub fn key(window: &AppWindow, action: Option<&str>, chord: &str) -> bool {
    let Some((mut typed, count)) = STATE.with(|s| s.borrow().as_ref().map(|s| (s.typed.clone(), s.links.len()))) else { return false };
    let enter = match action {
        Some("hint-digit") => {
            typed.push_str(chord);
            false
        }
        Some("hint-open") => true,
        Some("hint-back") => {
            typed.pop();
            false
        }
        _ => {
            cancel(window);
            return true;
        }
    };
    match resolve(&typed, count, enter) {
        Typed::Open(n) => {
            let target = STATE.with(|s| s.borrow().as_ref().and_then(|s| s.links.get(n - 1).cloned()));
            cancel(window);
            match target.as_deref().map(|t| t.strip_prefix("\u{1}card:").ok_or(t)) {
                // A card does what O does on it; a decision takes the chat's
                // keyboard so its digits answer it.
                Some(Ok(card)) => {
                    if let Some(app) = crate::app() {
                        crate::artifacts_view::activate(&app, window, card);
                        crate::commands::show_hints(&app, window);
                    }
                }
                Some(Err(url)) => crate::open_link(url),
                None => {}
            }
        }
        Typed::Wait => STATE.with(|s| {
            if let Some(state) = s.borrow_mut().as_mut() {
                state.typed = typed;
                show(window, state);
            }
        }),
        // A digit that starts no number is ignored; Backspace to nothing
        // shows every number again.
        Typed::Nothing => STATE.with(|s| {
            if let Some(state) = s.borrow_mut().as_mut()
                && typed.is_empty()
            {
                state.typed.clear();
                show(window, state);
            }
        }),
    }
    true
}

/// A pane reported where its chat is: the badges are drawn for the place
/// they were made, so a chat that moves (a scroll, a streaming reply it
/// follows, another pane) takes them away.
pub fn reported(window: &AppWindow, pane: &str, offset: f32, active: &str) -> bool {
    let stale = STATE.with(|s| s.borrow().as_ref().is_some_and(|s| active != s.pane || (s.pane == pane && (offset - s.offset).abs() > 0.5)));
    if stale {
        cancel(window);
    }
    stale
}

#[cfg(test)]
mod tests {
    use super::*;

    fn piece(url: &str, x: f32, y: f32) -> Piece {
        Piece { url: url.into(), card: String::new(), x, y, bottom: y + 18.0 }
    }

    #[test]
    fn links_are_numbered_top_to_bottom_once_each() {
        let pieces = order(vec![piece("c", 10.0, 80.0), piece("b", 200.0, 20.0), piece("a", 40.0, 21.0), piece("a", 10.0, 200.0)]);
        assert_eq!(pieces.iter().map(|p| p.url.as_str()).collect::<Vec<_>>(), ["a", "b", "c", "a"], "a line reads left to right");
        assert_eq!(number(&pieces), ["a", "b", "c"], "a link seen twice keeps its number");
        // A link wrapped onto the next line: one badge.
        let wrapped = order(vec![piece("w", 600.0, 20.0), piece("w", 0.0, 40.0)]);
        assert_eq!(wrapped.len(), 1);
    }

    #[test]
    fn a_number_opens_once_nothing_longer_starts_with_it() {
        assert_eq!(resolve("3", 6, false), Typed::Open(3), "up to nine, one digit opens");
        assert_eq!(resolve("7", 6, false), Typed::Nothing);
        assert_eq!(resolve("1", 12, false), Typed::Wait, "10, 11 and 12 start with 1");
        assert_eq!(resolve("1", 12, true), Typed::Open(1), "Enter takes the 1");
        assert_eq!(resolve("12", 12, false), Typed::Open(12));
        assert_eq!(resolve("5", 12, false), Typed::Open(5), "no 50s");
        assert_eq!(resolve("13", 12, false), Typed::Nothing);
        assert_eq!(resolve("0", 12, false), Typed::Nothing);
        assert_eq!(resolve("", 12, true), Typed::Nothing);
    }
}
