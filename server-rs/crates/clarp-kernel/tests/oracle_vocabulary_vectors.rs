//! The vocabulary an owner prepared for the Oracle's contact, against the Python Host's own.
//!
//! `fixtures/oracle-vocabulary-vectors.json` is written by
//! `server-rs/tools/oracle-vocabulary-vectors.py` from
//! `server/lib/oracle_voice_context.load`, run on files of that program's own: for every case
//! the small file, what stands at the path it names, and what came of it. The same must
//! come of the same here: what is taken, as it is taken; and what is refused, in the same
//! words, but where the words are the Python language's own for a file that cannot be read
//! (no JSON, a path that is no text, a text that is not UTF-8): there this Host refuses in
//! words of its own. And a small file that is JSON to Python's reader alone (signs of two
//! or four bytes each, half a pair of signs written out, a number that is none) is no JSON
//! here: this Host refuses what the Python Host takes there.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::oracle::voice;
use clarp_kernel::python;
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle-vocabulary-vectors.json")).unwrap()
}

#[test]
fn a_prepared_vocabulary_is_taken_and_refused_as_the_python_host_does() {
    let vectors = vectors();
    let (mut taken, mut refused, mut otherwise, mut alone) = (0, 0, 0, 0);
    for case in vectors["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let file = python::b64decode(case["file"].as_str().unwrap()).unwrap();
        let bytes = usize::try_from(case["bytes"].as_u64().unwrap()).unwrap();
        assert_eq!(file.len(), bytes, "{name}");
        // What a Host's reader finds at the path the file names; and it is asked for no
        // other path.
        let text = |path: &str| {
            assert_eq!(case["asks_for"], path, "{name}");
            let found = case["text"].as_str()?;
            Some(python::b64decode(found).unwrap())
        };
        let got = voice::prepared(case["primary"].as_str().unwrap(), &file, text);
        if case["only_the_python_host"] == true {
            assert!(case.get("taken").is_some(), "{name}");
            assert_eq!(got, Err(voice::NO_VOCABULARY.to_string()), "{name}");
            alone += 1;
            continue;
        }
        match (&got, case.get("taken"), case["in_its_own_words"].as_bool()) {
            (Ok(context), Some(expected), _) => {
                assert_eq!(context.to_string(), expected.to_string(), "{name}");
                taken += 1;
            }
            (Err(why), None, Some(true)) => {
                assert_eq!(case["refused"], why.as_str(), "{name}");
                refused += 1;
            }
            // The Python language's own words for a file it cannot read: refused here too,
            // in this Host's words for what it was that could not be read.
            (Err(why), None, Some(false)) => {
                let own = match case["refused"].as_str().unwrap() {
                    "JSONDecodeError" => voice::NO_VOCABULARY,
                    "TypeError" | "UnicodeDecodeError" => {
                        "Prepared voice context source is missing or invalid"
                    }
                    other => panic!("{name}: {other}"),
                };
                assert_eq!(why, own, "{name}");
                otherwise += 1;
            }
            (got, expected, _) => panic!("{name}: {got:?}, and the Python Host: {expected:?}"),
        }
    }
    assert_eq!((taken, refused, otherwise, alone), (14, 38, 6, 5));
    assert_eq!(vectors["without_a_file"], serde_json::json!([null, ""]));
    assert_eq!(voice::VOCABULARY_FILE_MOST, 16_384);
    assert_eq!(voice::VOCABULARY_TEXT_MOST, 2 * 1024 * 1024);
}
