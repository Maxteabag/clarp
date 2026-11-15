//! Compression as a port.
//!
//! A stored body carries the tag of the codec that wrote it. Changing the configured compressor
//! changes what new writes use; old data stays readable and is never rewritten for that reason.

use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;

/// Which codec wrote a stored body. A tag is permanent once data carries it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct CodecTag(pub u16);

#[derive(Debug)]
pub enum CodecError {
    /// The stored bytes are not something this codec wrote.
    Corrupt(String),
    /// The codec could not compress the input.
    Encode(String),
    /// A body carries a tag that no registered codec answers to.
    UnknownTag(CodecTag),
}

impl fmt::Display for CodecError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Corrupt(why) => write!(f, "stored body is corrupt: {why}"),
            Self::Encode(why) => write!(f, "body could not be compressed: {why}"),
            Self::UnknownTag(tag) => write!(f, "no codec registered for tag {}", tag.0),
        }
    }
}

impl std::error::Error for CodecError {}

/// One way of turning a body into stored bytes and back.
pub trait Compressor: Send + Sync {
    /// A short stable name for configuration and diagnostics, for example `zstd`.
    fn name(&self) -> &str;
    fn compress(&self, plain: &[u8]) -> Result<Vec<u8>, CodecError>;
    fn decompress(&self, stored: &[u8]) -> Result<Vec<u8>, CodecError>;
}

/// The codecs a store can read, and the one it writes with.
#[derive(Clone)]
pub struct CodecSet {
    by_tag: BTreeMap<CodecTag, Arc<dyn Compressor>>,
    write: CodecTag,
}

impl CodecSet {
    /// A set that writes with `writer` under `write`.
    pub fn new(write: CodecTag, writer: Arc<dyn Compressor>) -> Self {
        let mut by_tag = BTreeMap::new();
        by_tag.insert(write, writer);
        Self { by_tag, write }
    }

    /// Adds a codec that existing data may carry.
    #[must_use]
    pub fn with(mut self, tag: CodecTag, codec: Arc<dyn Compressor>) -> Self {
        self.by_tag.insert(tag, codec);
        self
    }

    /// The same codecs, writing with another registered one.
    pub fn writing_with(&self, tag: CodecTag) -> Result<Self, CodecError> {
        if self.by_tag.contains_key(&tag) {
            Ok(Self {
                by_tag: self.by_tag.clone(),
                write: tag,
            })
        } else {
            Err(CodecError::UnknownTag(tag))
        }
    }

    pub fn write_tag(&self) -> CodecTag {
        self.write
    }

    pub fn encode(&self, plain: &[u8]) -> Result<(CodecTag, Vec<u8>), CodecError> {
        let codec = self
            .by_tag
            .get(&self.write)
            .ok_or(CodecError::UnknownTag(self.write))?;
        Ok((self.write, codec.compress(plain)?))
    }

    pub fn decode(&self, tag: CodecTag, stored: &[u8]) -> Result<Vec<u8>, CodecError> {
        let codec = self.by_tag.get(&tag).ok_or(CodecError::UnknownTag(tag))?;
        codec.decompress(stored)
    }
}

impl fmt::Debug for CodecSet {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let names: Vec<(u16, &str)> = self
            .by_tag
            .iter()
            .map(|(tag, codec)| (tag.0, codec.name()))
            .collect();
        f.debug_struct("CodecSet")
            .field("write", &self.write.0)
            .field("codecs", &names)
            .finish()
    }
}
