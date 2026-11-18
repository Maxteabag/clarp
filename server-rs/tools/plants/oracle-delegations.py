#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The Oracle's delegations: the kernel's book, what an agent's actor says became of a turn, and the Host's part.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'oracle_delegation_vectors'],
    'actor': ['test', '-p', 'clarp-actor', '--test', 'actor', '--', 'what_became_of_the_turn'],
    'host': ['test', '--test', 'host', '--', 'the_oracle_hands'],
}
# NOT PLANTED, each with its reason:
#   that a delegation is never stamped before the moment it was last written (Host, `.max(row.updated_at)`): it
#     takes a turn that ends before its own admission returns, which no test can make.
#   that a delegation's turn is sent without speech (`synthesize_audio`): no engine speaks in this test, and that a
#     message which is not to be spoken is not is the send path's own rule, held by its tests.
#   the id the message of a delegation's turn is filed under (`client_msg_id`): it is the same text as its trace by
#     the kernel's rule, and a message without an id of its own is filed under its trace.
K = 'clarp-kernel/src/oracle/delegations.rs'
A = 'clarp-actor/src/lib.rs'
H = 'clarp-host/src/oracle/delegations.rs'
ACTIVE = 'row.status.active()'
PLANTS = [
    # ---- the id of a delegation
    (K, 'vectors', 'the blanks around an id are part of it', '    let id = python::strip(said);\n    let sign = ', '    let id = said;\n    let sign = '),
    (K, 'vectors', 'an id may be as long as it likes', 'if id.is_empty() || id.len() > ID_MOST || !id.bytes().all(sign) {', 'if id.is_empty() || !id.bytes().all(sign) {'),
    (K, 'vectors', 'an id may be of any signs', 'if id.is_empty() || id.len() > ID_MOST || !id.bytes().all(sign) {', 'if id.is_empty() || id.len() > ID_MOST {'),
    (K, 'vectors', 'nothing at all is an id', 'if id.is_empty() || id.len() > ID_MOST || !id.bytes().all(sign) {', 'if id.len() > ID_MOST || !id.bytes().all(sign) {'),
    (K, 'vectors', 'a dot is no sign of an id', "matches!(sign, b'.' | b'_' | b'-');", "matches!(sign, b'_' | b'-');"),
    (K, 'vectors', 'an underscore is no sign of an id', "matches!(sign, b'.' | b'_' | b'-');", "matches!(sign, b'.' | b'-');"),
    (K, 'vectors', 'a hyphen is no sign of an id', "matches!(sign, b'.' | b'_' | b'-');", "matches!(sign, b'.' | b'_');"),
    (K, 'vectors', 'an id of eighty signs is too long', 'if id.is_empty() || id.len() > ID_MOST || !id.bytes().all(sign) {', 'if id.is_empty() || id.len() >= ID_MOST || !id.bytes().all(sign) {'),
    # ---- a delegation is asked for
    (K, 'vectors', 'the blanks around a session are part of it', '(python::strip(asked.session), python::strip(asked.request));', '(asked.session, python::strip(asked.request));'),
    (K, 'vectors', 'the blanks around a request are part of it', '(python::strip(asked.session), python::strip(asked.request));', '(python::strip(asked.session), asked.request);'),
    (K, 'vectors', 'work for no session is taken as far as the agent', '        if session.is_empty() || request.is_empty() {\n            return Err(refused(400, "session and request are required"));', '        if request.is_empty() {\n            return Err(refused(400, "session and request are required"));'),
    (K, 'vectors', 'no work at all is handed on', '        if session.is_empty() || request.is_empty() {\n            return Err(refused(400, "session and request are required"));', '        if session.is_empty() {\n            return Err(refused(400, "session and request are required"));'),
    (K, 'vectors', 'an agent that is not there is a request that is wrong', 'refused(404, "unknown agent")', 'refused(400, "unknown agent")'),
    (K, 'vectors', 'a janitor is handed work', '        if agent.janitor {', '        if false && agent.janitor {'),
    (K, 'vectors', 'the blanks around who asks are part of them', '        let owner = python::strip(asked.owner);\n        if owner.is_empty() {', '        let owner = asked.owner;\n        if owner.is_empty() {'),
    (K, 'vectors', 'work is taken for nobody', '        if owner.is_empty() {\n', '        if false && owner.is_empty() {\n'),
    (K, 'vectors', "a delegation's turn is followed by the id alone", '        let trace_id = format!("oracle-{id}");', '        let trace_id = format!("{id}");'),
    (K, 'vectors', 'the same id for another agent and session is the same work', '            let same = there.agent_id == agent.agent_id\n                && there.session == session\n                && there.request_text == request', '            let same = there.request_text == request'),
    (K, 'vectors', 'the same id with another request is the same work', '                && there.request_text == request\n', ''),
    (K, 'vectors', 'the same id from another who asks is the same work', '                && there.request_text == request\n                && there.owner_principal == owner;', '                && there.request_text == request;'),
    (K, 'vectors', 'an id used for other work is a request that is wrong', '                let why = "delegation_id was already used for different work";\n                return Err(refused(409, why));', '                let why = "delegation_id was already used for different work";\n                return Err(refused(400, why));'),
    (K, 'vectors', 'a delegation that is over is run again', '            if !there.status.active() || has_message {', '            if has_message {'),
    (K, 'vectors', 'a delegation whose turn has begun is admitted again', '            if !there.status.active() || has_message {', '            if !there.status.active() {'),
    (K, 'vectors', 'a new delegation begins in the queue', '            status: Status::Accepted,\n            result_message_id: None,', '            status: Status::Queued,\n            result_message_id: None,'),
    (K, 'vectors', 'a new delegation was taken at no time', '            created_at: now_ms,\n            updated_at: now_ms,', '            created_at: 0,\n            updated_at: now_ms,'),
    (K, 'vectors', 'a new delegation is not given to write', '        self.changed.push(self.rows.len());\n        self.rows.push(Delegation {', '        self.rows.push(Delegation {'),
    # ---- its turn is admitted, begins, ends
    (K, 'vectors', 'a turn that waits is said to be under way, and one under way to wait', '        let to = if waits {\n            Status::Queued\n        } else {\n            Status::Accepted\n        };', '        let to = if waits {\n            Status::Accepted\n        } else {\n            Status::Queued\n        };'),
    (K, 'vectors', 'what is said of an admitted turn moves a delegation that is over', '        let holds = |row: &Delegation| row.delegation_id == id && row.status.active();\n        self.moved(holds, to, now_ms, |row| {', '        let holds = |row: &Delegation| row.delegation_id == id;\n        self.moved(holds, to, now_ms, |row| {'),
    (K, 'vectors', "the session of the agent's tool is not kept", '            row.backend_session_id = backend_session_id.to_string();\n', '            let _ = backend_session_id;\n'),
    (K, 'vectors', 'the blanks around the id of an admitted turn are part of it', '        let id = python::strip(delegation_id);\n        let to = if waits {', '        let id = delegation_id;\n        let to = if waits {'),
    (K, 'vectors', 'a turn that is under way begins again', 'row.trace_id == trace && row.status == Status::Queued;', 'row.trace_id == trace && row.status.active();'),
    (K, 'vectors', 'the blanks around the trace of a turn that begins are part of it', '        let trace = python::strip(trace_id);\n        let holds = |row: &Delegation| row.trace_id == trace && row.status == Status::Queued;', '        let trace = trace_id;\n        let holds = |row: &Delegation| row.trace_id == trace && row.status == Status::Queued;'),
    (K, 'vectors', 'an answer is kept with the blanks around it', '        let clean = python::strip(text);\n', '        let clean = text;\n'),
    (K, 'vectors', 'an answer in no row is a result', '        if message_id.is_empty() || clean.is_empty() {', '        if clean.is_empty() {'),
    (K, 'vectors', 'an answer that says nothing is a result', '        if message_id.is_empty() || clean.is_empty() {', '        if message_id.is_empty() {'),
    (K, 'vectors', 'the blanks around the trace of a turn that ended are part of it', '        let trace = python::strip(trace_id);\n        let holds = |row: &Delegation| row.trace_id == trace && row.status.active();\n        let moved = self.moved(holds, Status::Completed', '        let trace = trace_id;\n        let holds = |row: &Delegation| row.trace_id == trace && row.status.active();\n        let moved = self.moved(holds, Status::Completed'),
    (K, 'vectors', 'a second answer replaces the result', '        let holds = |row: &Delegation| row.trace_id == trace && row.status.active();\n        let moved = self.moved(holds, Status::Completed', '        let holds = |row: &Delegation| row.trace_id == trace;\n        let moved = self.moved(holds, Status::Completed'),
    (K, 'vectors', 'the row an answer is in is not kept', '            row.result_message_id = Some(message_id.to_string());\n', ''),
    (K, 'vectors', 'the answer is not kept', '            row.result_text = clean.to_string();\n', ''),
    (K, 'vectors', 'a turn that ends is said to have failed', '        let moved = self.moved(holds, Status::Completed, now_ms, |row| {', '        let moved = self.moved(holds, Status::Failed, now_ms, |row| {'),
    # ---- it fails
    (K, 'vectors', 'a failure without words has none', '        let said = if reason.is_empty() { NO_REASON } else { reason };', '        let said = reason;'),
    (K, 'vectors', 'the words of a failure are kept whole, however long', '        python::first(said, REASON_MOST).to_string()', '        said.to_string()'),
    (K, 'vectors', 'the blanks around the id of a delegation that failed are part of it', '        let id = python::strip(delegation_id);\n        let error = Self::reason(reason);', '        let id = delegation_id;\n        let error = Self::reason(reason);'),
    (K, 'vectors', 'a delegation that is over fails after all', '        let holds = |row: &Delegation| row.delegation_id == id && row.status.active();\n        self.moved(holds, Status::Failed', '        let holds = |row: &Delegation| row.delegation_id == id;\n        self.moved(holds, Status::Failed'),
    (K, 'vectors', 'why a delegation failed is not kept', '        self.moved(holds, Status::Failed, now_ms, |row| {\n            row.error.clone_from(&error);\n        });\n        self.get(id)', '        self.moved(holds, Status::Failed, now_ms, |_| {});\n        let _ = error;\n        self.get(id)'),
    (K, 'vectors', 'the blanks around the trace of a turn that failed are part of it', '        let trace = python::strip(trace_id);\n        let error = Self::reason(reason);', '        let trace = trace_id;\n        let error = Self::reason(reason);'),
    (K, 'vectors', 'a turn that failed fails a delegation that is over', '        let holds = |row: &Delegation| row.trace_id == trace && row.status.active();\n        let moved = self.moved(holds, Status::Failed', '        let holds = |row: &Delegation| row.trace_id == trace;\n        let moved = self.moved(holds, Status::Failed'),
    (K, 'vectors', 'why a turn failed is not kept', '        let moved = self.moved(holds, Status::Failed, now_ms, |row| {\n            row.error.clone_from(&error);\n        });', '        let moved = self.moved(holds, Status::Failed, now_ms, |_| {});\n        let _ = error;'),
    # ---- every row that moves
    (K, 'vectors', 'a delegation that moves on is not stamped', '                row.updated_at = now_ms;\n                change(row);', '                change(row);'),
    (K, 'vectors', 'a delegation that moves on is not given to write', '                change(row);\n                self.changed.push(at);', '                change(row);'),
    (K, 'vectors', 'what moved is counted one too few', '                count += 1;\n            }\n        }\n        count\n', '                count += 1;\n            }\n        }\n        count - usize::from(count > 0)\n'),
    # ---- looked at again
    (K, 'vectors', 'a delegation whose turn was never admitted is ended as lost', '                Fate::NotAdmitted | Fate::Live => continue,', '                Fate::Live => continue,\n                Fate::NotAdmitted => {}'),
    (K, 'vectors', 'a delegation whose turn runs is ended as lost', '                Fate::NotAdmitted | Fate::Live => continue,', '                Fate::NotAdmitted => continue,\n                Fate::Live => {}'),
    (K, 'vectors', 'the answer a lost turn left is not taken', '                    self.completed(&row.trace_id, &message_id, &text, at_ms);\n', '                    let _ = (&message_id, &text, at_ms);\n'),
    (K, 'vectors', 'a delegation whose turn is gone stays open', '                Fate::Lost => {}', '                Fate::Lost => continue,'),
    (K, 'vectors', 'a lost turn fails without the words for it', '            self.fail(&row.delegation_id, INTERRUPTED, now_ms);', '            self.fail(&row.delegation_id, NO_REASON, now_ms);'),
    # TAKEN OUT (18 Nov, 09:25): 'a delegation ended with its answer fails after all' was MISSED and changes nothing: a
    # delegation that ended is over, and failing it is asked of nothing. The guard went: what is still open after the
    # answer was taken failed.
    (K, 'vectors', 'what was ended is counted one too many', '            ended += 1;\n        }\n        ended\n', '            ended += 1;\n        }\n        ended + 1\n'),
    (K, 'vectors', 'delegations that are over are looked at again', '            .filter(|at| self.rows[*at].status.active())\n            .collect();\n        let mut ended = 0;', '            .filter(|at| self.rows[*at].delivered_at.is_none())\n            .collect();\n        let mut ended = 0;'),
    # ---- called off
    (K, 'vectors', "anybody's delegation is called off by its id", '        let Some(row) = found.filter(|row| row.owner_principal == owner) else {', '        let Some(row) = found.filter(|row| !row.owner_principal.is_empty() || owner.is_empty()) else {'),
    (K, 'vectors', 'a delegation that is not over is told as called off by its id', '        if row.status.active() {\n            let why = "nonterminal delegation requires agent cancellation";', '        if false && row.status.active() {\n            let why = "nonterminal delegation requires agent cancellation";'),
    (K, 'vectors', 'a delegation that is not over is a request that is wrong', '            let why = "nonterminal delegation requires agent cancellation";\n            return Err(refused(409, why));', '            let why = "nonterminal delegation requires agent cancellation";\n            return Err(refused(400, why));'),
    (K, 'vectors', 'the blanks around the session of a stop are part of it', '        let (session, owner) = (python::strip(session), python::strip(owner));', '        let (session, owner) = (session, python::strip(owner));'),
    (K, 'vectors', 'the blanks around who stops are part of them', '        let (session, owner) = (python::strip(session), python::strip(owner));', '        let (session, owner) = (python::strip(session), owner);'),
    (K, 'vectors', 'a stop of no session is asked of the book', '        if session.is_empty() || owner.is_empty() {\n            return Err(refused(400, "session and owner principal are required"));', '        if owner.is_empty() {\n            return Err(refused(400, "session and owner principal are required"));'),
    (K, 'vectors', 'a stop by nobody is asked of the book', '        if session.is_empty() || owner.is_empty() {\n            return Err(refused(400, "session and owner principal are required"));', '        if session.is_empty() {\n            return Err(refused(400, "session and owner principal are required"));'),
    (K, 'vectors', 'a stop calls off what was handed to other agents', '                row.session == session && row.owner_principal == owner && row.status.active()', '                row.owner_principal == owner && row.status.active()'),
    (K, 'vectors', 'a stop calls off what others handed on', '                row.session == session && row.owner_principal == owner && row.status.active()', '                row.session == session && row.status.active()'),
    (K, 'vectors', 'a stop is to call off what is over', '                row.session == session && row.owner_principal == owner && row.status.active()', '                row.session == session && row.owner_principal == owner'),
    (K, 'vectors', 'what a stop calls off is told in the order it was taken', '        found.sort_by_key(|row| std::cmp::Reverse(row.updated_at));\n', ''),
    (K, 'vectors', 'everything a stop calls off is said to have waited', '            waited: row.status == Status::Queued,', '            waited: true,'),
    (K, 'vectors', 'nothing a stop calls off is said to have waited', '            waited: row.status == Status::Queued,', '            waited: false,'),
    (K, 'vectors', 'a delegation that failed while its agent was stopped stays failed', '                && (row.status.active() || row.status == Status::Failed)', '                && row.status.active()'),
    (K, 'vectors', 'a delegation that ended while its agent was stopped is called off', '                && (row.status.active() || row.status == Status::Failed)', '                && row.status != Status::Cancelled'),
    (K, 'vectors', 'what a stop was to call off is all told as called off', '            .filter(|row| row.status == Status::Cancelled)\n            .cloned()', '            .cloned()'),
    # ---- shown and acknowledged
    (K, 'vectors', 'a result that was delivered is shown again', '                row.delivered_at.is_none()\n                    && row.owner_principal == owner', '                row.owner_principal == owner'),
    (K, 'vectors', "a phone is shown what others handed on", '                row.delivered_at.is_none()\n                    && row.owner_principal == owner\n                    && !row.status.active()', '                row.delivered_at.is_none()\n                    && !row.status.active()'),
    (K, 'vectors', 'a phone is shown what is not over', '                    && row.owner_principal == owner\n                    && !row.status.active()\n                    && !row.of_a_live_call()', '                    && row.owner_principal == owner\n                    && !row.of_a_live_call()'),
    (K, 'vectors', "a phone is shown what a live call asked", '                    && !row.status.active()\n                    && !row.of_a_live_call()', '                    && !row.status.active()'),
    (K, 'vectors', 'what is to be told is told in the order of its ids', '            let by_time = one.updated_at.cmp(&other.updated_at);', '            let by_time = std::cmp::Ordering::Equal;'),
    (K, 'vectors', 'results of one moment are told the last id first', '            let by_time = one.updated_at.cmp(&other.updated_at);\n            by_time.then_with(|| one.delegation_id.cmp(&other.delegation_id))', '            let by_time = one.updated_at.cmp(&other.updated_at);\n            by_time.then_with(|| other.delegation_id.cmp(&one.delegation_id))'),
    (K, 'vectors', 'nothing is shown where none is asked for', '    usize::try_from(limit.clamp(1, SHOWN_MOST)).unwrap_or(1)', '    usize::try_from(limit.clamp(0, SHOWN_MOST)).unwrap_or(1)'),
    (K, 'vectors', 'as many are shown as are asked for', '    usize::try_from(limit.clamp(1, SHOWN_MOST)).unwrap_or(1)', '    usize::try_from(limit.max(1)).unwrap_or(1)'),
    (K, 'vectors', 'the blanks around the id of a result that is had are part of it', '        let id = python::strip(delegation_id);\n        let found = self.rows.iter().position(|row| {', '        let id = delegation_id;\n        let found = self.rows.iter().position(|row| {'),
    (K, 'vectors', "anybody says they have another's result", '            row.delegation_id == id && row.owner_principal == owner && !row.status.active()', '            row.delegation_id == id && !row.status.active()'),
    (K, 'vectors', 'a result is had before there is one', '            row.delegation_id == id && row.owner_principal == owner && !row.status.active()', '            row.delegation_id == id && row.owner_principal == owner'),
    (K, 'vectors', 'a result that is had again was had now', '        row.delivered_at = row.delivered_at.or(Some(now_ms));', '        row.delivered_at = Some(now_ms);'),
    (K, 'vectors', 'a result that is had is not stamped', '        row.updated_at = now_ms;\n        self.changed.push(at);\n        true', '        self.changed.push(at);\n        true'),
    (K, 'vectors', 'a result that is had is not given to write', '        row.updated_at = now_ms;\n        self.changed.push(at);\n        true', '        row.updated_at = now_ms;\n        true'),
    (K, 'vectors', "everybody's delegations are listed for one who asks", '            .filter(|row| row.owner_principal == owner)\n            .collect();\n        found.sort_by(|one, other| {\n            let by_time = other.updated_at', '            .filter(|row| !row.owner_principal.is_empty() || owner.is_empty())\n            .collect();\n        found.sort_by(|one, other| {\n            let by_time = other.updated_at'),
    (K, 'vectors', 'all of it is listed the oldest first', '            let by_time = other.updated_at.cmp(&one.updated_at);', '            let by_time = one.updated_at.cmp(&other.updated_at);'),
    (K, 'vectors', 'all of one moment is listed the first id first', '            by_time.then_with(|| other.delegation_id.cmp(&one.delegation_id))', '            by_time.then_with(|| one.delegation_id.cmp(&other.delegation_id))'),
    # ---- what an agent owes
    (K, 'vectors', "an agent owes what another's turn is for", '        let found = open.find(|row| row.agent_id == agent_id && row.trace_id == trace_id);', '        let found = open.find(|row| row.trace_id == trace_id);'),
    (K, 'vectors', 'an agent owes whatever it was handed', '        let found = open.find(|row| row.agent_id == agent_id && row.trace_id == trace_id);', '        let found = open.find(|row| row.agent_id == agent_id);'),
    (K, 'vectors', 'an agent owes what is over', '        let mut open = self.rows.iter().filter(|row| row.status.active());\n        let found = open.find(', '        let mut open = self.rows.iter().filter(|row| !row.request_text.is_empty());\n        let found = open.find('),
    (K, 'vectors', "what is for the agent's eyes is part of what it owes", '            let asked = row.request_text.split(REFERENCE_DATA).next().unwrap_or("");', '            let asked = row.request_text.as_str();'),
    (K, 'vectors', 'what an agent owes is told whole, however long', '                "request": python::first(asked, REQUEST_MOST),', '                "request": asked,'),
    # ---- what is held back for a live call
    (K, 'vectors', 'an id of one sign less is a live call\'s', '            signs.len() == LIVE_ASK_SIGNS', '            signs.len() + 1 >= LIVE_ASK_SIGNS'),
    (K, 'vectors', 'an id in large letters is a live call\'s', "matches!(sign, b'0'..=b'9' | b'a'..=b'f')", "matches!(sign, b'0'..=b'9' | b'a'..=b'f' | b'A'..=b'F')"),
    (K, 'vectors', 'whatever comes under such an id is a live call\'s', '        by_id && begins_as_a_live_ask(&self.request_text)', '        by_id'),
    (K, 'vectors', 'whatever asks as a live call does is held back', '        by_id && begins_as_a_live_ask(&self.request_text)', '        begins_as_a_live_ask(&self.request_text)'),
    (K, 'vectors', 'letters of every alphabet are the same large and small', '    one == other || (one.is_ascii() && one.eq_ignore_ascii_case(&other))', '    one.to_lowercase().eq(other.to_lowercase())'),
    (K, 'vectors', 'a large letter is another than its small one', '    one == other || (one.is_ascii() && one.eq_ignore_ascii_case(&other))', '    one == other'),
    (K, 'vectors', 'a request that begins otherwise is a live call\'s too', '    if !begins {\n        return false;\n    }\n', ''),
    (K, 'vectors', 'a request that names no call is a live call\'s', '            .all(|(sign, wanted)| alike(*sign, *wanted))\n    })', '            .all(|(sign, wanted)| alike(*sign, *wanted) || sign != wanted)\n    })'),
    # ---- the session of the agent's tool, once it is known
    (K, 'vectors', "the session of an agent's tool replaces the one a delegation was taken with", '            row.delegation_id == delegation_id && row.backend_session_id.is_empty()', '            row.delegation_id == delegation_id'),
    (K, 'vectors', "a tool's session that is nothing is kept as one", '        let Some(at) = found.filter(|_| !session.is_empty()) else {', '        let Some(at) = found else {'),
    (K, 'vectors', "a tool's session that is kept later is not given to write", '        self.rows[at].backend_session_id = session.to_string();\n        self.changed.push(at);\n        true', '        self.rows[at].backend_session_id = session.to_string();\n        true'),
    (K, 'vectors', "a tool's session that is known later is not kept", '        self.rows[at].backend_session_id = session.to_string();\n        self.changed.push(at);\n        true', '        self.changed.push(at);\n        true'),
    (H, 'host', "a delegation taken before the tool had a session never has it", '        book.tool_session(&row.delegation_id, &tool_session);\n', ''),
    (H, 'host', "the session of an agent's tool is not asked for when the book is read", '    Ok((fate, tool_session.unwrap_or_default()))', '    Ok((fate, tool_session.map(|_| String::new()).unwrap_or_default()))'),
    # ---- the book as it was kept
    (K, 'vectors', 'a book is read in whatever order its rows come', '        rows.sort_by(|one, other| {\n            let by_time = one.created_at.cmp(&other.created_at);\n            by_time.then_with(|| one.delegation_id.cmp(&other.delegation_id))\n        });\n', ''),
    (K, 'vectors', 'what changed is given to write again and again', '        let mut places = std::mem::take(&mut self.changed);', '        let mut places = self.changed.clone();'),
    (K, 'vectors', 'a delegation is told without whether it was delivered', '"delivered_at": self.delivered_at, "delivered": self.delivered_at.is_some(),', '"delivered_at": self.delivered_at, "delivered": false,'),
    # ---- what an agent's actor says became of a turn
    (A, 'actor', 'a message nobody sent has a turn that is gone', '            return Ok(TurnFate::Unknown);', '            return Ok(TurnFate::Gone);'),
    (A, 'actor', 'the words of a failure are looked for in the row that closed the turn', '                format!("marker-{message_id}")\n            };\n            let left = self.recorder.log().find(&left)?;', '                end.closes.clone()\n            };\n            let left = self.recorder.log().find(&left)?;'),
    (A, 'actor', 'an answer is looked for under its message', '            let left = if end.status == "completed" {', '            let left = if end.status != "completed" {'),
    (A, 'actor', 'what an answer says is not told', '                Some(Ok(Some(Entry::Reply(reply)))) => reply.text,\n                Some(Ok(Some(Entry::Notice(notice)))) => notice.text,\n                _ => String::new(),\n            };\n            return Ok(TurnFate::Ended {', '                Some(Ok(Some(Entry::Reply(_)))) => String::new(),\n                Some(Ok(Some(Entry::Notice(notice)))) => notice.text,\n                _ => String::new(),\n            };\n            return Ok(TurnFate::Ended {'),
    (A, 'actor', 'the words of a failure are not told', '                Some(Ok(Some(Entry::Notice(notice)))) => notice.text,\n                _ => String::new(),\n            };\n            return Ok(TurnFate::Ended {', '                Some(Ok(Some(Entry::Notice(_)))) => String::new(),\n                _ => String::new(),\n            };\n            return Ok(TurnFate::Ended {'),
    (A, 'actor', 'a turn that ended is told without the moment', '                ended_at_ms: end.ended_at_ms,\n            });\n        }\n        let agent = self.recorder.agent();', '                ended_at_ms: 0,\n            });\n        }\n        let agent = self.recorder.agent();'),
    (A, 'actor', 'a turn that ended is told without how', '                status: end.status,\n                closes: end.closes,\n                text,', '                status: String::new(),\n                closes: end.closes,\n                text,'),
    (A, 'actor', 'a turn that ended is told without the row that closed it', '                status: end.status,\n                closes: end.closes,\n                text,', '                status: end.status,\n                closes: String::new(),\n                text,'),
    (A, 'actor', 'while any turn runs every message has a turn that runs', '        if agent.live().is_some_and(|live| live.id().0 == turn) {\n            return Ok(TurnFate::Runs);', '        if agent.live().is_some() {\n            return Ok(TurnFate::Runs);'),
    (A, 'actor', 'while anything waits every message waits', '        let waits = agent.waiting().any(|queued| queued.id.0 == turn);', '        let waits = agent.waiting().next().is_some();'),
    (A, 'actor', 'nothing waits: what does not run is gone', '        let waits = agent.waiting().any(|queued| queued.id.0 == turn);', '        let waits = agent.waiting().any(|queued| queued.id.0 == turn + 1_000_000);'),
    (A, 'actor', 'a turn that runs is said to wait', '            return Ok(TurnFate::Runs);\n        }\n        let waits', '            return Ok(TurnFate::Waits);\n        }\n        let waits'),
    # ---- the Host's part
    (H, 'host', 'a turn that has begun is not said to be under way', '                book.started(&row.trace_id, now);\n', ''),
    (H, 'host', 'a turn that waits is a turn that is gone', '            TurnFate::Unknown | TurnFate::Waits => Fate::NotAdmitted,', '            TurnFate::Unknown | TurnFate::Waits => Fate::Lost,'),
    (H, 'host', 'a turn that runs is a turn that is gone', '                book.started(&row.trace_id, now);\n                Fate::Live', '                book.started(&row.trace_id, now);\n                Fate::Lost'),
    (H, 'host', 'a turn that is gone leaves its delegation open', '            TurnFate::Gone => Fate::Lost,', '            TurnFate::Gone => Fate::NotAdmitted,'),
    (H, 'host', 'a turn that failed left an answer', '                if status != "completed" {', '                if status == "nothing a turn ends as" {'),
    (H, 'host', 'a failed delegation is kept with the sentence a user is told', '                    let why = why.map_or(text.as_str(), |(_, tools)| tools);', '                    let why = why.map_or(text.as_str(), |(sentence, _)| sentence);'),
    (H, 'host', 'a turn that ended with nothing said is a turn that was lost', '                    book.failed_for_trace(&row.trace_id, SAID_NOTHING, at_ms);\n', '                    let _ = SAID_NOTHING;\n'),
    (H, 'host', 'a result is stamped when the book is read', '                let at_ms = ended_at_ms.max(row.updated_at);', '                let at_ms = now.max(ended_at_ms);'),
    (H, 'host', 'the answer is told as said in no row', '                    message_id: closes,\n                    text,\n                    at_ms,', '                    message_id: String::new(),\n                    text,\n                    at_ms,'),
    (H, 'host', 'an agent that is removed still has its turns', '        return Ok((TurnFate::Gone, String::new()));', '        return Ok((TurnFate::Unknown, String::new()));'),
    (H, 'host', 'the message of a turn is looked for under the id it was sent with', '    let (message_id, _) = Message::filed(&row.client_msg_id);\n    let fate = member.agent.fate(message_id).await?;', '    let message_id = row.client_msg_id.clone();\n    let fate = member.agent.fate(message_id).await?;'),
    (H, 'host', 'the book is not read from what was kept', '        *held = Some(Book::kept(rows.collect()));', '        *held = Some(Book::kept(rows.take(0).collect()));'),
    (H, 'host', 'what changed in the book is not written', '        app.fleet.keep(KEPT, row.delegation_id, Some(kept)).await?;', '        let _ = (row.delegation_id, kept);'),
    (H, 'host', 'whatever is handed on is the administrator\'s', '    let owner = caller(&principal)?;\n    let (id, session, request) = (', '    let owner = caller(&Principal::Administrator)?;\n    let (id, session, request) = ('),
    (H, 'host', 'everybody is shown the administrator\'s delegations', '    let owner = caller(&principal)?;\n    let limit = asked', '    let owner = caller(&Principal::Administrator)?;\n    let limit = asked'),
    (H, 'host', 'everybody says they have the administrator\'s results', '    let owner = caller(&principal)?;\n    let id = text(&said, "delegation_id");', '    let owner = caller(&Principal::Administrator)?;\n    let id = text(&said, "delegation_id");'),
    (H, 'host', 'everybody calls off the administrator\'s delegations', '    let owner = caller(&principal)?;\n    let session = text(&said, "session");', '    let owner = caller(&Principal::Administrator)?;\n    let session = text(&said, "session");'),
    (H, 'host', 'a delegation asked for again is admitted again though its turn is there', '            member.agent.fate(message_id).await? != TurnFate::Unknown', '            member.agent.fate(message_id).await? == TurnFate::Unknown && false'),
    (H, 'host', 'a janitor is found as no agent', '    let member = app.anyone(python::strip(session));', '    let member = app.member(python::strip(session));'),
    (H, 'host', 'the blanks around a session name no agent', '    let member = app.anyone(python::strip(session));', '    let member = app.anyone(session);'),
    (H, 'host', 'a janitor is handed work as an agent', '        janitor: member.record.is_janitor,', '        janitor: false,'),
    (H, 'host', "a delegation's turn is sent as its user's", '        origin: Some("oracle".to_string()),', '        origin: None,'),
    (H, 'host', "a delegation's turn is said to the agent and not left for later", '        queue_if_busy: Some(true),', '        queue_if_busy: None,'),
    (H, 'host', "a delegation's turn is followed by a trace of the Host's own", '        trace_id: Some(admit.trace_id.clone()),', '        trace_id: None,'),
    (H, 'host', 'a turn that cannot be admitted leaves its delegation open', '            book.fail(&admit.delegation_id, refused.message(), app.clock.now_ms());\n', ''),
    (H, 'host', 'a turn that cannot be admitted is refused as the Host\'s own failure', '            return Err(ApiError::new(refused.status(), refused.message()));', '            return Err(ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, refused.message()));'),
    (H, 'host', 'a turn that waits is said to be under way', '    let waits = sent.queued == Some(true);', '    let waits = false;'),
    (H, 'host', "the session of the agent's tool is another's", '        tool_session.as_deref().unwrap_or(""),', '        tool_session.as_deref().map_or("", |_| "another"),'),
    (H, 'host', 'only what is still to be told is ever listed', '        let rows = if all {\n            book.recent(&owner, limit)', '        let rows = if false {\n            book.recent(&owner, limit)'),
    (H, 'host', 'everything is listed whatever is asked', '        let rows = if all {\n            book.recent(&owner, limit)', '        let rows = if true {\n            book.recent(&owner, limit)'),
    (H, 'host', 'a number of rows that is asked for is not read', '    let limit = limit.unwrap_or(SHOWN_USUALLY);', '    let limit = limit.map_or(SHOWN_USUALLY, |_| SHOWN_USUALLY);'),
    (H, 'host', 'a word for a number shows one row', '    let limit = limit.unwrap_or(SHOWN_USUALLY);', '    let limit = limit.unwrap_or(1);'),
    (H, 'host', 'only a one asks for everything', '    let all = matches!(all.as_deref(), Some("1" | "true" | "yes"));', '    let all = matches!(all.as_deref(), Some("1"));'),
    (H, 'host', 'what a request changed in the book is not written', '    let answered = request(book).await;\n    written(app, book).await?;\n    answered', '    let answered = request(book).await;\n    answered'),
    (H, 'host', 'a result nobody has is a request that is wrong', '            let why = "terminal delegation not found";\n            return Err(ApiError::new(StatusCode::NOT_FOUND, why));', '            let why = "terminal delegation not found";\n            return Err(ApiError::new(StatusCode::BAD_REQUEST, why));'),
    (H, 'host', 'a delegation nobody has is a request that is wrong', '        let none = || ApiError::new(StatusCode::NOT_FOUND, "delegation not found");', '        let none = || ApiError::new(StatusCode::BAD_REQUEST, "delegation not found");'),
    (H, 'host', 'a stop for the Oracle stops nobody', '        let stopped = member.agent.stop("user".to_string()).await;', '        let stopped = member.agent.queue().await;'),
    (H, 'host', 'what waited stays in the queue of an agent that was stopped', '                let withdrawn = member.agent.withdraw(waits.turn, "user".to_string());\n                withdrawn.await?;', '                let _ = (member, waits.turn);'),
    # GONE WITH with_book (18 Nov, after the first run): each handler's own look and its own write (five rules that the
    # first run did not catch: the next request wrote and looked anyway). One look before and one write after every
    # request are planted instead. The second look after a stop is gone with its code (not observable: what was open
    # before the stop is called off whatever the stop made of its turn, as on the Python Host). NOT PLANTED: writing
    # only when a request answered well (what a failing request changed stays to be written and goes with the next).
    (H, 'host', 'the book is not looked at again before a request', '    looked_again(app, book).await?;\n    let answered = request(book).await;', '    let answered = request(book).await;'),
    (H, 'host', 'a stop takes everything out of the queue', '        for waits in waiting.iter().filter(|waits| of_these(&waits.client_id)) {', '        for waits in waiting.iter().filter(|waits| of_these(&waits.client_id) || true) {'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
