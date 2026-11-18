#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The relay of a long text to the Oracle's voice: the kernel's rules.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'oracle_relay_vectors'],
}
K = 'clarp-kernel/src/oracle/relay.rs'
# NOT PLANTED, each with its reason:
#   that the longest head is counted for both ways of reading (`for exact in [true, false]`): the words for a
#     summary are the longer, so counting them alone is the same; the Python Host counts both, and so does this.
#   the numbers the longest head is counted with (part 11 of 99): one more or less of them is the same length.
#   where the search for the next marked piece goes on (after `</speak>`): before it finds the same pieces.
PLANTS = [
    # ---- how a text is cut
    (K, 'vectors', 'a piece may hold less than one sign', '    if max_bytes < CHUNK_LEAST {', '    if max_bytes < 1 {'),
    (K, 'vectors', 'a piece holds a byte less than it may', '            if after > max_bytes {', '            if after >= max_bytes {'),
    (K, 'vectors', 'a text is cut after its first blank', '            if python::blank(sign) && after >= max_bytes / 2 {', '            if python::blank(sign) {'),
    (K, 'vectors', 'only a space is a blank to cut after', '            if python::blank(sign) && after >= max_bytes / 2 {', "            if sign == ' ' && after >= max_bytes / 2 {"),
    (K, 'vectors', 'a text is cut where the bytes run out, whatever blanks it has', '            if python::blank(sign) && after >= max_bytes / 2 {', '            if after >= max_bytes / 2 {'),
    (K, 'vectors', 'a piece may be a quarter full where it is cut at a blank', '            if python::blank(sign) && after >= max_bytes / 2 {', '            if python::blank(sign) && after >= max_bytes / 4 {'),
    (K, 'vectors', 'the last piece is cut at a blank though the rest fits', '        if end < remaining.len() && preferred > 0 {', '        if preferred > 0 {'),
    # ---- how a relay is sized
    (K, 'vectors', 'the room for a text is counted without what was asked', '            worst = worst.max(relay.head(10, 99, every, true).chars().count());', '            worst = worst.max(relay.head(10, 99, every, false).chars().count());'),
    (K, 'vectors', 'the room for a text is counted for the exact words only', '        for exact in [true, false] {', '        for exact in [true] {'),
    (K, 'vectors', 'the room for a text is counted without that it was served', '            served: true,\n            resumed: true,\n            pause_after: true,\n        };\n        let mut worst = 0;', '            served: false,\n            resumed: true,\n            pause_after: true,\n        };\n        let mut worst = 0;'),
    (K, 'vectors', 'the room for a text is counted without that it was resumed', '            served: true,\n            resumed: true,\n            pause_after: true,\n        };\n        let mut worst = 0;', '            served: true,\n            resumed: false,\n            pause_after: true,\n        };\n        let mut worst = 0;'),
    (K, 'vectors', 'the room for a text is counted for a part without a pause', '            served: true,\n            resumed: true,\n            pause_after: true,\n        };\n        let mut worst = 0;', '            served: true,\n            resumed: true,\n            pause_after: false,\n        };\n        let mut worst = 0;'),
    (K, 'vectors', 'the room for a text is counted without the mark before it', '        let taken = worst + TEXT_MARK.len();', '        let taken = worst;'),
    (K, 'vectors', 'a part holds as much as there is room for', '        let cut = chunks(text, room.min(PART_BYTES))?;', '        let cut = chunks(text, room)?;'),
    (K, 'vectors', 'a relay of nothing has no part', '        if relay.chunks.is_empty() {\n            relay.chunks.push(String::new());\n        }\n', ''),
    (K, 'vectors', 'a relay is made for a summary whatever was asked', '        relay.verbatim = verbatim;\n        let taken', '        let taken'),
    # ---- what the head of a part says
    (K, 'vectors', 'the part after the last is the end', '        if number == total {', '        if index == total {'),
    (K, 'vectors', 'a text of one part says it is the end of it', '            if total > 1 {', '            if total > 0 {'),
    (K, 'vectors', 'one more part is said as parts', '            let parts = if left == 1 { "part" } else { "parts" };', '            let parts = if left == 0 { "part" } else { "parts" };'),
    (K, 'vectors', 'a part before a pause says the next follows', '            head += if handed.pause_after {', '            head += if !handed.pause_after {'),
    (K, 'vectors', 'that a part was served is said of one that was resumed', '        if handed.served {\n            head += " Served', '        if handed.resumed {\n            head += " Served'),
    (K, 'vectors', 'that a part was resumed is said of one that was served', '        if handed.resumed {\n            head += " If you stopped', '        if handed.served {\n            head += " If you stopped'),
    (K, 'vectors', 'what was asked is said with every part', '        if first && !self.request.is_empty() {', '        if !self.request.is_empty() {'),
    (K, 'vectors', 'what was asked is said where nothing was', '        if first && !self.request.is_empty() {', '        if first {'),
    (K, 'vectors', 'the exact words are asked for where a summary is, and a summary where they are', '        head + if self.verbatim { VERBATIM } else { SUMMARY }', '        head + if self.verbatim { SUMMARY } else { VERBATIM }'),
    (K, 'vectors', 'what was asked is said with the second part', '        let head = self.head(index, self.total(), handed, index == 0);', '        let head = self.head(index, self.total(), handed, index == 1);'),
    (K, 'vectors', 'a part says there is one part more than there is', '        let head = self.head(index, self.total(), handed, index == 0);', '        let head = self.head(index, self.total() + 1, handed, index == 0);'),
    (K, 'vectors', 'the text of a part follows its head without the mark', '        format!("{head}{TEXT_MARK}{text}")', '        format!("{head}{text}")'),
    (K, 'vectors', 'the label of a relay is not in its head', '        let mut head = format!("{label} ({source}), part {number} of {total}");', '        let mut head = format!("({source}), part {number} of {total}");'),
    # ---- how far a relay came
    (K, 'vectors', 'a relay that was handed over whole says it is not complete', '        if self.next < total {', '        if self.next <= total {'),
    (K, 'vectors', 'how far a relay came is how far it ever came', '            let sent = self.next;', '            let sent = self.sent;'),
    (K, 'vectors', 'a relay says less of how its text ended', '        let from = self.text.char_indices().nth(signs.saturating_sub(TAIL));', '        let from = self.text.char_indices().nth(signs.saturating_sub(TAIL / 2));'),
    (K, 'vectors', 'how a text ended is said with the blanks around it', '        let tail = python::strip(from.map_or("", |(at, _)| &self.text[at..]));', '        let tail = from.map_or("", |(at, _)| &self.text[at..]);'),
    (K, 'vectors', 'every part of a relay is always still to be handed over', '        self.total().saturating_sub(self.next)', '        self.total()'),
    # ---- what of an answer is relayed
    (K, 'vectors', 'a delegation without an answer has nothing to relay', '    let raw = if result.is_empty() { error } else { result };', '    let raw = result;'),
    (K, 'vectors', 'what was marked to be spoken is joined without a blank', '    without_marks(&spoken.join(" "))', '    without_marks(&spoken.join(""))'),
    (K, 'vectors', 'the marks inside what is spoken are relayed', '    without_marks(&spoken.join(" "))', '    spoken.join(" ")'),
    (K, 'vectors', 'only the first piece that is marked is relayed', '        spoken.push(&inside[..ends]);\n        rest = &inside[ends + close.len()..];', '        spoken.push(&inside[..ends]);\n        rest = "";'),
    (K, 'vectors', 'a mark with nothing in it is taken out', '            Some(closes) if closes > 0 => {', '            Some(closes) => {'),
    (K, 'vectors', 'a sign that opens no mark is taken out', '                out.push_str(&rest[..=opens]);', '                out.push_str(&rest[..opens]);'),
    (K, 'vectors', 'what stands before a mark is taken out with it', '                out.push_str(&rest[..opens]);\n                rest = &after[closes + 1..];', '                rest = &after[closes + 1..];'),
    # ---- what was asked, as it is said
    (K, 'vectors', "the words before the user's own are part of what was asked", '        .map_or(request_text, |(_, own)| own);', '        .map_or(request_text, |_| request_text);'),
    (K, 'vectors', "the user's own words begin at the last mark for them", '        .split_once(marked)', '        .rsplit_once(marked)'),
    (K, 'vectors', "what was added for the agent's eyes is said", '    let own = python::strip(own.split(data).next().unwrap_or(""));', '    let own = python::strip(own);'),
    (K, 'vectors', 'what was asked is said with the blanks around it', '    let own = python::strip(own.split(data).next().unwrap_or(""));', '    let own = own.split(data).next().unwrap_or("");'),
    (K, 'vectors', 'what was asked is always said to go on', '    if begins.len() < own.len() {', '    if begins.len() <= own.len() {'),
    (K, 'vectors', 'what was asked is said whole, however long', '    let begins = python::first(own, EXCERPT_MOST);', '    let begins = own;'),
    # ---- the relay of an answer, and of a conversation
    (K, 'vectors', "an agent's session is over what it is called", '    let agent = named.or(session).unwrap_or("the agent");', '    let agent = session.or(named).unwrap_or("the agent");'),
    (K, 'vectors', 'an agent without a session has no name at all', '    let session = Some(row.session.as_str()).filter(|session| !session.is_empty());', '    let session = Some(row.session.as_str());'),
    (K, 'vectors', 'what was asked is not said with an answer', '        &request_excerpt(&row.request_text),\n        false,', '        "",\n        false,'),
    (K, 'vectors', "how a delegation ended is not in its relay's source", '            &format!("operation {id}, status {status}"),', '            &format!("operation {id}"),'),
    (K, 'vectors', 'an agent nobody named has no name in its conversation', '    let agent = if agent.is_empty() { "the agent" } else { agent };', '    let agent = if agent.is_empty() { "" } else { agent };'),
    (K, 'vectors', "the agent's words are read as its user's, and the user's as its own", '            let who = if said.by_the_agent { agent } else { "User" };', '            let who = if said.by_the_agent { "User" } else { agent };'),
    (K, 'vectors', 'a message that was cut is read as whole', '            let cut = if said.truncated {', '            let cut = if false && said.truncated {'),
    (K, 'vectors', 'the messages of a conversation follow each other without a line between', '        lines.join("\\n\\n")', '        lines.join("\\n")'),
    (K, 'vectors', 'a conversation without messages is read as nothing', '    let text = if lines.is_empty() {', '    let text = if false && lines.is_empty() {'),
    (K, 'vectors', 'the age of a conversation nobody knows is said as nothing', '    let age = if newest_age.is_empty() {', '    let age = if false && newest_age.is_empty() {'),
    (K, 'vectors', 'older messages are said to exist where there are none', '    if older {', '    if !older {'),
    (K, 'vectors', 'every conversation is known by one key', '            &format!("transcript:{agent}"),', '            "transcript:",'),
    (K, 'vectors', 'a conversation is never read word for word', '        "",\n        verbatim,\n    )', '        "",\n        false,\n    )'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
