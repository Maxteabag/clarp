//! Finding words in one open chat (the find bar): which rows hold them and
//! where in a row's text they are, by the Host's rule (`GET /search`): each
//! word of the query matches the start of a word, ignoring case and accents
//! ("pa" finds "på", "oslo" finds "Oslofjorden"), while ø and æ are letters
//! of their own. A row matches when it holds every word.
//!
//! A Host that lists `FEATURE` answers a chat's search with every matching
//! row's id in chat order (`Positions`); without it the ids are gathered
//! from pages of hits, and on a Host without message search the loaded
//! rows are searched here.

use std::ops::Range;

use pulldown_cmark::{Event, LinkType, Options, Parser, Tag, TagEnd};

/// The feature a Host lists when `GET /search?session=S&positions=1`
/// answers with every matching row of the chat.
pub const FEATURE: &str = "search_positions";

/// A letter without its accent, lower case: å, ä, é, ñ … are their base
/// letter; ø, æ, ß and letters outside Latin stay as they are.
fn base(c: char) -> char {
    match c {
        'à'..='å' | 'ā' | 'ă' | 'ą' => 'a',
        'ç' | 'ć' | 'ĉ' | 'ċ' | 'č' => 'c',
        'ď' => 'd',
        'è'..='ë' | 'ē' | 'ĕ' | 'ė' | 'ę' | 'ě' => 'e',
        'ĝ' | 'ğ' | 'ġ' | 'ģ' => 'g',
        'ĥ' => 'h',
        'ì'..='ï' | 'ĩ' | 'ī' | 'ĭ' | 'į' => 'i',
        'ĵ' => 'j',
        'ķ' => 'k',
        'ĺ' | 'ļ' | 'ľ' => 'l',
        'ñ' | 'ń' | 'ņ' | 'ň' => 'n',
        'ò'..='ö' | 'ō' | 'ŏ' | 'ő' => 'o',
        'ŕ' | 'ŗ' | 'ř' => 'r',
        'ś' | 'ŝ' | 'ş' | 'š' => 's',
        'ţ' | 'ť' => 't',
        'ù'..='ü' | 'ũ' | 'ū' | 'ŭ' | 'ů' | 'ű' | 'ų' => 'u',
        'ŵ' => 'w',
        'ý' | 'ÿ' | 'ŷ' => 'y',
        'ź' | 'ż' | 'ž' => 'z',
        _ => c,
    }
}

/// A combining accent (written after its letter): ignored.
fn combining(c: char) -> bool {
    matches!(c, '\u{300}'..='\u{36f}')
}

fn fold(c: char) -> char {
    base(c.to_lowercase().next().unwrap_or(c))
}

/// The words of a query as they are matched: folded, each run of letters
/// and digits its own word (what has none is no word).
pub fn terms(query: &str) -> Vec<String> {
    let mut terms: Vec<String> = Vec::new();
    let mut word = String::new();
    for c in query.chars().filter(|c| !combining(*c)) {
        if c.is_alphanumeric() {
            word.push(fold(c));
        } else if !word.is_empty() {
            terms.push(std::mem::take(&mut word));
        }
    }
    if !word.is_empty() {
        terms.push(word);
    }
    terms.dedup();
    terms
}

/// `text` folded, each character with the bytes it came from (a combining
/// accent joins the letter before it).
fn folded(text: &str) -> Vec<(Range<usize>, char)> {
    let mut out: Vec<(Range<usize>, char)> = Vec::with_capacity(text.len());
    for (at, c) in text.char_indices() {
        let end = at + c.len_utf8();
        if combining(c) {
            if let Some(last) = out.last_mut() {
                last.0.end = end;
            }
            continue;
        }
        out.push((at..end, fold(c)));
    }
    out
}

