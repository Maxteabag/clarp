//! Every compressor passes the port's suite, and the standard set switches without losing data.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_codec::{NoneCodec, TAG_NONE, TAG_ZSTD, Zstd, ZstdDict, standard_set};
use clarp_ports::testkit::compressor_conformance;

#[test]
fn none_passes_the_suite() {
    compressor_conformance(&NoneCodec);
}

#[test]
fn zstd_passes_the_suite() {
    compressor_conformance(&Zstd::default());
}

#[test]
fn zstd_with_a_dictionary_passes_the_suite() {
    // Any bytes serve as a raw-content dictionary; a trained one only compresses better.
    let dictionary = b"{\"role\":\"assistant\",\"text\":\"\",\"tools\":[],\"turn\":0}".repeat(40);
    compressor_conformance(&ZstdDict::new(3, dictionary));
}

#[test]
fn zstd_shrinks_prose() {
    use clarp_ports::codec::Compressor;
    let prose = b"The Host answers the apps. The runtime owns the agents. ".repeat(400);
    let stored = Zstd::default().compress(&prose).unwrap();
    assert!(
        stored.len() * 10 < prose.len(),
        "repetitive prose compresses at least tenfold, got {} of {}",
        stored.len(),
        prose.len()
    );
}

#[test]
fn a_dictionary_is_needed_to_read_what_it_wrote() {
    use clarp_ports::codec::Compressor;
    let dictionary = b"common words that many bodies share, again and again. ".repeat(30);
    let with = ZstdDict::new(3, dictionary);
    let stored = with
        .compress(b"common words that many bodies share")
        .unwrap();
    let wrong = ZstdDict::new(
        3,
        b"another dictionary entirely, nothing in common here at all".repeat(30),
    );
    let read = wrong.decompress(&stored);
    assert!(
        read.is_err() || read.unwrap() != b"common words that many bodies share",
        "the wrong dictionary must not silently read the body"
    );
}

#[test]
fn the_standard_set_switches_compressor_and_still_reads_old_bodies() {
    let plain = b"a body written before the configuration changed".to_vec();
    let before = standard_set("none").unwrap();
    let (tag, stored) = before.encode(&plain).unwrap();
    assert_eq!(tag, TAG_NONE);
    let after = standard_set("zstd").unwrap();
    assert_eq!(after.write_tag(), TAG_ZSTD);
    assert_eq!(after.decode(tag, &stored).unwrap(), plain);
    let (new_tag, new_stored) = after.encode(&plain).unwrap();
    assert_eq!(new_tag, TAG_ZSTD);
    assert_eq!(before.decode(new_tag, &new_stored).unwrap(), plain);
    assert!(
        standard_set("brotli").is_none(),
        "an unknown name is refused, not guessed"
    );
}
