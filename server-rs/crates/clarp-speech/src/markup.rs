//! Voice markup: what of a reply is spoken, and what of it is shown.
//!
//! Agents write spoken replies with markup that must never reach a screen and is either spoken
//! or honoured by the speech engine:
//!
//! | markup | shown | spoken |
//! |---|---|---|
//! | `<speak>…</speak>` | the markers go, the text stays | only what is inside is spoken at all |
//! | `<vox>…</vox>` (fillers: um, well) | dropped whole, with the punctuation only the filler needed | the words are spoken |
//! | `<break/>` | dropped | kept for an engine that reads SSML, a space for one that does not |
//! | `<speed/>` `<volume/>` `<emotion/>` | dropped | dropped |
//! | `[laughing]` `[sigh]` | dropped inside `<speak>` | kept for an engine that acts them out, removed for the rest |
//! | `<team>…</team>` | dropped whole | dropped whole |
//! | `<oai-mem-citation>`, `<environment_context>` | dropped whole | dropped whole |
//!
//! This is a port of the Python Host's `server/lib/voice_markup.py`, function for function. The
//! two are held together by `tests/fixtures/markup-vectors.json`, which
//! `server-rs/tools/markup-vectors.py` writes from the Python module, and by the repository's
//! own contract for what is shown, `contract/fixtures/voice-display.json`.

use std::sync::LazyLock;

use regex::{Captures, Regex};

/// Stands where a filler was until the punctuation around it is settled.
const FILLER: char = '\u{E000}';
const SOFT_MARKS: &str = ",;:—–";
const END_MARKS: &str = ".!?…";
const WORD_STOPS: &str = "—–,;:!?…()[]\"“”<";
/// What may stand at either end of a filler and belongs to the sentence around it.
const FILLER_EDGE: &str = ",.;:!?…—–-";

/// The longest text one request to a speech engine is given. The smallest limit among the
/// engines is 2,000 characters; this leaves a margin.
pub const TTS_CHUNK_MAX_CHARS: usize = 1_800;

/// A pattern that is part of the program. One that does not compile is a mistake in this file,
/// and every test would say so.
#[allow(clippy::expect_used)]
fn pattern(text: &str) -> Regex {
    Regex::new(text).expect("a pattern of this module compiles")
}

macro_rules! patterns {
    ($($name:ident = $text:expr;)*) => {
        $(static $name: LazyLock<Regex> = LazyLock::new(|| pattern($text));)*
    };
}

patterns! {
    HIDDEN_BLOCK = r"(?is)<oai-mem-citation\b[^>]*>.*?</oai-mem-citation>|<environment_context\b[^>]*>.*?</environment_context>";
    HIDDEN_OPEN_TAIL = r"(?is)<(?:oai-mem-citation|environment_context)\b[^>]*>.*$";
    SPEAK_TAG = r"(?i)</?speak\b[^>]*>";
    VOX_TAG = r"(?i)</?vox\b[^>]*>";
    SSML = r"(?i)</?(?:break|speed|volume|emotion)\b[^>]*/?>";
    // A lowercase word or two in square brackets. What follows decides whether it is a tag:
    // see `emotion_tags`.
    EMOTION_TAG = r"\[[a-z][a-z' -]{2,30}\]";
    SPEAK_BLOCK_BARE = r"(?is)<speak>(.*?)</speak>";
    SPEAK_BLOCK_ATTR = r"(?is)<speak\b[^>]*>(.*?)</speak>";
    SPEAK_REGION = r"(?is)(<speak\b[^>]*>)(.*?)(</speak>|$)";
    TTS_DROP_SSML = r"(?i)</?(?:speed|volume|emotion)\b[^>]*/?>";
    TEAM_BLOCK = r"(?is)<team\b[^>]*>.*?</team>";
    TEAM_OPEN_TAIL = r"(?is)<team\b[^>]*(?:>.*)?$";
    INLINE_WS = r"[ \t]{2,}";
    SPACE_BEFORE_PUNCT = r"[ \t]+([,.;:!?])";
    VOX_CONTENT = r"(?is)<vox\b[^>]*>(.*?)</vox>";
    VOX_RUN = "\u{E000}(?:[ \\t]*[,;:—–]?[ \\t]*\u{E000})+";
    LINE_OPENING = r"^[ \t]*(?:(?:[-*+>]|\d+[.)]|#{1,6})[ \t]+)*(?:\*\*|__|\*|_)?[ \t]*$";
    REPEATED_MARKS = r"([,;:])(?:\s*[,;:])+";
    BREAK_TAG = r"(?i)<break\b[^>]*?/?>";
}

