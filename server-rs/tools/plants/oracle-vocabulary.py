#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The words an owner prepared for the Oracle's contact, as a call with that contact takes them: the kernel's rule of
which small file is one and which words stand in the text they were taken from, and how the Host finds the file,
reads it and the text, refuses a call for it and tells the voice the words.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'oracle_vocabulary_vectors'],
    'host': ['test', '--test', 'host', '--', 'a_call_of_the_oracle_is_told_the_words'],
}
K = 'clarp-kernel/src/oracle/voice.rs'
C = 'clarp-host/src/oracle/call.rs'
# NOT PLANTED, each with its reason:
#   that the Host reads no more of a small file than a small file may have, and a byte (`let most =
#     voice::VOCABULARY_FILE_MOST as u64`): the kernel's rule refuses what is larger whatever was read of it. It only
#     saves the reading.
#   that a reading which broke is a file that cannot be read (`read.unwrap_or_else(...)`, after the thread the files
#     are read on): nothing in the reading breaks.
#   that a vocabulary is read once the way a call goes about its work is known, and not before: whose contact a call
#     has is only known then, and the words are a contact's.
#   that a small file which is no JSON to this Host though it is to Python's reader is refused: no rule of this Host
#     says so, its reader of JSON does (the cases marked `only_the_python_host` hold what comes of it).
OF_ANOTHER = '    if !first || contact.is_empty() || its != Some(contact) {'
THE_TEXT = '    let Some(raw) = Some(path).filter(|path| !path.is_empty()).and_then(text) else {'
THE_DIGEST = '    if binding.get("sha256").and_then(Value::as_str) != Some(digest.as_str()) {'
NO_UTF8 = '    let Ok(original) = String::from_utf8(raw) else {'
LINES = "                && !word.contains(['\\n', '\\r'])"
IN_THE_TEXT = '                && original.contains(&python::casefold(word))'
LONG = '            (1..=WORD_SIGNS_MOST).contains(&long)'
MANY = '    let Some(words) = words.filter(|words| (1..=WORDS_MOST).contains(&words.len())) else {'
TOLD = '        "primary_session": contact, "prepared_context_sha256": digest, "terms": words,'
NAMED = ('        let named = std::env::var(VOCABULARY_FILE).ok();\n'
         '        let named = named.filter(|path| !path.is_empty()).or_else(|| {\n'
         '            let file = owners.read()?;\n'
         '            owner::text_at(&file, &["openai", "oracle_voice_context_file"])\n'
         '        });')
OWNERS_FIRST = ('        let named = owners.read().and_then(|file| owner::text_at(&file, &["openai", "oracle_voice_context_file"]));\n'
                '        let named = named.or_else(|| std::env::var(VOCABULARY_FILE).ok().filter(|path| !path.is_empty()));')
FIRST_OF_ALL = ('        let vocabulary = vocabulary(app, &contact).await?;\n'
                '        if !named(query, "podcast_artifact").is_empty() {\n'
                '            return Err("Podcast detours are not available on this Host".to_string());\n'
                '        }\n'
                '        for name in ["thread_id", "new_conversation"] {\n'
                '            if named(query, name).len() > 1 {\n'
                '                return Err("Use one Oracle context selection".to_string());\n'
                '            }\n'
                '        }\n'
                '        let fresh = match named(query, "new_conversation").first() {\n'
                '            None | Some(&"0") => false,\n'
                '            Some(&"1") => true,\n'
                '            Some(_) => return Err("new_conversation must be 0 or 1".to_string()),\n'
                '        };\n')
