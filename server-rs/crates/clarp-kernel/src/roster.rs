//! The roster's edits: what the user changes about an agent after it is made.
//!
//! Its name, whether its replies page the user, whether it is put away, and which model it
//! runs with how much effort. And the list of the agents that are put away, which the apps
//! read a page at a time.
//!
//! These are the Python Host's rules (`server/lib/routes/agents.py`, `routes/voice.py`,
//! `snapshot.py`), in its words where it refuses.

use serde_json::Value;

/// The longest name an agent can be given.
pub const NAME_MAX: usize = 60;
/// What a name that is empty or too long is refused with.
pub const NAME_REFUSED: &str = "name required (maximum 60 characters)";

/// A name as it is kept: runs of white space are one space, and there is none at the ends.
/// A name that differs from another only in its spacing is not another contact. `None`:
/// nothing is left of it, or too much.
pub fn display_name(said: &str) -> Option<String> {
    let name = said.split_whitespace().collect::<Vec<_>>().join(" ");
    (!name.is_empty() && name.chars().count() <= NAME_MAX).then_some(name)
}

/// Whether two names are one contact: compared without the case of their letters and
/// without the white space at their ends. One agent has a name at a time: who is written
/// to by name must be one agent.
pub fn same_name(a: &str, b: &str) -> bool {
    a.trim().to_lowercase() == b.trim().to_lowercase()
}

/// An effort as it is kept: in small letters, nothing around it. Nothing said is the tool's
/// own choice. `None`: the tool has no such effort.
pub fn effort(said: &str, valid: &[&str]) -> Option<String> {
    let effort = said.trim().to_lowercase();
    (effort.is_empty() || valid.contains(&effort.as_str())).then_some(effort)
}

// ---- the agents that are put away, a page at a time ---------------------------------------

/// How many agents a page holds when nothing is said.
pub const PAGE_DEFAULT: usize = 50;
/// The most a page holds.
pub const PAGE_MAX: usize = 200;
const CURSOR_MAX: usize = 512;

/// Where an agent stands among the ones that are put away: when it was put away, then its
/// id. The list runs from the last put away to the first.
pub type Place = (i64, String);

/// The size of a page from what a request said: 1 to [`PAGE_MAX`], written in plain digits
/// without a leading zero. Nothing said is the usual size.
pub fn page_limit(said: &str) -> Option<usize> {
    if said.is_empty() {
        return Some(PAGE_DEFAULT);
    }
    let plain = said.len() <= 3 && said.bytes().all(|byte| byte.is_ascii_digit());
    if !plain || said.starts_with('0') {
        return None;
    }
    said.parse()
        .ok()
        .filter(|limit| (1..=PAGE_MAX).contains(limit))
}

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

fn encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let padded = [
            chunk[0],
            chunk.get(1).copied().unwrap_or(0),
            chunk.get(2).copied().unwrap_or(0),
        ];
        let bits =
            (u32::from(padded[0]) << 16) | (u32::from(padded[1]) << 8) | u32::from(padded[2]);
        for index in 0..4 {
            if index <= chunk.len() {
                let six = (bits >> (18 - 6 * index)) & 0x3f;
                out.push(char::from(ALPHABET[six as usize]));
            } else {
                out.push('=');
            }
        }
    }
    out
}

fn decode(text: &str) -> Option<Vec<u8>> {
    let bytes = text.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return None;
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for (at, chunk) in bytes.chunks(4).enumerate() {
        let last = (at + 1) * 4 == bytes.len();
        let padding = chunk.iter().rev().take_while(|byte| **byte == b'=').count();
        if padding > 2 || (padding > 0 && !last) {
            return None;
        }
        let mut bits = 0u32;
        for byte in &chunk[..4 - padding] {
            let six = ALPHABET.iter().position(|letter| letter == byte)?;
            bits = (bits << 6) | u32::try_from(six).ok()?;
        }
        bits <<= 6 * padding;
        let whole = bits.to_be_bytes();
        out.extend_from_slice(&whole[1..4 - padding]);
    }
    Some(out)
}

/// The cursor that names a place: what a page hands out for the page after it. Nothing in
/// it names a listing, so it means the same place in a later one.
pub fn cursor(place: &Place) -> String {
    // As the Python Host writes it: the two as a JSON list, a space after the comma.
    let written = format!("[{}, {}]", place.0, Value::String(place.1.clone()));
    encode(written.as_bytes())
}

/// The place a cursor names. `None`: this is no cursor a Host handed out.
pub fn place_of(cursor: &str) -> Option<Place> {
    if cursor.len() > CURSOR_MAX {
        return None;
    }
    let written: Value = serde_json::from_slice(&decode(cursor)?).ok()?;
    match written.as_array()?.as_slice() {
        [Value::Number(at), Value::String(agent_id)] => Some((at.as_i64()?, agent_id.clone())),
        _ => None,
    }
}

/// A page cut from the list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Page {
    /// The first agent of the page, counted from the start of the list.
    pub start: usize,
    /// One past the last.
    pub end: usize,
    /// The cursor for the page after this one, when there is one.
    pub next_cursor: Option<String>,
    /// How many agents are put away.
    pub total: usize,
}

/// The `limit` agents after `position`, the last agent of the page before (`None`: from
/// the start). `order` is every agent that is put away, the last put away first. The
/// position is a place in that order, not a count: an agent that was put away or taken out
/// since the page before moves nothing.
pub fn page(order: &[Place], limit: usize, position: Option<&Place>) -> Page {
    let start = position.map_or(0, |position| {
        order.iter().filter(|place| *place >= position).count()
    });
    let end = (start + limit).min(order.len());
    let more = start + limit < order.len();
    Page {
        start: start.min(order.len()),
        end,
        next_cursor: more.then(|| cursor(&order[start + limit - 1])),
        total: order.len(),
    }
}