fn is_blank(c: char) -> bool {
    c == ' ' || c == '\t'
}

/// Replaces every emotion tag with `with`. A bracketed word that is followed by `(` or `:` is
/// a markdown link or a reference, and stays.
fn replace_emotion_tags(text: &str, with: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut copied = 0;
    for found in EMOTION_TAG.find_iter(text) {
        let next = text[found.end()..].chars().next();
        if matches!(next, Some('(' | ':')) {
            continue;
        }
        out.push_str(&text[copied..found.start()]);
        out.push_str(with);
        copied = found.end();
    }
    out.push_str(&text[copied..]);
    out
}

/// Removes the blocks that are metadata and never conversation, also one that is still
/// arriving and has no end yet.
pub fn strip_hidden_blocks(text: &str) -> String {
    let closed = HIDDEN_BLOCK.replace_all(text, "");
    HIDDEN_OPEN_TAIL.replace_all(&closed, "").into_owned()
}

/// Hides team messages from users, including an open tail in a streaming reply.
pub fn strip_team_blocks(text: &str) -> std::borrow::Cow<'_, str> {
    let closed = TEAM_BLOCK.replace_all(text, "");
    if TEAM_OPEN_TAIL.is_match(&closed) {
        let result = TEAM_OPEN_TAIL.replace_all(&closed, "").into_owned();
        std::borrow::Cow::Owned(result)
    } else {
        closed
    }
}

/// Only a plain lowercase word is given a capital: "iOS", "npm" in backticks, file.py stay as
/// they were written.
fn capitalize_sentence(rest: &[char]) -> Vec<char> {
    let end = rest
        .iter()
        .position(|c| c.is_whitespace() || WORD_STOPS.contains(*c))
        .unwrap_or(rest.len());
    let mut word = &rest[..end];
    while let [head @ .., last] = word {
        if !".'’*_".contains(*last) {
            break;
        }
        word = head;
    }
    let plain = word.first().is_some_and(|c| c.is_lowercase())
        && word.iter().all(|c| c.is_lowercase() || "'’-".contains(*c));
    if !plain {
        return rest.to_vec();
    }
    rest[0]
        .to_uppercase()
        .chain(rest[1..].iter().copied())
        .collect()
}

/// What stands to the right of a filler.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Right {
    /// A comma, a semicolon, a dash: a mark inside the sentence.
    Soft,
    /// A full stop, a question mark: a mark that ends it.
    EndMark,
    LineEnd,
    Word,
}

