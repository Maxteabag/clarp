//! Frame a bounded window of JSONL for all observers of the same open file.
//! The incomplete payload lives only in the reader, never in a job record.
#[derive(Default)]
pub(crate) struct Lines {
    pub fragment: Vec<u8>,
    pub discard: bool,
}

impl Lines {
    /// The visitor receives the position immediately after the line. False
    /// leaves subsequent bytes for the next visit.
    pub fn feed(
        &mut self,
        data: &[u8],
        offset: u64,
        limit: usize,
        mut line: impl FnMut(&[u8], u64) -> bool,
    ) -> usize {
        let mut used = 0;
        for part in data.split_inclusive(|b| *b == b'\n') {
            used += part.len();
            if !self.discard && self.fragment.len().saturating_add(part.len()) <= limit {
                self.fragment.extend_from_slice(part);
            } else {
                self.fragment.clear();
                self.discard = true;
            }
            if part.last() == Some(&b'\n') {
                let keep = self.discard || line(&self.fragment, offset + used as u64);
                self.fragment.clear();
                self.discard = false;
                if !keep {
                    break;
                }
            }
        }
        used
    }
}
