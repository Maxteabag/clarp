#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The memory of the Oracle's threads: which thread a call goes on in and that one connection has it, what is kept of
the talk, the actions that were begun, the work of a thread, what its voice was told of, and the excerpt a call
begins with. The kernel's rules.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'oracle_memory_vectors'],
}
# NOT PLANTED, each with its reason:
#   the order the threads are read back in from what was kept (`Memory::kept`, by when they were made): a thread is
#     chosen by its place only where no connection has any thread of its owner and contact, which a Host never
#     leaves so (the stories make it so by hand); and no thread of the stories is made later under an earlier name.
#   the moment a new thread is made with as its last writing (`updated_at` where it is made): the thread a call
#     takes is written at that moment, whatever it was made with.
#   that what was kept is read with the thread's own number over whatever number it holds (`state.insert` and not
#     `or_insert`): the two are the same number for every talk the memory can keep.
#   that a row which cannot be read is none (`.ok()` in `Memory::kept`): the rows are the memory's own.
#   a result that is no JSON, where an action's result is read again (`unwrap_or`): the memory wrote it.
#   that the excerpt takes a row only where it is a record (`filter_map(.. as_object ..)`): the talk that is kept is
#     the conversation's own, and the work is the book's.
K = 'clarp-kernel/src/oracle/threads.rs'
OF_THESE = 'thread.owner_principal == opening.owner && thread.contact == opening.contact'
BEST = '(!thread.active_connection.is_empty(), thread.updated_at, *at)'
AT = ('            row.thread_id == opened.thread_id\n'
      '                && row.revision == revision\n'
      '                && row.action_index == index\n')