/// Removes the filler marker at `i` with the punctuation that only the filler needed.
fn close_vox_gap(s: &[char], i: usize) -> Vec<char> {
    let at = |k: usize| s.get(k).copied();
    let blank_at = |k: usize| at(k).is_some_and(is_blank);
    let mut j = i;
    while j > 0 && is_blank(s[j - 1]) {
        j -= 1;
    }
    let mut m = i + 1;
    while blank_at(m) {
        m += 1;
    }
    let space = j < i || m > i + 1;
    let right = at(m);
    let mut after = m + 1;
    let kind = match right {
        Some(c)
            if SOFT_MARKS.contains(c) || (c == '-' && (at(m + 1).is_none() || blank_at(m + 1))) =>
        {
            while blank_at(after) {
                after += 1;
            }
            Right::Soft
        }
        Some(c) if END_MARKS.contains(c) => Right::EndMark,
        None | Some('\n') => Right::LineEnd,
        Some(_) => Right::Word,
    };
    let from = |k: usize| &s[k.min(s.len())..];
    let left = j.checked_sub(1).map(|k| s[k]);
    let line_start = s[..i].iter().rposition(|c| *c == '\n').map_or(0, |k| k + 1);
    let line_before: String = s[line_start..i].iter().collect();
    let mut out = s[..j].to_vec();
    if LINE_OPENING.is_match(&line_before) || left.is_some_and(|c| ".!?".contains(c)) {
        // The filler opened a sentence: its comma or stop goes with it, and the next word
        // starts the sentence.
        if kind == Right::EndMark {
            while at(after).is_some_and(|c| END_MARKS.contains(c) || is_blank(c)) {
                after += 1;
            }
        }
        let rest = if matches!(kind, Right::Soft | Right::EndMark) {
            from(after)
        } else {
            from(m)
        };
        if j < i && rest.first().is_some_and(|c| *c != '\n') {
            out.push(' ');
        }
        out.extend(capitalize_sentence(rest));
        return out;
    }
    let dash_after_blank = left == Some('-') && j >= 2 && is_blank(s[j - 2]);
    if left.is_some_and(|c| SOFT_MARKS.contains(c)) || dash_after_blank {
        if matches!(kind, Right::EndMark | Right::LineEnd) {
            // A comma left dangling before a stop or a line end goes too.
            let mut k = j - 1;
            while k > 0 && is_blank(s[k - 1]) {
                k -= 1;
            }
            out.truncate(k);
            out.extend_from_slice(from(m));
            return out;
        }
        // The mark before the filler is the sentence's own: it stays, and the filler's
        // second one goes.
        out.push(' ');
        out.extend_from_slice(if kind == Right::Soft {
            from(after)
        } else {
            from(m)
        });
        return out;
    }
    let rest = if kind == Right::Soft && right == Some(',') {
        from(after)
    } else {
        from(m)
    };
    let closes = rest
        .first()
        .is_none_or(|c| *c == '\n' || ",;:.!?…".contains(*c));
    if space && !closes {
        out.push(' ');
    }
    out.extend_from_slice(rest);
    out
}

/// What a filler leaves behind: the punctuation that was written inside it at its edges
/// belongs to the sentence around it, and a marker stands where its words were.
fn filler_marker(inner: &str) -> String {
    let edge = |c: char| c.is_whitespace() || FILLER_EDGE.contains(c);
    let core_start = inner.find(|c| !edge(c)).unwrap_or(inner.len());
    let (lead, rest) = inner.split_at(core_start);
    let core_end = rest
        .char_indices()
        .rev()
        .find(|(_, c)| !edge(*c))
        .map_or(0, |(at, c)| at + c.len_utf8());
    let (core, trail) = rest.split_at(core_end);
    if core.is_empty() {
        format!("{lead}{trail}")
    } else {
        format!("{lead}{FILLER}{trail}")
    }
}

/// Hides `<vox>` fillers from what is shown, with the punctuation only the filler needed.
fn drop_vox_for_display(text: &str) -> String {
    let marked = VOX_CONTENT.replace_all(text, |caps: &Captures| filler_marker(&caps[1]));
    if !marked.contains(FILLER) {
        return marked.into_owned();
    }
    let merged = VOX_RUN.replace_all(&marked, FILLER.to_string().as_str());
    let mut s: Vec<char> = merged.chars().collect();
    while let Some(i) = s.iter().position(|c| *c == FILLER) {
        s = close_vox_gap(&s, i);
    }
    s.into_iter().collect()
}

/// The inner text of each closed `<speak>…</speak>` block, in order and as written.
/// `attributes` also takes an opening tag with attributes (`<speak lang="en">`); without it
/// only a bare `<speak>` counts. A turn's spoken blocks are read without attributes, as the
/// Python Host reads them.
pub fn speak_blocks(text: &str, attributes: bool) -> Vec<String> {
    let blocks = if attributes {
        &*SPEAK_BLOCK_ATTR
    } else {
        &*SPEAK_BLOCK_BARE
    };
    blocks
        .captures_iter(text)
        .map(|caps| caps[1].to_string())
        .collect()
}

