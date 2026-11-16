//! What a published file is read as, against the Python Host's reading.
//!
//! `fixtures/media-vectors.json` is written by `server-rs/tools/media-vectors.py` from
//! `server/lib/media_store.py`: made-up files of every kind the store takes and of kinds it
//! refuses, under the names and the declared types an agent might give. The same must be read
//! here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::media::{
    Asset, FILES, IMAGES, MAX_BYTES, Part, Refused, byte_range, detect_mime, extension, image_size,
    is_image, is_taken, markdown_alt, read, safe_name, unquoted, upload_directory, upload_id,
    upload_name,
};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/media-vectors.json")).unwrap()
}

fn bytes(hex: &str) -> Vec<u8> {
    (0..hex.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&hex[at..at + 2], 16).unwrap())
        .collect()
}

#[test]
fn the_kinds_that_are_taken_are_the_python_hosts() {
    let vectors = vectors();
    let listed = |key: &str| -> Vec<String> {
        vectors[key]
            .as_array()
            .unwrap()
            .iter()
            .map(|kind| kind.as_str().unwrap().to_string())
            .collect()
    };
    assert_eq!(listed("images"), IMAGES);
    assert_eq!(listed("files"), FILES);
    assert_eq!(vectors["max_bytes"], MAX_BYTES);
}

#[test]
fn an_upload_is_kept_under_its_agents_name_and_its_own_id_as_the_python_host_keeps_it() {
    let vectors = vectors();
    for vector in vectors["sessions"].as_array().unwrap() {
        assert_eq!(
            upload_directory(vector["session"].as_str().unwrap()),
            vector["safe"].as_str().unwrap(),
            "{vector}"
        );
    }
    let ids = vectors["upload_ids"].as_array().unwrap();
    assert!(ids.len() >= 10);
    for vector in ids {
        let ours = upload_id(vector["given"].as_str().unwrap());
        assert_eq!(ours, vector["id"].as_str(), "{vector}");
    }
}

#[test]
fn a_file_is_named_as_the_python_host_names_it() {
    let vectors = vectors();
    let names = vectors["names"].as_array().unwrap();
    assert!(names.len() >= 150);
    let mut wrong = Vec::new();
    for vector in names {
        let (raw, declared) = (
            vector["raw"].as_str().unwrap(),
            vector["content_type"].as_str().unwrap(),
        );
        let ours = safe_name(raw, declared);
        if ours != vector["safe"].as_str().unwrap() {
            wrong.push(format!(
                "{raw:?} as {declared:?}: {ours:?}, the Python Host: {}",
                vector["safe"]
            ));
        }
        // And as what a user attached.
        let ours = upload_name(raw, declared);
        if ours != vector["upload"].as_str().unwrap() {
            wrong.push(format!(
                "{raw:?} as {declared:?}, attached: {ours:?}, the Python Host: {}",
                vector["upload"]
            ));
        }
    }
    assert!(
        wrong.is_empty(),
        "{} named differently:\n{}",
        wrong.len(),
        wrong.join("\n")
    );
}

#[test]
fn a_file_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let blobs: BTreeMap<String, Vec<u8>> = vectors["blobs"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(label, hex)| (label.clone(), bytes(hex.as_str().unwrap())))
        .collect();
    let readings = vectors["read"].as_array().unwrap();
    assert!(readings.len() >= 1_500);
    let mut wrong = Vec::new();
    for vector in readings {
        let blob = &blobs[vector["blob"].as_str().unwrap()];
        let (declared, name) = (
            vector["content_type"].as_str().unwrap(),
            vector["name"].as_str().unwrap(),
        );
        let safe = safe_name(name, declared);
        let mime = detect_mime(blob, declared, &safe);
        let (width, height) = image_size(blob, &mime);
        let ours = (
            safe.clone(),
            mime.clone(),
            is_taken(&mime),
            is_image(&mime),
            extension(&safe, &mime),
            width.map(u64::from),
            height.map(u64::from),
        );
        let theirs = (
            vector["safe_name"].as_str().unwrap().to_string(),
            vector["mime"].as_str().unwrap().to_string(),
            vector["taken"].as_bool().unwrap(),
            vector["image"].as_bool().unwrap(),
            vector["extension"].as_str().unwrap().to_string(),
            vector["width"].as_u64(),
            vector["height"].as_u64(),
        );
        if ours != theirs {
            wrong.push(format!(
                "{} declared {declared:?} named {name:?}: {ours:?}, the Python Host: {theirs:?}",
                vector["blob"]
            ));
        }
        // And read in one go, as a file that is handed in is: taken as that, or refused.
        let whole = read(blob, name, declared);
        let agrees = match &whole {
            Ok(reading) => {
                theirs.2
                    && (&reading.name, &reading.mime, &reading.extension)
                        == (&ours.0, &ours.1, &ours.4)
                    && (reading.width, reading.height) == (width, height)
            }
            Err(refused) => !theirs.2 && *refused == Refused::Kind,
        };
        if !agrees {
            wrong.push(format!(
                "{} declared {declared:?} named {name:?}: handed in, it is {whole:?}",
                vector["blob"]
            ));
        }
    }
    assert!(
        wrong.is_empty(),
        "{} of {} read differently:\n{}",
        wrong.len(),
        readings.len(),
        wrong.join("\n")
    );
}