PLANTS = [
    # ---- the names of an action and of its work
    (K, 'vectors', 'an action is named by its thread and number alone', '    let place = format!("{thread_id}:{revision}:{index}");', '    let place = format!("{thread_id}:{revision}");'),
    (K, 'vectors', 'the name of an action is a sign longer', '    format!("oracle-{}", python::first(&digest(&place), 40))', '    format!("oracle-{}", python::first(&digest(&place), 41))'),
    (K, 'vectors', 'the name of an action begins otherwise', '    format!("oracle-{}", python::first(&digest(&place), 40))', '    format!("action-{}", python::first(&digest(&place), 40))'),
    (K, 'vectors', 'work that takes the place of other work is named as any work', '    let mark = if replacement { ":follow:" } else { ":" };', '    let mark = ":";'),
    (K, 'vectors', 'the name of work is a sign shorter', '    format!("rtc-{}", python::first(&digest(&named), 40))', '    format!("rtc-{}", python::first(&digest(&named), 39))'),
    (K, 'vectors', 'work is named by its action alone', '    let named = format!("{owner}{mark}{call_id}");', '    let named = format!("{mark}{call_id}");'),
    # ---- the name a thread is asked for by
    (K, 'vectors', 'a name may be as long as it likes', '    text.chars().count() <= NAME_SIGNS && text.chars().all(signs)', '    text.chars().all(signs)'),
    (K, 'vectors', 'a name of 160 signs is too long', '    text.chars().count() <= NAME_SIGNS && text.chars().all(signs)', '    text.chars().count() < NAME_SIGNS && text.chars().all(signs)'),
    (K, 'vectors', 'a name may be of any signs', '    text.chars().count() <= NAME_SIGNS && text.chars().all(signs)', '    text.chars().count() <= NAME_SIGNS'),
    (K, 'vectors', 'a dot is no sign of a name', '"._:-".contains(sign)', '"_:-".contains(sign)'),
    (K, 'vectors', 'an underscore is no sign of a name', '"._:-".contains(sign)', '".:-".contains(sign)'),
    (K, 'vectors', 'a colon is no sign of a name', '"._:-".contains(sign)', '"._-".contains(sign)'),
    (K, 'vectors', 'a hyphen is no sign of a name', '"._:-".contains(sign)', '"._:".contains(sign)'),
    (K, 'vectors', 'a letter of another alphabet is a sign of a name', 'sign.is_ascii_alphanumeric() || "._:-".contains(sign)', 'sign.is_alphanumeric() || "._:-".contains(sign)'),
    # ---- a call begins
    (K, 'vectors', 'a call of nobody opens a thread', '        if opening.owner.is_empty() {\n            return Err(NO_OWNER);\n        }\n', ''),
    (K, 'vectors', 'a thread is of its owner whoever the contact', OF_THESE, 'thread.owner_principal == opening.owner'),
    (K, 'vectors', 'a thread is of its contact whoever the owner', OF_THESE, 'thread.contact == opening.contact'),
    (K, 'vectors', 'a thread that is asked for by no name is looked for', '        let named = opening.thread_id.filter(|thread_id| !thread_id.is_empty());', '        let named = opening.thread_id;'),
    (K, 'vectors', 'a thread may be asked for by any text', '                if !name(thread_id) {\n                    return Err(NO_NAME);\n                }\n', ''),
    (K, 'vectors', 'a thread of another owner may be asked for', '                    thread.thread_id == thread_id && thread.owner_principal == opening.owner', '                    thread.thread_id == thread_id'),
    (K, 'vectors', 'a thread of another contact may be asked for', '                if thread.contact != opening.contact {\n                    return Err(OTHER_CONTACT);\n                }\n', ''),
    (K, 'vectors', 'a thread asked for by its name goes on though a new one was wanted', '                (!opening.fresh).then(|| thread_id.to_string())', '                Some(thread_id.to_string())'),
    (K, 'vectors', 'a call that wants a new thread goes on in the old', '            None if opening.fresh => None,\n', ''),
    (K, 'vectors', 'the thread a connection has is not the one that goes on', BEST, '(thread.updated_at, *at)'),
    (K, 'vectors', 'the thread that was last written is not the one that goes on', BEST, '(!thread.active_connection.is_empty(), *at)'),
    (K, 'vectors', 'of threads written at once the one made first goes on', BEST, '(!thread.active_connection.is_empty(), thread.updated_at, std::cmp::Reverse(*at))'),
    (K, 'vectors', 'the other threads keep their connections', '            if !thread.active_connection.is_empty() {\n                thread.active_connection.clear();', '            if false {\n                thread.active_connection.clear();'),
    (K, 'vectors', 'a thread whose connection was taken is not written', '                thread.active_connection.clear();\n                self.changed.insert((THREADS, thread.thread_id.clone()));', '                thread.active_connection.clear();'),
    (K, 'vectors', 'a new thread begins at another number', '                    revision: 0,\n                    state: json!({}),', '                    revision: 1,\n                    state: json!({}),'),
    (K, 'vectors', 'a new thread has something kept already', '                    state: json!({}),', '                    state: json!({ "fragments": [] }),'),
    (K, 'vectors', 'a new thread was made at no time', '                    state: json!({}),\n                    created_at: now_ms,', '                    state: json!({}),\n                    created_at: 0,'),
    (K, 'vectors', 'a new thread is of nobody', '                    owner_principal: opening.owner.to_string(),\n                    contact: opening.contact.to_string(),', '                    owner_principal: String::new(),\n                    contact: opening.contact.to_string(),'),
    (K, 'vectors', 'a new thread is of no contact', '                    contact: opening.contact.to_string(),\n                    active_connection: String::new(),', '                    contact: String::new(),\n                    active_connection: String::new(),'),
    (K, 'vectors', "the thread of a call is not its connection's", '            thread.active_connection = opening.connection.to_string();\n', ''),
    (K, 'vectors', 'a thread that goes on is not written at that moment', '            thread.active_connection = opening.connection.to_string();\n            thread.updated_at = now_ms;\n', '            thread.active_connection = opening.connection.to_string();\n'),
    (K, 'vectors', 'a thread that is opened is not written', '        self.changed.insert((THREADS, thread_id.clone()));\n        Ok(thread_id)', '        Ok(thread_id)'),
    # ---- whose a thread is
    (K, 'vectors', "a thread is its connection's whoever owns it", '            thread.thread_id == opened.thread_id && thread.owner_principal == opened.owner\n        };\n        let thread = self.threads.iter().find(own).ok_or(NOT_ITS_OWN)?;', '            thread.thread_id == opened.thread_id\n        };\n        let thread = self.threads.iter().find(own).ok_or(NOT_ITS_OWN)?;'),
    (K, 'vectors', 'a connection that lost its thread still has it', '        if active && thread.active_connection != opened.connection {', '        if false && thread.active_connection != opened.connection {'),
    (K, 'vectors', 'a thread is read only by the connection that has it', '        if active && thread.active_connection != opened.connection {', '        if thread.active_connection != opened.connection {'),
    (K, 'vectors', 'a connection is asked whether it has a thread of its owner alone', '        self.owned(opened, true).is_ok()', '        self.owned(opened, false).is_ok()'),
    # ---- what is kept of the talk
    (K, 'vectors', 'what was kept is read without its number', '        state.insert("revision".to_string(), json!(thread.revision));\n', ''),
    (K, 'vectors', 'the talk may be kept however long', '        if python::canonical(state).len() > STATE_BYTES {\n            return Err(TOO_MUCH);\n        }\n', ''),
    (K, 'vectors', 'a talk of exactly the bound is too long', '        if python::canonical(state).len() > STATE_BYTES {', '        if python::canonical(state).len() >= STATE_BYTES {'),
    (K, 'vectors', 'the talk is measured in signs', '        if python::canonical(state).len() > STATE_BYTES {', '        if python::canonical(state).chars().count() > STATE_BYTES {'),
    (K, 'vectors', 'the talk is kept by a connection that lost its thread', '                && thread.owner_principal == opened.owner\n                && thread.active_connection == opened.connection\n        };', '                && thread.owner_principal == opened.owner\n        };'),
    (K, 'vectors', 'the talk is kept by a connection whoever owns the thread', '            thread.thread_id == opened.thread_id\n                && thread.owner_principal == opened.owner\n                && thread.active_connection == opened.connection', '            thread.thread_id == opened.thread_id\n                && thread.active_connection == opened.connection'),
    (K, 'vectors', 'the number of the talk is not kept', '        thread.revision = state["revision"].as_i64().unwrap_or(0);\n', ''),
    (K, 'vectors', 'a talk that is kept is not kept at that moment', '        thread.updated_at = now_ms;\n        self.changed.insert((THREADS, opened.thread_id.to_string()));', '        self.changed.insert((THREADS, opened.thread_id.to_string()));'),
    (K, 'vectors', 'a talk that is kept is not written', '        self.changed.insert((THREADS, opened.thread_id.to_string()));\n        Ok(())', '        Ok(())'),
    # ---- an action is begun
    (K, 'vectors', 'an action is begun by a connection that lost its thread', '        self.owned(opened, true)?;\n        let arguments_json = python::canonical(arguments);', '        self.owned(opened, false)?;\n        let arguments_json = python::canonical(arguments);'),
    (K, 'vectors', 'what an action is handed is written in the order it came', '        let arguments_json = python::canonical(arguments);', '        let arguments_json = python::compact(arguments);'),
    (K, 'vectors', 'an action is found by its number alone', AT + '        };\n        let place = match self.begun.iter().position(at) {', '            row.thread_id == opened.thread_id && row.revision == revision\n        };\n        let place = match self.begun.iter().position(at) {'),
    (K, 'vectors', 'an action is found in any thread', '        let at = |row: &Begun| {\n            row.thread_id == opened.thread_id\n                && row.revision == revision', '        let at = |row: &Begun| {\n            row.revision == revision'),
    (K, 'vectors', 'an action that is begun is ended already', '                    result_json: None,\n                    completed: false,', '                    result_json: None,\n                    completed: true,'),
    (K, 'vectors', 'an action that is begun was begun at no time', '                    completed: false,\n                    created_at: now_ms,', '                    completed: false,\n                    created_at: 0,'),
    (K, 'vectors', 'an action that is begun is not written', '                let made = self.begun.len() - 1;\n                self.changed.insert((BEGUN, self.begun[made].name()));', '                let made = self.begun.len() - 1;'),
    (K, 'vectors', 'another tool at the place of an action is the same action', '        if row.tool != tool || row.arguments_json != arguments_json {', '        if row.arguments_json != arguments_json {'),
    (K, 'vectors', 'the same tool at the place of an action is the same action whatever it is handed', '        if row.tool != tool || row.arguments_json != arguments_json {', '        if row.tool != tool {'),
    # ---- an action is ended
    (K, 'vectors', 'an action is ended by a connection that lost its thread', '        self.owned(opened, true)?;\n        let at = |row: &&mut Begun| {', '        self.owned(opened, false)?;\n        let at = |row: &&mut Begun| {'),
    (K, 'vectors', 'an action is ended with its last answer', '        if let Some(row) = self.begun.iter_mut().find(at).filter(|row| !row.completed) {', '        if let Some(row) = self.begun.iter_mut().find(at) {'),
    (K, 'vectors', 'an action that is ended is not ended', '            row.completed = true;\n', ''),
    (K, 'vectors', 'what an action was answered is not kept', '            row.result_json = Some(python::canonical(result));\n', ''),
    (K, 'vectors', 'an action is ended at no new moment', '            row.updated_at = now_ms;\n            self.changed.insert((BEGUN, row.name()));', '            self.changed.insert((BEGUN, row.name()));'),
    (K, 'vectors', 'an action that is ended is not written', '            row.updated_at = now_ms;\n            self.changed.insert((BEGUN, row.name()));', '            row.updated_at = now_ms;'),
    (K, 'vectors', 'the work an answer names is not the work of the thread', '            Some(operation_id) => self.link(opened, operation_id, owner_of, now_ms),', '            Some(_) => Ok(()),'),
    (K, 'vectors', 'work of no name is the work of a thread', '        match result["operation_id"].as_str().filter(|id| !id.is_empty()) {\n            Some(operation_id) => self.link', '        match result["operation_id"].as_str() {\n            Some(operation_id) => self.link'),
    # ---- the work of a thread
    (K, 'vectors', 'work is given to a thread by a connection that lost it', '        self.owned(opened, true)?;\n        if owner_of(operation_id).as_deref() != Some(opened.owner) {', '        self.owned(opened, false)?;\n        if owner_of(operation_id).as_deref() != Some(opened.owner) {'),
    (K, 'vectors', "the work of another is a thread's work", '        if owner_of(operation_id).as_deref() != Some(opened.owner) {\n            return Err(NOT_THEIR_WORK);\n        }\n', ''),
    (K, 'vectors', "work nobody has is a thread's work", '        if owner_of(operation_id).as_deref() != Some(opened.owner) {', '        if owner_of(operation_id).is_some_and(|owner| owner != opened.owner) {'),
    (K, 'vectors', 'work is given to a thread as often as it is asked', '        if !self.linked.iter().any(there) {', '        if true {'),
    (K, 'vectors', 'work is the work of every thread that names it', '            |row: &Linked| row.thread_id == opened.thread_id && row.operation_id == operation_id;', '            |row: &Linked| row.operation_id == operation_id;'),
    (K, 'vectors', 'work was given to a thread at no time', '                operation_id: operation_id.to_string(),\n                created_at: now_ms,', '                operation_id: operation_id.to_string(),\n                created_at: 0,'),
    (K, 'vectors', 'work that is given to a thread is not written', '            self.changed.insert((LINKED, linked.name()));\n', ''),
    (K, 'vectors', 'the work of a thread is read only by the connection that has it', '        self.owned(opened, false)?;\n        let of_thread = |row: &&Delegation| {', '        self.owned(opened, true)?;\n        let of_thread = |row: &&Delegation| {'),
    (K, 'vectors', 'the work of a thread is every work of its owner', '            self.linked.iter().any(linked)\n', '            row.owner_principal == opened.owner\n'),
    (K, 'vectors', 'the work of a thread is the work of all threads', '                link.thread_id == opened.thread_id && link.operation_id == row.delegation_id', '                link.operation_id == row.delegation_id'),
    (K, 'vectors', 'the work of a thread is told the oldest first', '            let newest = other.created_at.cmp(&one.created_at);', '            let newest = one.created_at.cmp(&other.created_at);'),
    (K, 'vectors', 'work of one moment is told by its name the smallest first', '            newest.then_with(|| other.delegation_id.cmp(&one.delegation_id))', '            newest.then_with(|| one.delegation_id.cmp(&other.delegation_id))'),
    # ---- the actions of a thread
    (K, 'vectors', 'the actions of a thread are read only by the connection that has it', '        self.owned(opened, false)?;\n        let wanted = |row: &&Begun| {', '        self.owned(opened, true)?;\n        let wanted = |row: &&Begun| {'),
    (K, 'vectors', 'the actions of a thread are those of every thread', '            row.thread_id == opened.thread_id\n                && revision.is_none_or(|revision| row.revision == revision)', '            revision.is_none_or(|revision| row.revision == revision)'),
    (K, 'vectors', 'the actions for one number are all the actions', '                && revision.is_none_or(|revision| row.revision == revision)', '                && revision.is_none_or(|_| true)'),
    (K, 'vectors', 'the actions are told in the order they were begun', '            threads.then_with(|| place(one).cmp(&place(other)))', '            threads'),
    # ---- an action whose receipt was lost
    (K, 'vectors', 'an action that was ended names no work again', '            if row.completed {\n                let result = row.result_json.as_deref().unwrap_or("null");', '            if false {\n                let result = row.result_json.as_deref().unwrap_or("null");'),
    (K, 'vectors', 'the work of an action that was ended is not given to its thread again', '                    self.link(opened, operation_id, &owner_of, now_ms)?;\n                }\n                continue;', '                }\n                continue;'),
    (K, 'vectors', 'an action that was ended is ended again by what it handed on', '                    self.link(opened, operation_id, &owner_of, now_ms)?;\n                }\n                continue;\n', '                    self.link(opened, operation_id, &owner_of, now_ms)?;\n                }\n'),
    (K, 'vectors', 'work handed to an agent is not known by its action', '                "delegate_to_agent" | "investigate_with_oracle" => {', '                "investigate_with_oracle" => {'),
    (K, 'vectors', 'work handed to the contact is not known by its action', '                "delegate_to_agent" | "investigate_with_oracle" => {', '                "delegate_to_agent" => {'),
    (K, 'vectors', 'work handed on is known as work in the place of other work', '                    operation_id(opened.owner, &row.call_id, false)', '                    operation_id(opened.owner, &row.call_id, true)'),
    (K, 'vectors', 'a stop without other work is taken for done', '                "cancel_agent" if python::truthy(&arguments["request"]) => {', '                "cancel_agent" => {'),
    (K, 'vectors', 'work in the place of what was stopped is known as any work', '                    operation_id(opened.owner, &row.call_id, true)', '                    operation_id(opened.owner, &row.call_id, false)'),
    (K, 'vectors', "an action is ended by another's work", '            if let Some(work) = handed.filter(|work| work.owner_principal == opened.owner) {', '            if let Some(work) = handed {'),
    (K, 'vectors', 'an action whose receipt was lost is ended as accepted whatever became of its work', '                let result = json!({ "status": work.status.word(), "operation_id": operation_id });', '                let result = json!({ "status": "accepted", "operation_id": operation_id });'),
    # ---- what a voice was told of
    (K, 'vectors', 'a voice is told of a thing by a connection that lost its thread', '        let (kind, id) = told_as(kind, id)?;\n        self.owned(opened, true)?;', '        let (kind, id) = told_as(kind, id)?;\n        self.owned(opened, false)?;'),
    (K, 'vectors', 'a thing is told anew in a thread as often as it waits', '        if self.told.iter().any(there) {\n            return Ok(false);\n        }\n', ''),
    (K, 'vectors', 'a thing is told once in all threads', '            row.thread_id == opened.thread_id && row.source_kind == kind && row.source_id == id\n        };\n        if self.told.iter().any(there) {', '            row.source_kind == kind && row.source_id == id\n        };\n        if self.told.iter().any(there) {'),
    (K, 'vectors', 'things of one id are one thing whatever their kind', '            row.thread_id == opened.thread_id && row.source_kind == kind && row.source_id == id\n        };\n        if self.told.iter().any(there) {', '            row.thread_id == opened.thread_id && row.source_id == id\n        };\n        if self.told.iter().any(there) {'),
    (K, 'vectors', 'what was told is told of as never stale', '            reference_ts,\n            stale,\n            sent_at: now_ms,', '            reference_ts,\n            stale: false,\n            sent_at: now_ms,'),
    (K, 'vectors', 'what was told was told at no time', '            stale,\n            sent_at: now_ms,', '            stale,\n            sent_at: 0,'),
    (K, 'vectors', 'what was told is not written', '        self.changed.insert((TOLD, told.name()));\n        self.told.push(told);', '        self.told.push(told);'),
    (K, 'vectors', 'a thing told for the first time is said to have been told', '        self.told.push(told);\n        Ok(true)', '        self.told.push(told);\n        Ok(false)'),
    (K, 'vectors', 'what could not be told may be told again by a connection that lost its thread', '        let kind = python::first(python::strip(kind), KIND_SIGNS);\n        self.owned(opened, true)?;', '        let kind = python::first(python::strip(kind), KIND_SIGNS);\n        self.owned(opened, false)?;'),
    (K, 'vectors', 'what could not be told is let go in every thread', '            row.thread_id == opened.thread_id\n                && row.owner_principal == opened.owner\n                && row.source_kind == kind', '            row.owner_principal == opened.owner\n                && row.source_kind == kind'),
    (K, 'vectors', 'what could not be told is let go whatever its kind', '                && row.source_kind == kind\n                && row.source_id == id\n        };\n        let gone', '                && row.source_id == id\n        };\n        let gone'),
    (K, 'vectors', 'what is let go is not written', '        self.changed.extend(gone.map(|name| (TOLD, name)));\n', '        drop(gone);\n'),
    (K, 'vectors', 'what is let go stays', '        self.told.retain(|row| !of_it(row));\n', ''),
    (K, 'vectors', 'a thing without an id may be let go', '        let id = python::strip(id);\n        if id.is_empty() {\n            return Err(NO_ID);\n        }\n        let kind = python::first(python::strip(kind), KIND_SIGNS);\n        self.owned(opened, true)?;', '        let id = python::strip(id);\n        let kind = python::first(python::strip(kind), KIND_SIGNS);\n        self.owned(opened, true)?;'),
    (K, 'vectors', 'a thing without an id may be told', '    let id = python::strip(id);\n    if id.is_empty() {\n        return Err(NO_ID);\n    }\n    let kind = python::first(python::strip(kind), KIND_SIGNS);\n    if kind.is_empty() {', '    let id = python::strip(id);\n    let kind = python::first(python::strip(kind), KIND_SIGNS);\n    if kind.is_empty() {'),
    (K, 'vectors', 'a thing of no kind may be told', '    if kind.is_empty() {\n        return Err(NO_KIND);\n    }\n', ''),
    (K, 'vectors', 'the blanks around an id are part of it', '    let id = python::strip(id);\n    if id.is_empty() {\n        return Err(NO_ID);\n    }\n    let kind = python::first(python::strip(kind), KIND_SIGNS);\n    if kind.is_empty() {', '    if id.is_empty() {\n        return Err(NO_ID);\n    }\n    let kind = python::first(python::strip(kind), KIND_SIGNS);\n    if kind.is_empty() {'),
    (K, 'vectors', 'a kind may be as long as it likes', '    let kind = python::first(python::strip(kind), KIND_SIGNS);\n    if kind.is_empty() {', '    let kind = python::strip(kind);\n    if kind.is_empty() {'),
    (K, 'vectors', 'what a voice was told of is told of every thread', '        let of_thread = self.told.iter().filter(|row| row.thread_id == thread_id);', '        let of_thread = self.told.iter().filter(|_| !thread_id.is_empty());'),
    # ---- ages and times
    (K, 'vectors', 'an age is counted from a moment later', '    let seconds = (now_ms - ms).max(0) / 1000;', '    let seconds = (now_ms - ms).max(0) / 1000 + 1;'),
    (K, 'vectors', 'what is to come was long ago', '    let seconds = (now_ms - ms).max(0) / 1000;', '    let seconds = (now_ms - ms).abs() / 1000;'),
    (K, 'vectors', 'a minute is not a minute yet', '        if seconds >= size {', '        if seconds > size {'),
    (K, 'vectors', 'one day is days', '            let many = if count == 1 { "" } else { "s" };', '            let many = "s";'),
    (K, 'vectors', 'a day is of 24 hours and a minute', '[("day", 86_400), ("hour", 3600), ("minute", 60)]', '[("day", 86_460), ("hour", 3600), ("minute", 60)]'),
    (K, 'vectors', 'hours are not told', '[("day", 86_400), ("hour", 3600), ("minute", 60)]', '[("day", 86_400), ("minute", 60)]'),
    (K, 'vectors', 'a time is told without how far its clocks are from the world', '"%Y-%m-%dT%H:%M:%S%:z"', '"%Y-%m-%dT%H:%M:%S"'),
    (K, 'vectors', "a time is told on the world's own clocks", '    let shown = moment.to_zoned(zone.clone());', '    let shown = moment.to_zoned(TimeZone::UTC);'),
    (K, 'vectors', 'a record keeps the time no clock shows', '        None => {\n            row.shift_remove(key);\n        }', '        None => {}'),
    (K, 'vectors', 'a record with a time is not told how long ago', '            row.insert("age".to_string(), json!(age_phrase(ms, now_ms)));\n', ''),
    (K, 'vectors', 'a record keeps its time as a number', '            row.insert(key.to_string(), json!(time));\n', ''),
    # ---- what of a result is shown
    (K, 'vectors', 'what was asked is shown with what was added for the agent', '    let asked = row.request_text.split("<oracle-reference-data>").next();', '    let asked = Some(row.request_text.as_str());'),
    (K, 'vectors', 'what was asked is shown with the blanks around it', '    let asked = python::strip(asked.unwrap_or(""));', '    let asked = asked.unwrap_or("");'),
    (K, 'vectors', 'what was asked is shown 121 signs long', '            json!(python::first(asked, 120)),', '            json!(python::first(asked, 121)),'),
    (K, 'vectors', 'what was asked is said to be cut at 120 signs', '        let longer = asked.chars().count() > 120;', '        let longer = asked.chars().count() >= 120;'),
    (K, 'vectors', 'a result is shown whole however long', '    while !finding.is_empty() && written(&record(&finding, cut)).chars().count() > room {', '    while false && written(&record(&finding, cut)).chars().count() > room {'),
    (K, 'vectors', 'a record of exactly its room is too long', 'written(&record(&finding, cut)).chars().count() > room {', 'written(&record(&finding, cut)).chars().count() >= room {'),
    (K, 'vectors', 'a result is cut at a full stop alone', '        let ends = [". ", "! ", "? "].map(|end| finding.rfind(end));', '        let ends = [". "].map(|end| finding.rfind(end));'),
    (K, 'vectors', 'a result is cut at its first sentence', '        finding = match ends.into_iter().flatten().max().filter(|end| *end > 0) {', '        finding = match ends.into_iter().flatten().min().filter(|end| *end > 0) {'),
    (K, 'vectors', 'a result is cut before the sign that ends its sentence', '            Some(end) => finding[..=end].to_string(),', '            Some(end) => finding[..end].to_string(),'),
    (K, 'vectors', 'one sentence that is too long is cut by forty signs', '                let signs = finding.chars().count().saturating_sub(80);', '                let signs = finding.chars().count().saturating_sub(40);'),
    (K, 'vectors', 'a result that was cut is not said to be', '        cut = true;\n', ''),
    # ---- the excerpt a call begins with
    (K, 'vectors', 'the newest work is kept room for whatever came of it', '    let reserved = excerpt.work.iter().find(over).or(excerpt.work.first());', '    let reserved = excerpt.work.first();'),
    (K, 'vectors', 'room is kept only for work that is over', '    let reserved = excerpt.work.iter().find(over).or(excerpt.work.first());', '    let reserved = excerpt.work.iter().find(over);'),
    (K, 'vectors', 'work that is over and said nothing is kept room for', '        !row.status.active() && !(row.result_text.is_empty() && row.error.is_empty())', '        !row.status.active()'),
    (K, 'vectors', 'work that is under way is kept room for as work that is over', '        !row.status.active() && !(row.result_text.is_empty() && row.error.is_empty())', '        !(row.result_text.is_empty() && row.error.is_empty())'),
    (K, 'vectors', 'more agents are named than a voice is told', '        .take(AGENTS_NAMED)', '        .take(AGENTS_NAMED + 1)'),
    (K, 'vectors', 'the agents are never said to be an excerpt', '            "is_excerpt": agents.len() > AGENTS_NAMED,', '            "is_excerpt": false,'),
    (K, 'vectors', 'thirty agents are an excerpt', '            "is_excerpt": agents.len() > AGENTS_NAMED,', '            "is_excerpt": agents.len() >= AGENTS_NAMED,'),
    (K, 'vectors', 'the contact of a call is not told with its history', '            "oracle_contact": excerpt.roster["oracle_contact"],', '            "oracle_contact": null,'),
    (K, 'vectors', 'the newest result is shown in less room', '        .map(|row| with_time(result_payload(row, RESULT_ROOM), row))', '        .map(|row| with_time(result_payload(row, RESULT_ROOM - 1), row))'),
    (K, 'vectors', 'the talk is told the oldest first in the choosing', '    let said: Vec<Map<String, Value>> = said\n        .iter()\n        .rev()\n', '    let said: Vec<Map<String, Value>> = said\n        .iter()\n'),
    (K, 'vectors', 'the other work has the newest again', '        .filter(|row| reserved.is_none_or(|reserved| reserved.delegation_id != row.delegation_id));', '        .filter(|_| true);'),
    (K, 'vectors', 'other work is shown by its error first', '            let answer = if row.result_text.is_empty() {\n                &row.error\n            } else {\n                &row.result_text\n            };\n            let mut record = Map::new();', '            let answer = if !row.error.is_empty() {\n                &row.error\n            } else {\n                &row.result_text\n            };\n            let mut record = Map::new();'),
    (K, 'vectors', 'the talk goes before the newest result', '    let lists = [\n        ("work", newest),\n        ("recent_conversation", said),', '    let lists = [\n        ("recent_conversation", said),\n        ("work", newest),'),
    (K, 'vectors', 'what the user added goes before the talk', '        ("recent_conversation", said),\n        ("user_context", added),', '        ("user_context", added),\n        ("recent_conversation", said),'),
    (K, 'vectors', 'an excerpt may be a byte longer', '            if python::canonical(&Value::Object(longer.clone())).len() <= HISTORY_BYTES {', '            if python::canonical(&Value::Object(longer.clone())).len() <= HISTORY_BYTES + 1 {'),
    (K, 'vectors', 'an excerpt of exactly its bound is too long', '            if python::canonical(&Value::Object(longer.clone())).len() <= HISTORY_BYTES {', '            if python::canonical(&Value::Object(longer.clone())).len() < HISTORY_BYTES {'),
    (K, 'vectors', 'the talk is told the newest first', '    if let Some(Value::Array(said)) = payload.get_mut("recent_conversation") {\n        said.reverse();\n    }\n', ''),
    (K, 'vectors', 'an excerpt does not say that it is one', '    payload.insert("history_is_excerpt".to_string(), json!(true));\n', ''),
    (K, 'vectors', 'an excerpt is told to the voice as its own words', '        "role": "user",\n        "content": [{ "type": "input_text", "text": text }],', '        "role": "assistant",\n        "content": [{ "type": "input_text", "text": text }],'),
    (K, 'vectors', 'an excerpt is told without what it is', '        "{HISTORY_HEAD}{}",', '        "{}",'),
    (K, 'vectors', 'an excerpt does not name its thread', '    payload.insert("thread_id".to_string(), json!(excerpt.thread_id));\n', ''),
    (K, 'vectors', 'an excerpt does not say when it is', '    payload.insert("now".to_string(), json!(iso_time(now_ms, zone)));\n', ''),
    (K, 'vectors', 'an excerpt may be acted on', '    payload.insert("reference_only".to_string(), json!(true));\n', ''),
    (K, 'vectors', 'an excerpt comes without how it is to be read', '    payload.insert("replay_guidance".to_string(), json!(GUIDANCE));\n', ''),
    # ---- which rows changed
    (K, 'vectors', 'the memory forgets which rows it changed only when it is made anew', '        let changed = std::mem::take(&mut self.changed);', '        let changed = self.changed.clone();'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
