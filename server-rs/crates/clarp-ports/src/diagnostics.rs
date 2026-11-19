//! The single writer owns this bounded, expiring event store.
pub trait DiagnosticStore: Send {
    /// Prune expired buckets, append complete records, and return the count refused.
    fn append(&mut self, now_ms:i64, records:&[(i64,Vec<u8>)])->std::io::Result<usize>;
}

/// Read-only accounting for the resource sampler.
pub trait DiagnosticUsage {
    fn usage(&self)->std::io::Result<(usize,u64)>;
}
