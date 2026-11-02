//! Poison-tolerant mutex acquisition for command-facing state.
//!
//! A panicked thread must not crash every later command through a poisoned
//! mutex. The panic itself is already recorded once by the global panic hook
//! (`main.rs`, `Event::NativePanic`), so this helper only recovers the guard
//! instead of recording a second event.

use std::sync::{Mutex, MutexGuard};

pub(crate) fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|error| error.into_inner())
}