/// An emotion tag that stands bare inside `<speak>` is not shown either.
fn drop_spoken_emotion_tags(text: &str) -> String {
    if !text.contains('[') {
        return text.to_string();
    }
    SPEAK_REGION
        .replace_all(text, |caps: &Captures| {
            format!(
                "{}{}{}",
                &caps[1],
                replace_emotion_tags(&caps[2], " "),
                &caps[3]
            )
        })
        .into_owned()
}

/// Removes the emotion tags for a speech engine that would read them aloud. Each becomes a
/// space, and the gaps that leaves are tidied.
pub fn strip_emotion_tags(text: &str) -> String {
    if !text.contains('[') {
        return text.to_string();
    }
    let s = replace_emotion_tags(text, " ");
    let s = INLINE_WS.replace_all(&s, " ");
    let s = REPEATED_MARKS.replace_all(s.trim(), "$1");
    SPACE_BEFORE_PUNCT.replace_all(&s, "$1").into_owned()
}

/// A text as it is shown: in a chat, in the list of chats, in a notification. All voice markup
/// is gone. `oneline` makes one line of it, for a preview; otherwise the lines stay and only
/// the gaps that removed markup left inside a line are closed.
pub fn clean_for_display(text: &str, oneline: bool) -> String {
    if text.is_empty() {
        return String::new();
    }
    let s = strip_hidden_blocks(text);
    let s = TEAM_BLOCK.replace_all(&s, "");
    let s = drop_spoken_emotion_tags(&s);
    let s = SPEAK_TAG.replace_all(&s, "");
    let s = SSML.replace_all(&s, "");
    let s = drop_vox_for_display(&s);
    let s = if oneline {
        s.split_whitespace().collect::<Vec<_>>().join(" ")
    } else {
        INLINE_WS.replace_all(&s, " ").trim().to_string()
    };
    SPACE_BEFORE_PUNCT.replace_all(&s, "$1").into_owned()
}

/// A spoken text as a speech engine is given it: the fillers' words stay, the pauses stay,
/// and what no engine honours reliably goes. (Taking the `<speak>` blocks out of a reply
/// comes before this.)
pub fn spoken_for_tts(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let s = strip_hidden_blocks(text);
    let s = TEAM_BLOCK.replace_all(&s, "");
    let s = VOX_TAG.replace_all(&s, "");
    let s = TTS_DROP_SSML.replace_all(&s, "");
    let s = INLINE_WS.replace_all(&s, " ");
    SPACE_BEFORE_PUNCT.replace_all(s.trim(), "$1").into_owned()
}

/// Removes every pause and style tag for an engine that does not read SSML. Each becomes one
/// space, never nothing: `one<break/>two` must be spoken as "one two".
pub fn strip_ssml_for_plain_tts(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let s = SSML.replace_all(text, " ");
    let s = s.split_whitespace().collect::<Vec<_>>().join(" ");
    SPACE_BEFORE_PUNCT.replace_all(&s, "$1").into_owned()
}

/// Cuts a text into sentences: after a mark that ends one, at the white space that follows.
fn sentences(text: &[char]) -> Vec<&[char]> {
    let mut parts = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i < text.len() {
        if text[i].is_whitespace() && i > 0 && END_MARKS.contains(text[i - 1]) {
            let mut end = i;
            while end < text.len() && text[end].is_whitespace() {
                end += 1;
            }
            parts.push(&text[start..i]);
            start = end;
            i = end;
        } else {
            i += 1;
        }
    }
    parts.push(&text[start..]);
    parts
}

fn trimmed(text: &[char]) -> &[char] {
    let start = text
        .iter()
        .position(|c| !c.is_whitespace())
        .unwrap_or(text.len());
    let end = text
        .iter()
        .rposition(|c| !c.is_whitespace())
        .map_or(start, |at| at + 1);
    &text[start..end]
}