#[test]
fn a_file_is_kept_under_the_extension_the_python_host_keeps_it_under() {
    let vectors = vectors();
    let mut wrong = Vec::new();
    for vector in vectors["extensions"].as_array().unwrap() {
        let (name, mime) = (
            vector["name"].as_str().unwrap(),
            vector["mime"].as_str().unwrap(),
        );
        let ours = extension(name, mime);
        if ours != vector["extension"].as_str().unwrap() {
            wrong.push(format!(
                "{name:?} of {mime:?}: {ours:?}, the Python Host: {}",
                vector["extension"]
            ));
        }
    }
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    // The part of Python's table of file types this carries is that table's.
    for (mime, of_type) in vectors["extension_of_type"].as_object().unwrap() {
        let expected = of_type.as_str().unwrap_or(".img");
        assert_eq!(extension("", mime), expected, "{mime}");
    }
    // What a name's ending alone makes of a file whose bytes say nothing and that was
    // declared as nothing.
    for (ending, read_as) in vectors["read_by_ending"].as_object().unwrap() {
        let named = format!("x{ending}");
        assert_eq!(
            detect_mime(b"\x00\x01\x02", "", &named),
            read_as.as_str().unwrap(),
            "{named}"
        );
    }
}

#[test]
fn a_published_file_is_told_as_the_python_host_tells_it() {
    let vectors = vectors();
    for vector in vectors["alts"].as_array().unwrap() {
        assert_eq!(
            markdown_alt(vector["text"].as_str().unwrap()),
            vector["alt"].as_str().unwrap()
        );
    }
    let rows = vectors["rows"].as_array().unwrap();
    assert!(rows.len() >= 7);
    for vector in rows {
        let row = &vector["row"];
        let text = |key: &str| row[key].as_str().unwrap().to_string();
        let asset = Asset {
            asset_id: text("asset_id"),
            session: text("session"),
            source_name: text("source_name"),
            sha256: text("sha256"),
            extension: String::new(),
            mime_type: text("mime_type"),
            bytes: row["bytes"].as_u64().unwrap(),
            width: row["width"]
                .as_u64()
                .map(|width| u32::try_from(width).unwrap()),
            height: row["height"]
                .as_u64()
                .map(|height| u32::try_from(height).unwrap()),
            caption: text("caption"),
            created_by: text("created_by"),
            created_at: row["created_at"].as_i64().unwrap(),
            named_only: false,
        };
        assert_eq!(asset.public(), vector["public"], "{row}");
    }
}

#[test]
fn a_part_of_a_file_is_asked_for_as_the_python_host_reads_it() {
    let vectors = vectors();
    let ranges = vectors["ranges"].as_array().unwrap();
    assert!(ranges.len() >= 200);
    let mut wrong = Vec::new();
    for vector in ranges {
        let (header, total) = (
            vector["header"].as_str().unwrap(),
            vector["total"].as_u64().unwrap(),
        );
        let theirs = match vector["kind"].as_str().unwrap() {
            "full" => Part::Whole,
            "unsatisfiable" => Part::Missing,
            "partial" => Part::Bytes {
                first: vector["start"].as_u64().unwrap(),
                last: vector["end"].as_u64().unwrap(),
            },
            other => panic!("a kind of range nobody knows: {other}"),
        };
        let ours = byte_range(header, total);
        if ours != theirs {
            let shown: String = header.chars().take(60).collect();
            wrong.push(format!(
                "{shown:?} of {total}: {ours:?}, the Python Host: {theirs:?}"
            ));
        }
    }
    assert!(
        wrong.is_empty(),
        "{} of {} read differently:\n{}",
        wrong.len(),
        ranges.len(),
        wrong.join("\n")
    );
}

#[test]
fn a_name_in_a_header_is_read_back_as_python_reads_it() {
    let vectors = vectors();
    let quoted = vectors["quoted"].as_array().unwrap();
    assert!(quoted.len() >= 30);
    for vector in quoted {
        let text = vector["text"].as_str().unwrap();
        assert_eq!(
            unquoted(text),
            vector["unquoted"].as_str().unwrap(),
            "{text:?}"
        );
    }
}
