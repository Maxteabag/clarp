//! The compressors the Host ships with, behind [`clarp_ports::codec::Compressor`].
//!
//! Tags are permanent: once a log holds a body written under a tag, that tag must keep meaning
//! the same codec. Tags below [`FIRST_DICTIONARY_TAG`] are fixed here; each dictionary gets its
//! own tag from the fleet's registry, because a dictionary is part of the data format.

use std::io::{Read, Write};
use std::sync::Arc;

use clarp_ports::codec::{CodecError, CodecSet, CodecTag, Compressor};

pub const TAG_NONE: CodecTag = CodecTag(0);
pub const TAG_ZSTD: CodecTag = CodecTag(1);
pub const FIRST_DICTIONARY_TAG: CodecTag = CodecTag(16);

/// The level the report's storage experiment measured: fast, and 0.53 of the size without a dictionary.
pub const DEFAULT_ZSTD_LEVEL: i32 = 3;

/// Stores bodies as they are.
#[derive(Debug, Default, Clone, Copy)]
pub struct NoneCodec;

impl Compressor for NoneCodec {
    fn name(&self) -> &str {
        "none"
    }

    fn compress(&self, plain: &[u8]) -> Result<Vec<u8>, CodecError> {
        Ok(plain.to_vec())
    }

    fn decompress(&self, stored: &[u8]) -> Result<Vec<u8>, CodecError> {
        Ok(stored.to_vec())
    }
}

/// Zstandard without a dictionary.
#[derive(Debug, Clone, Copy)]
pub struct Zstd {
    level: i32,
}

impl Zstd {
    pub fn new(level: i32) -> Self {
        Self { level }
    }
}

impl Default for Zstd {
    fn default() -> Self {
        Self::new(DEFAULT_ZSTD_LEVEL)
    }
}

impl Compressor for Zstd {
    fn name(&self) -> &str {
        "zstd"
    }

    fn compress(&self, plain: &[u8]) -> Result<Vec<u8>, CodecError> {
        zstd::encode_all(plain, self.level).map_err(|e| CodecError::Encode(e.to_string()))
    }

    fn decompress(&self, stored: &[u8]) -> Result<Vec<u8>, CodecError> {
        zstd::decode_all(stored).map_err(|e| CodecError::Corrupt(e.to_string()))
    }
}

/// Zstandard with a shared dictionary: what makes small bodies compress well
/// (0.29 of today's size in the report's experiment, against 0.53 without one).
#[derive(Debug, Clone)]
pub struct ZstdDict {
    level: i32,
    dictionary: Vec<u8>,
}

impl ZstdDict {
    pub fn new(level: i32, dictionary: Vec<u8>) -> Self {
        Self { level, dictionary }
    }
}

impl Compressor for ZstdDict {
    fn name(&self) -> &str {
        "zstd-dict"
    }

    fn compress(&self, plain: &[u8]) -> Result<Vec<u8>, CodecError> {
        let encode = |e: std::io::Error| CodecError::Encode(e.to_string());
        let mut encoder =
            zstd::stream::write::Encoder::with_dictionary(Vec::new(), self.level, &self.dictionary)
                .map_err(encode)?;
        encoder.write_all(plain).map_err(encode)?;
        encoder.finish().map_err(encode)
    }

    fn decompress(&self, stored: &[u8]) -> Result<Vec<u8>, CodecError> {
        let corrupt = |e: std::io::Error| CodecError::Corrupt(e.to_string());
        let mut decoder = zstd::stream::read::Decoder::with_dictionary(stored, &self.dictionary)
            .map_err(corrupt)?;
        let mut plain = Vec::new();
        decoder.read_to_end(&mut plain).map_err(corrupt)?;
        Ok(plain)
    }
}

/// The codec a configuration value names, with its tag. Dictionary codecs are not named here:
/// they come from the fleet's registry with their dictionary.
pub fn by_name(name: &str) -> Option<(CodecTag, Arc<dyn Compressor>)> {
    match name {
        "none" => Some((TAG_NONE, Arc::new(NoneCodec))),
        "zstd" => Some((TAG_ZSTD, Arc::new(Zstd::default()))),
        _ => None,
    }
}

/// A set that reads every built-in codec and writes with the named one.
pub fn standard_set(write: &str) -> Option<CodecSet> {
    let (tag, writer) = by_name(write)?;
    let mut set = CodecSet::new(tag, writer);
    for name in ["none", "zstd"] {
        if let Some((other_tag, codec)) = by_name(name) {
            set = set.with(other_tag, codec);
        }
    }
    Some(set)
}