/// A spoken text in pieces no longer than `max_chars`, cut at the ends of sentences where
/// that is possible and never inside a pause tag. Nothing is left out: the pieces, joined by
/// spaces, are the text.
pub fn spoken_chunks_for_tts(text: &str, max_chars: usize) -> Vec<String> {
    let clean = spoken_for_tts(text);
    if clean.is_empty() {
        return Vec::new();
    }
    let limit = max_chars.max(100);
    // A space inside a pause tag is no place to cut: it is made one that is not looked for.
    let protected: Vec<char> = BREAK_TAG
        .replace_all(&clean, |caps: &Captures| caps[0].replace(' ', "\u{A0}"))
        .chars()
        .collect();
    let mut chunks: Vec<Vec<char>> = Vec::new();
    let mut current: Vec<char> = Vec::new();
    fn append(piece: &[char], current: &mut Vec<char>, chunks: &mut Vec<Vec<char>>, limit: usize) {
        let piece = trimmed(piece);
        if piece.is_empty() {
            return;
        }
        if !current.is_empty() && current.len() + 1 + piece.len() > limit {
            chunks.push(std::mem::take(current));
            current.extend_from_slice(piece);
        } else {
            if !current.is_empty() {
                current.push(' ');
            }
            current.extend_from_slice(piece);
        }
    }
    for sentence in sentences(&protected) {
        let mut remaining = trimmed(sentence);
        while remaining.len() > limit {
            let cut = remaining[..=limit]
                .iter()
                .rposition(|c| *c == ' ')
                .filter(|at| *at > 0)
                .unwrap_or(limit);
            append(&remaining[..cut], &mut current, &mut chunks, limit);
            if !current.is_empty() {
                chunks.push(std::mem::take(&mut current));
            }
            remaining = trimmed(&remaining[cut..]);
        }
        append(remaining, &mut current, &mut chunks, limit);
    }
    if !current.is_empty() {
        chunks.push(current);
    }
    chunks
        .into_iter()
        .map(|chunk| {
            chunk
                .into_iter()
                .map(|c| if c == '\u{A0}' { ' ' } else { c })
                .collect()
        })
        .collect()
}

