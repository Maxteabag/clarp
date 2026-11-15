//! Files on disk pass the suite every store of whole files must pass, and keep their own
//! promises.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_ports::blobs::BlobEngine;
use clarp_ports::testkit::{blob_conformance, scratch_dir};
use clarp_store_files::FileBlobs;

#[test]
fn files_pass_the_suite_of_whole_files() {
    let root = scratch_dir("files-suite");
    blob_conformance(&FileBlobs::new(), &root.join("a"), &root.join("b"));
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_file_lies_under_its_name_in_a_directory_named_after_how_it_begins() {
    let root = scratch_dir("files-layout");
    let store = FileBlobs::new().open(&root).unwrap();
    let name = format!("{}.png", "ab".repeat(32));
    store.put(&name, b"an image").unwrap();
    assert_eq!(
        std::fs::read(root.join("ab").join(&name)).unwrap(),
        b"an image"
    );
    // Nothing else is left beside it.
    let beside: Vec<_> = std::fs::read_dir(root.join("ab"))
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect();
    assert_eq!(beside, [std::ffi::OsString::from(&name)]);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn what_a_process_that_died_left_half_written_is_never_read_and_is_removed() {
    let root = scratch_dir("files-leftover");
    let name = "cdcdcd.bin";
    // A process with a number no process of this machine has died after it had written half
    // of a file.
    std::fs::create_dir_all(root.join("cd")).unwrap();
    let left = root.join("cd").join(format!(".{name}.4294967295-0.part"));
    std::fs::write(&left, b"half of").unwrap();

    let store = FileBlobs::new().open(&root).unwrap();
    assert!(
        !left.exists(),
        "the leftover is removed when the store opens"
    );
    assert_eq!(store.len(name).unwrap(), None, "and was never a file");
    store.put(name, b"half of it and the rest").unwrap();
    assert_eq!(
        store.read(name, 0, 100).unwrap(),
        b"half of it and the rest"
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_directory_in_a_files_place_is_no_file() {
    let root = scratch_dir("files-directory");
    let store = FileBlobs::new().open(&root).unwrap();
    std::fs::create_dir_all(root.join("ef").join("efef.bin")).unwrap();
    assert_eq!(store.len("efef.bin").unwrap(), None);
    assert!(
        store.put("efef.bin", b"bytes").is_err(),
        "and what cannot be kept is said, not passed over"
    );
    std::fs::remove_dir_all(root).unwrap();
}
