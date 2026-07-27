//! Standalone numerical primitives for Heimdall ranging and CIR processing.
//! Inputs deliberately do not depend on the wire protocol crate.

pub mod calibration;
pub mod cir;
pub mod filter;
pub mod ranging;
pub mod spectral;

pub use calibration::*;
pub use cir::*;
pub use filter::*;
pub use ranging::*;
pub use spectral::*;

/// Speed of light used by the DW3000 ranging model, in metres per second.
pub const SPEED_OF_LIGHT_M_S: f64 = 299_702_547.0;
/// DW3000 device-time units per second (499.2 MHz * 128).
pub const DW_DTU_PER_SECOND: f64 = 63_897_600_000.0;
pub const METRES_PER_DTU: f64 = SPEED_OF_LIGHT_M_S / DW_DTU_PER_SECOND;