/// What of a reply is spoken: the text of each of its `<speak>` blocks, each in pieces a
/// speech engine can take. The second of a pair says whether the piece starts a block.
pub fn spoken_pieces(reply: &str) -> Vec<(String, bool)> {
    let mut pieces = Vec::new();
    for block in speak_blocks(reply, false) {
        for (index, chunk) in spoken_chunks_for_tts(block.trim(), TTS_CHUNK_MAX_CHARS)
            .into_iter()
            .enumerate()
        {
            pieces.push((chunk, index == 0));
        }
    }
    pieces
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn what_is_shown_has_no_voice_markup() {
        assert_eq!(
            clean_for_display("<speak>hello there</speak>", false),
            "hello there"
        );
        assert_eq!(
            clean_for_display("So <vox>um,</vox> it's done", false),
            "So it's done"
        );
        assert_eq!(
            clean_for_display(
                "<speak>Oh <vox>[laughing]</vox> that worked. [sigh] Finally.</speak>\nSee [the docs](https://x.y) and [x] done [1].",
                false
            ),
            "Oh that worked. Finally.\nSee [the docs](https://x.y) and [x] done [1]."
        );
        assert_eq!(clean_for_display("", true), "");
        assert_eq!(
            clean_for_display("a\n\n b  <break time=\"1s\"/> c", true),
            "a b c"
        );
    }

    #[test]
    fn what_is_metadata_is_neither_shown_nor_spoken() {
        let raw = "Visible reply.\n<oai-mem-citation><citation_entries>x</citation_entries></oai-mem-citation>";
        assert_eq!(strip_hidden_blocks(raw), "Visible reply.\n");
        assert_eq!(clean_for_display(raw, false), "Visible reply.");
        assert_eq!(spoken_for_tts(raw), "Visible reply.");
        // One that is still arriving has no end yet, and a block ends with its own tag only.
        assert_eq!(
            strip_hidden_blocks("Visible.\n<oai-mem-citation><citation_entries>partial"),
            "Visible.\n"
        );
        assert_eq!(
            spoken_for_tts(
                "Visible. <environment_context>private </oai-mem-citation>still private</environment_context>"
            ),
            "Visible."
        );
        assert!(
            !spoken_for_tts("All done. <team>refactored the parser</team> Cheers.")
                .contains("parser")
        );
    }

    #[test]
    fn what_is_spoken_keeps_the_fillers_words_and_the_pauses() {
        let out = spoken_for_tts(
            r#"<vox>um</vox> ready <break time="300ms"/><speed ratio="0.9">now</speed>"#,
        );
        assert_eq!(out, r#"um ready <break time="300ms"/>now"#);
        assert_eq!(
            strip_ssml_for_plain_tts(r#"one<break time="300ms"/>two"#),
            "one two"
        );
        assert_eq!(
            strip_ssml_for_plain_tts(
                r#"ready <break time="350ms"/> now, <speed ratio="0.9">go</speed><volume value="+2dB">loud</volume><emotion name="calm"/>"#
            ),
            "ready now, go loud"
        );
        let spoken = spoken_for_tts("Oh, <vox>[laughing]</vox> that worked.");
        assert_eq!(spoken, "Oh, [laughing] that worked.");
        assert_eq!(strip_emotion_tags(&spoken), "Oh, that worked.");
        assert_eq!(
            strip_emotion_tags(r#"Done [sigh]. <break time="300ms"/> Next"#),
            r#"Done. <break time="300ms"/> Next"#
        );
        assert_eq!(strip_emotion_tags("keep [x] and [1]"), "keep [x] and [1]");
    }

    #[test]
    fn a_long_spoken_text_is_cut_into_pieces_and_nothing_is_lost() {
        let text = (0..120)
            .map(|n| {
                format!("Sentence {n} contains enough words to exercise provider-safe splitting.")
            })
            .collect::<Vec<_>>()
            .join(" ");
        let chunks = spoken_chunks_for_tts(&text, 500);
        assert!(chunks.len() > 1);
        assert!(chunks.iter().all(|chunk| {
            let n = chunk.chars().count();
            n > 0 && n <= 500
        }));
        assert_eq!(chunks.join(" "), spoken_for_tts(&text));
        // Never inside a pause tag.
        let text = format!(
            "{} <break time=\"350ms\"/> {} tail words here.",
            "A".repeat(75),
            "B".repeat(75)
        );
        let chunks = spoken_chunks_for_tts(&text, 100);
        assert!(
            chunks.iter().all(
                |chunk| !chunk.contains("<break") || chunk.contains(r#"<break time="350ms"/>"#)
            )
        );
        assert_eq!(chunks.join(" "), spoken_for_tts(&text));
        assert!(spoken_chunks_for_tts("", 500).is_empty());
    }

    #[test]
    fn only_what_is_inside_speak_is_spoken_block_by_block() {
        let reply = "Working on it.\n<speak>First block. <vox>um</vox> still first.</speak>\nA table nobody reads aloud.\n<speak>Second block!</speak>";
        assert_eq!(
            spoken_pieces(reply),
            vec![
                ("First block. um still first.".to_string(), true),
                ("Second block!".to_string(), true)
            ]
        );
        assert!(spoken_pieces("Nothing here is for the ear.").is_empty());
        assert!(
            spoken_pieces("<speak>not closed yet").is_empty(),
            "a block is spoken when it is whole"
        );
        assert_eq!(
            speak_blocks(r#"<speak lang="en">x</speak>"#, false),
            Vec::<String>::new()
        );
        assert_eq!(
            speak_blocks(r#"<speak lang="en">x</speak>"#, true),
            vec!["x"]
        );
    }
}