AFTER_THE_REST = FIRST_OF_ALL.split('\n', 1)[1] + FIRST_OF_ALL.split('\n', 1)[0] + '\n'
PLANTS = [
    # Which small file is one: the kernel's rule.
    (K, 'vectors', 'a small file a byte too large is read', '    if file.len() > VOCABULARY_FILE_MOST {', '    if file.len() > VOCABULARY_FILE_MOST + 1 {'),
    (K, 'vectors', 'a small file of the most bytes is too large', '    if file.len() > VOCABULARY_FILE_MOST {', '    if file.len() >= VOCABULARY_FILE_MOST {'),
    (K, 'vectors', 'the mark of its order before a small file makes it unreadable', '    let written = file.strip_prefix(b"\\xef\\xbb\\xbf").unwrap_or(file);', '    let written = file;'),
    (K, 'vectors', 'a file that is no JSON is said to be no object', '        return refused(NO_VOCABULARY);\n    };\n    let Some(held) = held.as_object() else {', '        return refused("Prepared voice context must be an object");\n    };\n    let Some(held) = held.as_object() else {'),
    (K, 'vectors', 'a small file that is a list is said to be of another contact', '        return refused("Prepared voice context must be an object");', '        return refused("Prepared voice context does not match selected primary");'),
    (K, 'vectors', 'a version with a point is another version', '        Some(Value::Number(version)) => version.as_f64() == Some(1.0),', '        Some(Value::Number(version)) => version.as_i64() == Some(1),'),
    (K, 'vectors', 'any version that is a number is the first', '        Some(Value::Number(version)) => version.as_f64() == Some(1.0),', '        Some(Value::Number(version)) => version.as_f64().is_some(),'),
    (K, 'vectors', 'a version that is a yes is none', '        Some(Value::Bool(yes)) => *yes,', '        Some(Value::Bool(_)) => false,'),
    (K, 'vectors', 'a version that is a no is the first', '        Some(Value::Bool(yes)) => *yes,', '        Some(Value::Bool(_)) => true,'),
    (K, 'vectors', 'a version that is a text, or none, is the first', '        _ => false,\n    };\n    let its = held', '        _ => true,\n    };\n    let its = held'),
    (K, 'vectors', 'a file of another contact is taken', OF_ANOTHER, '    if !first || contact.is_empty() || its.is_none() {'),
    (K, 'vectors', 'a file that names no contact is taken', OF_ANOTHER, '    if !first || contact.is_empty() || its.is_some_and(|its| its != contact) {'),
    (K, 'vectors', 'a call with no contact takes a file that names none', OF_ANOTHER, '    if !first || its != Some(contact) {'),
    (K, 'vectors', 'a contact is the same in other letters', OF_ANOTHER, '    if !first || contact.is_empty() || its.map(python::casefold) != Some(python::casefold(contact)) {'),
    (K, 'vectors', 'a binding that is no object is none at all', '        Some(_) => return refused("Invalid prepared context binding"),', '        Some(_) => &nothing,'),
    (K, 'vectors', 'a binding that is nothing is said to be none that can be', '        .filter(|said| python::truthy(said))\n    {\n        None => &nothing,', '        .filter(|_| true)\n    {\n        None => &nothing,'),
    (K, 'vectors', 'a text is asked for at no path', THE_TEXT, '    let Some(raw) = text(path) else {'),
    (K, 'vectors', 'no text at the path is said to be a small file that cannot be read', THE_TEXT + '\n        return refused(NO_TEXT);', THE_TEXT + '\n        return refused(NO_VOCABULARY);'),
    (K, 'vectors', 'a text is the one its words were taken from whatever its digest', THE_DIGEST, '    if binding.get("sha256").is_none() {'),
    (K, 'vectors', 'a digest is the same in large letters', THE_DIGEST, '    if binding.get("sha256").and_then(Value::as_str).map(str::to_lowercase) != Some(digest.clone()) {'),
    (K, 'vectors', 'a digest is written in large letters', '    bytes.iter().map(|byte| format!("{byte:02x}")).collect()', '    bytes.iter().map(|byte| format!("{byte:02X}")).collect()'),
    (K, 'vectors', 'a text that is no UTF-8 is read as far as it is one', NO_UTF8, '    let Ok(original) = Ok::<_, ()>(String::from_utf8_lossy(&raw).into_owned()) else {'),
    (K, 'vectors', 'a text that is no UTF-8 is said to be a small file that cannot be read', NO_UTF8 + '\n        return refused(NO_TEXT);', NO_UTF8 + '\n        return refused(NO_VOCABULARY);'),
    # Which words stand in the text.
    (K, 'vectors', 'the letters of the text are made small as Rust makes them', '    let original = python::casefold(&original);', '    let original = original.to_lowercase();'),
    (K, 'vectors', 'the letters of a word are made small as Rust makes them', IN_THE_TEXT, '                && original.contains(&word.to_lowercase())'),
    (K, 'vectors', 'a word the text has not is taken', LINES + '\n' + IN_THE_TEXT, LINES),
    (K, 'vectors', 'a word with a return in it is taken', LINES, "                && !word.contains('\\n')"),
    (K, 'vectors', 'a word of two lines is taken', LINES, "                && !word.contains('\\r')"),
    (K, 'vectors', 'a word may be a sign longer', LONG, '            (1..=WORD_SIGNS_MOST + 1).contains(&long)'),
    (K, 'vectors', 'a word of the most signs is too long', LONG, '            (1..WORD_SIGNS_MOST).contains(&long)'),
    (K, 'vectors', 'an empty word is a word', LONG, '            (0..=WORD_SIGNS_MOST).contains(&long)'),
    (K, 'vectors', 'a word is measured in bytes', '            let long = word.chars().count();', '            let long = word.len();'),
    (K, 'vectors', 'a word that is no text is passed over', '        if fits.is_none() {', '        if word.is_string() && fits.is_none() {'),
    (K, 'vectors', 'no words are a vocabulary', MANY, MANY.replace('(1..=WORDS_MOST)', '(0..=WORDS_MOST)')),
    (K, 'vectors', 'one word more is a vocabulary', MANY, MANY.replace('(1..=WORDS_MOST)', '(1..=WORDS_MOST + 1)')),
    (K, 'vectors', 'as many words as there may be are too many', MANY, MANY.replace('(1..=WORDS_MOST)', '(1..WORDS_MOST)')),
    (K, 'vectors', 'the words together may have a sign more', '    if signs > WORDS_SIGNS_MOST {', '    if signs > WORDS_SIGNS_MOST + 1 {'),
    (K, 'vectors', 'as many signs as the words may have together are too many', '    if signs > WORDS_SIGNS_MOST {', '    if signs >= WORDS_SIGNS_MOST {'),
    # What is said of a vocabulary that is taken.
    (K, 'vectors', 'the words are told in the order of their letters', TOLD, TOLD.replace('"terms": words,', '"terms": { let mut told = words.clone(); told.sort_by_key(Value::to_string); told },')),
    (K, 'vectors', 'the digest of a small file leaves out the mark of its order', '        "sidecar_sha256": hex(&Sha256::digest(file)),', '        "sidecar_sha256": hex(&Sha256::digest(written)),'),
    # How the Host finds the file, reads it and the text, and tells the voice.
    (C, 'host', 'the words that were read are not told', '        vocabulary: prepared.map(|context| &context["terms"]),', '        vocabulary: None,'),
    (C, 'host', 'the voice is told all that was read of the file', '        vocabulary: prepared.map(|context| &context["terms"]),', '        vocabulary: prepared,'),
    (C, 'host', 'the environment names no file', '        let named = std::env::var(VOCABULARY_FILE).ok();', '        let named: Option<String> = None;'),
    (C, 'host', 'an empty word of the environment names a file', '        let named = named.filter(|path| !path.is_empty()).or_else(|| {', '        let named = named.or_else(|| {'),
    (C, 'host', "the owner's file names the file by another setting", '            owner::text_at(&file, &["openai", "oracle_voice_context_file"])', '            owner::text_at(&file, &["openai", "voice_context_file"])'),
    (C, 'host', "the owner's file is asked before the environment", NAMED, OWNERS_FIRST),
    (C, 'host', 'a file that is named and is not there is no file named', '            return Err(voice::NO_VOCABULARY.to_string());\n        };\n        voice::prepared', '            return Ok(None);\n        };\n        voice::prepared'),
    (C, 'host', 'a file is read a byte short', '    file.take(most + 1).read_to_end(&mut held).ok()?;', '    file.take(most).read_to_end(&mut held).ok()?;'),
    (C, 'host', 'a text a byte too large is a text', '    (held.len() as u64 <= voice::VOCABULARY_TEXT_MOST).then_some(held)', '    Some(held)'),
    (C, 'host', 'a text of the most bytes is too large', '    (held.len() as u64 <= voice::VOCABULARY_TEXT_MOST).then_some(held)', '    ((held.len() as u64) < voice::VOCABULARY_TEXT_MOST).then_some(held)'),
    (C, 'host', 'a path that is not whole is read from where the Host stands', '    if !path.is_absolute() || !path.is_file() {', '    if !path.is_file() {'),
    (C, 'host', 'what can be read and is no file is read as a text', '    if !path.is_absolute() || !path.is_file() {', '    if !path.is_absolute() {'),
    (C, 'host', 'a vocabulary is read after the rest of the call is asked', FIRST_OF_ALL, AFTER_THE_REST),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