/// The order of the list: the last put away first, and of two put away at once the one
/// with the later id.
pub fn listed(mut places: Vec<Place>) -> Vec<Place> {
    places.sort_by(|a, b| b.cmp(a));
    places
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_name_is_kept_without_its_spacing() {
        assert_eq!(
            display_name("  Ada   Lovelace \n"),
            Some("Ada Lovelace".to_string())
        );
        assert_eq!(display_name("Ada"), Some("Ada".to_string()));
        assert_eq!(display_name("   "), None);
        assert_eq!(display_name(""), None);
        let long = "n".repeat(NAME_MAX);
        assert_eq!(display_name(&long), Some(long.clone()));
        assert_eq!(display_name(&format!("{long}n")), None);
        // Sixty letters, whatever they take to write.
        let wide = "é".repeat(NAME_MAX);
        assert_eq!(display_name(&wide), Some(wide));
    }

    #[test]
    fn a_name_is_one_contact_whatever_its_case() {
        assert!(same_name("Ada", " ada "));
        assert!(same_name("ÉMILE", "émile"));
        assert!(!same_name("Ada", "Ada B"));
    }

    #[test]
    fn an_effort_is_one_the_tool_has_or_none() {
        let valid = ["low", "high"];
        assert_eq!(effort(" High ", &valid), Some("high".to_string()));
        assert_eq!(effort("", &valid), Some(String::new()));
        assert_eq!(effort("   ", &valid), Some(String::new()));
        assert_eq!(effort("extreme", &valid), None);
        assert_eq!(effort("low", &[]), None);
    }

    #[test]
    fn a_page_has_a_size_in_plain_digits() {
        assert_eq!(page_limit(""), Some(PAGE_DEFAULT));
        assert_eq!(page_limit("1"), Some(1));
        assert_eq!(page_limit("200"), Some(200));
        for refused in [
            "0", "201", "007", "+5", " 5", "5 ", "1_0", "٣", "1000", "abc", "-1",
        ] {
            assert_eq!(page_limit(refused), None, "{refused}");
        }
    }

    #[test]
    fn a_cursor_is_written_as_the_python_host_writes_it() {
        // base64.urlsafe_b64encode(json.dumps([1794749532207, "4578ba215ef1667a"]).encode())
        let place = (1_794_749_532_207, "4578ba215ef1667a".to_string());
        assert_eq!(
            cursor(&place),
            "WzE3OTQ3NDk1MzIyMDcsICI0NTc4YmEyMTVlZjE2NjdhIl0="
        );
        assert_eq!(place_of(&cursor(&place)), Some(place));
        // Every length of padding comes back.
        for id in ["a", "ab", "abc", "abcd", "ab?>~"] {
            let place = (7, id.to_string());
            assert_eq!(place_of(&cursor(&place)), Some(place));
        }
    }

    #[test]
    fn what_no_host_handed_out_is_no_cursor() {
        for refused in [
            "zzz",
            "====",
            "W10=",                     // []
            "WzEsIDJd",                 // [1, 2]
            "WyJhIiwgImIiXQ==",         // ["a", "b"]
            "W3RydWUsICJhIl0=",         // [true, "a"]
            "WzEuNSwgImEiXQ==",         // [1.5, "a"]
            "WzEsICJhIiwgMl0=",         // [1, "a", 2]
            "eyJhIjogMSwgImIiOiAyfQ==", // {"a": 1, "b": 2}
            "WzEsICJhIl0",              // without its padding
        ] {
            assert_eq!(place_of(refused), None, "{refused}");
        }
        assert_eq!(place_of(&"A".repeat(516)), None);
    }

    #[test]
    fn a_walk_through_the_pages_meets_every_agent_once() {
        let order = listed(vec![
            (10, "a".to_string()),
            (30, "c".to_string()),
            (30, "b".to_string()),
            (20, "d".to_string()),
            (5, "e".to_string()),
        ]);
        let ids: Vec<&str> = order.iter().map(|place| place.1.as_str()).collect();
        assert_eq!(ids, ["c", "b", "d", "a", "e"]);

        let first = page(&order, 2, None);
        assert_eq!((first.start, first.end, first.total), (0, 2, 5));
        let after_first = place_of(first.next_cursor.as_deref().unwrap()).unwrap();
        assert_eq!(after_first, (30, "b".to_string()));

        let second = page(&order, 2, Some(&after_first));
        assert_eq!((second.start, second.end), (2, 4));
        let after_second = place_of(second.next_cursor.as_deref().unwrap()).unwrap();

        let third = page(&order, 2, Some(&after_second));
        assert_eq!((third.start, third.end, third.next_cursor), (4, 5, None));

        // A page that ends where the list ends hands out no cursor.
        let whole = page(&order, 5, None);
        assert_eq!((whole.start, whole.end, whole.next_cursor), (0, 5, None));
        // An agent taken out since the page before moves nothing: the walk goes on after
        // the place the cursor names.
        let fewer: Vec<Place> = order
            .iter()
            .filter(|place| place.1 != "b")
            .cloned()
            .collect();
        let resumed = page(&fewer, 2, Some(&after_first));
        assert_eq!((resumed.start, resumed.end), (1, 3));
        // Past the end there is nothing.
        let past = page(&order, 2, Some(&(1, "a".to_string())));
        assert_eq!((past.start, past.end, past.next_cursor), (5, 5, None));
    }
}