/// Where in `text` (bytes) a word of `terms` starts a word, in order, none
/// overlapping (the longest at a place wins).
pub fn ranges(text: &str, terms: &[String]) -> Vec<Range<usize>> {
    let terms: Vec<Vec<char>> = terms.iter().filter(|t| !t.is_empty()).map(|t| t.chars().collect()).collect();
    if terms.is_empty() || text.is_empty() {
        return Vec::new();
    }
    let folded = folded(text);
    let mut found = Vec::new();
    let mut at = 0;
    while at < folded.len() {
        let word_start = at == 0 || !folded[at - 1].1.is_alphanumeric();
        let hit = word_start
            .then(|| terms.iter().filter(|t| folded.len() - at >= t.len() && folded[at..at + t.len()].iter().map(|f| f.1).eq(t.iter().copied())).map(Vec::len).max())
            .flatten();
        match hit {
            Some(len) => {
                found.push(folded[at].0.start..folded[at + len - 1].0.end);
                at += len;
            }
            None => at += 1,
        }
    }
    found
}

/// `text` holds every word of `terms`, each at the start of a word.
pub fn matches(text: &str, terms: &[String]) -> bool {
    if terms.is_empty() {
        return false;
    }
    let folded = folded(text);
    terms.iter().filter(|t| !t.is_empty()).all(|term| {
        let term: Vec<char> = term.chars().collect();
        (0..folded.len()).any(|at| {
            (at == 0 || !folded[at - 1].1.is_alphanumeric()) && folded.len() - at >= term.len() && folded[at..at + term.len()].iter().map(|f| f.1).eq(term.iter().copied())
        })
    })
}

/// `markdown` with every place a word of `terms` shows in its text wrapped
/// in `open` and `close` (inline HTML), as Slint's styled text reads it.
/// Code, link addresses and autolinks are left alone: only words that show
/// as text are marked.
pub fn highlight_markdown(markdown: &str, terms: &[String], open: &str, close: &str) -> String {
    if terms.is_empty() {
        return markdown.to_owned();
    }
    let mut inserts: Vec<(usize, bool)> = Vec::new();
    let mut autolink = 0usize;
    let mut image = 0usize;
    for (event, range) in Parser::new_ext(markdown, Options::ENABLE_STRIKETHROUGH).into_offset_iter() {
        match event {
            Event::Start(Tag::Link { link_type: LinkType::Autolink | LinkType::Email, .. }) => autolink += 1,
            Event::End(TagEnd::Link) if autolink > 0 => autolink -= 1,
            Event::Start(Tag::Image { .. }) => image += 1,
            Event::End(TagEnd::Image) if image > 0 => image -= 1,
            Event::Text(text) if autolink == 0 && image == 0 => {
                let source = &markdown[range.clone()];
                // The text as written: escapes and entities make it differ,
                // and then only a text found once in its source is marked.
                let offset = if source == &*text {
                    Some(0)
                } else {
                    source.find(&*text).filter(|at| !text.is_empty() && !source[at + text.len()..].contains(&*text))
                };
                let Some(offset) = offset else { continue };
                for hit in ranges(&text, terms) {
                    inserts.push((range.start + offset + hit.start, true));
                    inserts.push((range.start + offset + hit.end, false));
                }
            }
            _ => {}
        }
    }
    if inserts.is_empty() {
        return markdown.to_owned();
    }
    let mut out = String::with_capacity(markdown.len() + inserts.len() * (open.len() + close.len()) / 2);
    let mut from = 0;
    for (at, opening) in inserts {
        out.push_str(&markdown[from..at]);
        out.push_str(if opening { open } else { close });
        from = at;
    }
    out.push_str(&markdown[from..]);
    out
}

/// A chat's matching rows as the Host lists them (`positions=1`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Positions {
    /// Row ids (`GET /log` ids), oldest first.
    pub ids: Vec<String>,
    /// How many rows of the chat match (more than `ids` when cut).
    pub total: usize,
    /// Only the newest are listed.
    pub truncated: bool,
}

