//! Directory snapshot taken before an upgrade installs anything.
//!
//! The update path calls this between shutting the engine down and starting the
//! installer, which is the only moment the data directory is static.
//!
//! Nothing here opens SQLite. The crate carries no SQLite binding (a C build
//! dependency is exactly what the Windows build avoids), so the snapshot is a
//! byte-for-byte copy plus a manifest that proves the copy. Byte equality is the
//! stronger claim: it holds for every table this code has never heard of.
//!
//! `Snapshot::load` is what the *next* version reads. The process that writes a
//! backup and the process that trusts it are two different binaries, so a module
//! that could only write would leave the new version with no way to check a copy
//! the old one made.

mod backup;

pub use backup::{snapshot_sources, BackupSource};
// The reading half of the contract. Nothing in this build calls it: a backup is
// written by the version being replaced and read by the version that replaces it,
// so the caller is the next version's first launch, not this one. Keeping the
// functions compiled is what stops that reader from being written against an
// interface nobody has exercised.
#[allow(unused_imports)]
pub use backup::{preflight, snapshot, verify, Entry, Snapshot};

use std::fmt;

#[derive(Debug)]
pub enum Error {
    /// The data directory could not be read. Nothing was written.
    Source(std::io::Error),
    /// The snapshot could not be written. A partial copy may remain, but it
    /// never carries a manifest, so it can never pass for a complete backup.
    Target(std::io::Error),
    /// The target volume cannot hold the copy plus the headroom it needs.
    InsufficientSpace { required: u64, available: u64 },
    /// A file under the snapshot root does not match what the manifest claims.
    ManifestMismatch(String),
    /// An entry that cannot be reproduced byte for byte: a link, a special
    /// file, or a name that is not valid Unicode.
    Unsupported(String),
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Source(_) => formatter.write_str(
                "Wenyi could not read its data folder, so the update was cancelled.",
            ),
            Self::Target(_) => formatter.write_str(
                "Wenyi could not write the update backup, so the update was cancelled.",
            ),
            Self::InsufficientSpace {
                required,
                available,
            } => write!(
                formatter,
                "The update backup needs {} but only {} is free on that drive, so the update was cancelled.",
                size(*required),
                size(*available)
            ),
            Self::ManifestMismatch(path) => write!(
                formatter,
                "The update backup does not match its manifest ({path}), so the update was cancelled."
            ),
            Self::Unsupported(path) => write!(
                formatter,
                "Wenyi cannot back up {path} byte for byte, so the update was cancelled."
            ),
        }
    }
}

impl std::error::Error for Error {
    /// The underlying filesystem failure, when there is one.
    ///
    /// The message above is what a person reads; this is what a support log keeps,
    /// and it is the only way the operating system's own reason survives the
    /// conversion to a displayable string.
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Source(error) | Self::Target(error) => Some(error),
            _ => None,
        }
    }
}

/// Sizes in a form a person can compare at a glance.
fn size(bytes: u64) -> String {
    const MEGABYTE: f64 = 1024.0 * 1024.0;
    const GIGABYTE: f64 = 1024.0 * MEGABYTE;
    let bytes = bytes as f64;
    if bytes >= GIGABYTE {
        format!("{:.1} GB", bytes / GIGABYTE)
    } else {
        format!("{:.0} MB", (bytes / MEGABYTE).ceil())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_failure_says_that_the_update_was_cancelled() {
        let failures = [
            Error::Source(std::io::Error::other("gone")),
            Error::Target(std::io::Error::other("full")),
            Error::ManifestMismatch("local.sqlite3".into()),
            Error::Unsupported("link".into()),
            Error::InsufficientSpace {
                required: 2_000_000_000,
                available: 1_000_000_000,
            },
        ];
        for failure in failures {
            let message = failure.to_string();
            assert!(message.contains("cancelled"), "{message}");
        }
    }

    #[test]
    fn space_failures_report_both_numbers() {
        let message = Error::InsufficientSpace {
            required: 3 * 1024 * 1024 * 1024,
            available: 1024 * 1024 * 1024,
        }
        .to_string();
        assert!(message.contains("3.0 GB"), "{message}");
        assert!(message.contains("1.0 GB"), "{message}");
    }

    #[test]
    fn small_amounts_are_not_rounded_down_to_zero() {
        assert_eq!(size(1), "1 MB");
        assert_eq!(size(1024 * 1024), "1 MB");
    }
}
