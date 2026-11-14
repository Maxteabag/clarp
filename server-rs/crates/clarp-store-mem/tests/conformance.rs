//! The in-memory engine passes the suite every log engine must pass.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::Path;

use clarp_codec::standard_set;
use clarp_ports::testkit::log_engine_conformance;
use clarp_store_mem::MemEngine;

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