impl Positions {
    /// The positions of a `/search` answer; None when it has none.
    pub fn from_json(object: &crate::json::Object) -> Option<Self> {
        let list = object.get("positions")?.as_array()?;
        let ids: Vec<String> = list
            .iter()
            .filter_map(|p| p.get("message_id").and_then(serde_json::Value::as_str))
            .filter(|id| !id.is_empty())
            .map(str::to_owned)
            .collect();
        let total = object.get("total").and_then(serde_json::Value::as_u64).map_or(ids.len(), |t| t as usize).max(ids.len());
        let truncated = crate::json::boolean(object, "positions_truncated");
        Some(Self { ids, total, truncated })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(query: &str) -> Vec<String> {
        terms(query)
    }

    #[test]
    fn words_match_at_a_word_start_without_case_or_accents_but_ø_and_æ_are_letters() {
        assert!(matches("Vi tar møtet på kontoret", &t("pa")));
        assert!(matches("Shall we meet in Oslofjorden?", &t("oslo")));
        assert!(matches("Café au lait", &t("CAFE")));
        assert!(!matches("kontoret", &t("tor")), "inside a word is no match");
        assert!(!matches("møtet", &t("motet")), "ø is not o");
        assert!(!matches("blåbær", &t("blabaer")), "æ is not ae");
        assert!(matches("blåbær", &t("blabær")));
        assert!(matches("a\u{30a}pen dør", &t("apen")), "a decomposed accent is ignored too");
        assert!(matches("Oslo on Friday", &t("friday oslo")), "every word, in any order");
        assert!(!matches("Oslo on Monday", &t("friday oslo")));
        assert!(!matches("anything", &t("  ")), "no words match nothing");
        assert_eq!(t("e-post, OSLO"), ["e", "post", "oslo"]);
    }

    #[test]
    fn ranges_are_where_the_words_show() {
        let text = "Møtet i Oslo, på Oslofjorden";
        let found: Vec<&str> = ranges(text, &t("oslo pa")).into_iter().map(|r| &text[r]).collect();
        assert_eq!(found, ["Oslo", "på", "Oslo"]);
        let decomposed = "a\u{30a}pen";
        assert_eq!(ranges(decomposed, &t("ap")), [0..4], "the accent belongs to its letter");
    }

    #[test]
    fn markdown_is_marked_in_its_text_only() {
        let open = "<u>";
        let close = "</u>";
        let mark = |md: &str, q: &str| highlight_markdown(md, &t(q), open, close);
        assert_eq!(mark("Meet in **Oslo** today", "oslo"), "Meet in **<u>Oslo</u>** today");
        assert_eq!(mark("see `oslo` and [Oslo map](https://oslo.no)", "oslo"), "see `oslo` and [<u>Oslo</u> map](https://oslo.no)", "not in code or the address");
        assert_eq!(mark("<https://oslo.no> Oslo", "oslo"), "<https://oslo.no> <u>Oslo</u>", "not in an autolink");
        assert_eq!(mark("a \\*Oslo\\* b", "oslo"), "a \\*<u>Oslo</u>\\* b", "escaped text still marked");
        assert_eq!(mark("nothing here", "oslo"), "nothing here");
    }

    #[test]
    fn positions_read_ids_total_and_truncation() {
        let answer = serde_json::json!({
            "hits": [],
            "total": 7,
            "positions": [{"message_id": "m1", "revision": 1, "timestamp": "2026-11-25T10:02:03.000Z"}, {"message_id": ""}, {"message_id": "m2"}],
            "positions_truncated": true
        });
        let positions = Positions::from_json(answer.as_object().unwrap()).unwrap();
        assert_eq!(positions, Positions { ids: vec!["m1".into(), "m2".into()], total: 7, truncated: true });
        assert_eq!(Positions::from_json(serde_json::json!({"hits": []}).as_object().unwrap()), None);
    }
}
