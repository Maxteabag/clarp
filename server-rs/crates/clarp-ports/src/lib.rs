//! The ports of the Clarp Host.
//!
//! Every part of the server that has more than one implementation is a trait in this crate: the
//! compressor, the engine that stores an agent's log, the store of the fleet's own facts, the
//! store of the files agents publish, the clock. Each port comes with one
//! conformance suite in [`testkit`] that every implementation must pass, so that choosing an
//! implementation is a configuration value and not a porting project.
//!
//! A port needs two implementations to exist. The in-memory ones the simulator uses count.

pub mod blobs;
pub mod clock;
pub mod codec;
pub mod fcm;
pub mod kv;
pub mod log;
pub mod testkit;
