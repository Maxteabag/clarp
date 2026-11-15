//! The in-memory engine passes the suite every log engine must pass.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::Path;

use clarp_codec::standard_set;
use clarp_ports::testkit::{blob_conformance, key_value_conformance, log_engine_conformance};
use clarp_store_mem::{MemBlobs, MemEngine, MemKeyValue};

#[test]
fn memory_passes_the_log_engine_suite() {
    let engine = MemEngine::new();
    let none = standard_set("none").unwrap();
    let zstd = standard_set("zstd").unwrap();
    log_engine_conformance(
        &engine,
        Path::new("/agents/a"),
        Path::new("/agents/b"),
        &none,
        &zstd,
    );
}

#[test]
fn memory_passes_the_suite_starting_compressed() {
    let engine = MemEngine::new();
    let none = standard_set("none").unwrap();
    let zstd = standard_set("zstd").unwrap();
    log_engine_conformance(
        &engine,
        Path::new("/agents/a"),
        Path::new("/agents/b"),
        &zstd,
        &none,
    );
}

#[test]
fn memory_passes_the_key_value_suite() {
    key_value_conformance(
        &MemKeyValue::new(),
        Path::new("/fleet-a.db"),
        Path::new("/fleet-b.db"),
    );
}

#[test]
fn memory_passes_the_suite_of_whole_files() {
    blob_conformance(
        &MemBlobs::new(),
        Path::new("/media-a"),
        Path::new("/media-b"),
    );
}
