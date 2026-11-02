//! Cross-process exclusive file locks behind a two-symbol C ABI.
//!
//! Python loads this library through ctypes. Handles are opaque u64 tokens
//! into a process-local registry, so no platform descriptor crosses the FFI
//! boundary. On an OS error the raw OS error code is written to `out_handle`.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub const WENYI_LOCK_OK: i32 = 0;
pub const WENYI_LOCK_BUSY: i32 = 1;
pub const WENYI_LOCK_INVALID: i32 = 2;
pub const WENYI_LOCK_OS_ERROR: i32 = 3;

const RETRY_INTERVAL: Duration = Duration::from_millis(50);

/// One locked file held on behalf of the caller.
struct Held {
    file: File,
}

static REGISTRY: Mutex<Option<BTreeMap<u64, Held>>> = Mutex::new(None);
static NEXT_HANDLE: AtomicU64 = AtomicU64::new(1);

/// Result of one non-blocking exclusive-lock attempt.
enum Attempt {
    Acquired,
    Busy,
    Failed(i64),
}

#[cfg(windows)]
mod platform {
    use std::fs::File;
    use std::os::windows::io::AsRawHandle;

    use windows_sys::Win32::Foundation::{ERROR_LOCK_VIOLATION, HANDLE};
    use windows_sys::Win32::Storage::FileSystem::{
        LockFileEx, UnlockFileEx, LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY,
    };
    use windows_sys::Win32::System::IO::OVERLAPPED;

    use crate::Attempt;

    fn overlapped() -> OVERLAPPED {
        // Byte zero on a zeroed structure is the only lock range used here.
        unsafe { std::mem::zeroed() }
    }

    pub(crate) fn try_lock(file: &File) -> Attempt {
        let mut overlapped = overlapped();
        // SAFETY: the handle is owned by `file`, and `overlapped` outlives the call.
        let ok = unsafe {
            LockFileEx(
                file.as_raw_handle() as HANDLE,
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                0,
                1,
                0,
                &mut overlapped,
            )
        };
        if ok != 0 {
            return Attempt::Acquired;
        }
        let code = unsafe { windows_sys::Win32::Foundation::GetLastError() } as i64;
        if code == ERROR_LOCK_VIOLATION as i64 {
            Attempt::Busy
        } else {
            Attempt::Failed(code)
        }
    }

    pub(crate) fn unlock(file: &File) {
        let mut overlapped = overlapped();
        // SAFETY: same reasoning as `try_lock`; a failed unlock only closes the file.
        unsafe {
            UnlockFileEx(
                file.as_raw_handle() as HANDLE,
                0,
                1,
                0,
                &mut overlapped,
            );
        }
    }
}

#[cfg(unix)]
mod platform {
    use std::fs::File;
    use std::os::unix::io::AsRawFd;

    use crate::Attempt;

    pub(crate) fn try_lock(file: &File) -> Attempt {
        // SAFETY: the descriptor is owned by `file` for the duration of the call.
        let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        if result == 0 {
            return Attempt::Acquired;
        }
        let code = std::io::Error::last_os_error();
        match code.raw_os_error() {
            Some(value) if value == libc::EWOULDBLOCK || value == libc::EAGAIN => Attempt::Busy,
            Some(value) => Attempt::Failed(value as i64),
            None => Attempt::Failed(-1),
        }
    }

    pub(crate) fn unlock(file: &File) {
        // SAFETY: same reasoning as `try_lock`; a failed unlock only closes the file.
        unsafe {
            libc::flock(file.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

fn registry() -> std::sync::MutexGuard<'static, Option<BTreeMap<u64, Held>>> {
    REGISTRY.lock().unwrap_or_else(|error| error.into_inner())
}

/// Acquire an exclusive lock on the file at `path` (UTF-8, `path_len` bytes).
///
/// `blocking == 0`: a single attempt. Otherwise retry every 50 ms until the
/// lock is acquired or `timeout_ms` elapses (`0` waits forever). Returns
/// `WENYI_LOCK_OK` and writes the handle to `out_handle`; `WENYI_LOCK_BUSY`
/// when a non-blocking attempt or a deadline finds another owner;
/// `WENYI_LOCK_INVALID` for null or empty arguments, or when `out_handle` is
/// null; `WENYI_LOCK_OS_ERROR` with the raw OS error code in `out_handle`
/// otherwise.
///
/// # Safety
///
/// `path` must point to `path_len` readable bytes, and `out_handle` must be
/// writable when non-null.
#[no_mangle]
pub unsafe extern "C" fn wenyi_lock_acquire(
    path: *const u8,
    path_len: usize,
    blocking: u8,
    timeout_ms: u32,
    out_handle: *mut u64,
) -> i32 {
    if path.is_null() || path_len == 0 || out_handle.is_null() {
        return WENYI_LOCK_INVALID;
    }
    let bytes = std::slice::from_raw_parts(path, path_len);
    let Ok(text) = std::str::from_utf8(bytes) else {
        return WENYI_LOCK_INVALID;
    };
    let Ok(file) = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(Path::new(text))
    else {
        let code = std::io::Error::last_os_error()
            .raw_os_error()
            .unwrap_or(-1) as i64 as u64;
        std::ptr::write(out_handle, code);
        return WENYI_LOCK_OS_ERROR;
    };

    let deadline = if blocking == 0 || timeout_ms == 0 {
        None
    } else {
        Some(Instant::now() + Duration::from_millis(timeout_ms as u64))
    };

    loop {
        match platform::try_lock(&file) {
            Attempt::Acquired => break,
            Attempt::Busy => {
                if blocking == 0 {
                    return WENYI_LOCK_BUSY;
                }
                if let Some(limit) = deadline {
                    if Instant::now() >= limit {
                        return WENYI_LOCK_BUSY;
                    }
                }
                std::thread::sleep(RETRY_INTERVAL);
            }
            Attempt::Failed(code) => {
                std::ptr::write(out_handle, code as u64);
                return WENYI_LOCK_OS_ERROR;
            }
        }
    }

    let handle = NEXT_HANDLE.fetch_add(1, Ordering::Relaxed);
    registry()
        .get_or_insert_with(BTreeMap::new)
        .insert(handle, Held { file });
    std::ptr::write(out_handle, handle);
    WENYI_LOCK_OK
}

/// Release a handle returned by `wenyi_lock_acquire`.
///
/// Returns `WENYI_LOCK_OK`, or `WENYI_LOCK_INVALID` for an unknown handle.
#[no_mangle]
pub extern "C" fn wenyi_lock_release(handle: u64) -> i32 {
    let mut guard = registry();
    let Some(held) = guard.as_mut().and_then(|map| map.remove(&handle)) else {
        return WENYI_LOCK_INVALID;
    };
    platform::unlock(&held.file);
    drop(held);
    WENYI_LOCK_OK
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_path(name: &str) -> PathBuf {
        let mut path = std::env::temp_dir();
        path.push(format!("wenyi-locks-{}-{}", std::process::id(), name));
        let _ = std::fs::remove_file(&path);
        path
    }

    fn acquire(path: &Path, blocking: u8, timeout_ms: u32) -> (i32, u64) {
        let bytes = path.to_str().unwrap().as_bytes();
        let mut handle: u64 = 0;
        let code = unsafe {
            wenyi_lock_acquire(bytes.as_ptr(), bytes.len(), blocking, timeout_ms, &mut handle)
        };
        (code, handle)
    }

    #[test]
    fn acquire_then_release_reports_ok() {
        let path = temp_path("basic");
        let (code, handle) = acquire(&path, 0, 0);
        assert_eq!(code, WENYI_LOCK_OK);
        assert!(handle > 0);
        assert_eq!(wenyi_lock_release(handle), WENYI_LOCK_OK);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn second_acquire_reports_busy_and_unknown_handle_is_invalid() {
        let path = temp_path("busy");
        let (code, handle) = acquire(&path, 0, 0);
        assert_eq!(code, WENYI_LOCK_OK);
        let (busy, _) = acquire(&path, 0, 0);
        assert_eq!(busy, WENYI_LOCK_BUSY);
        assert_eq!(wenyi_lock_release(handle), WENYI_LOCK_OK);
        assert_eq!(wenyi_lock_release(handle), WENYI_LOCK_INVALID);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn blocking_acquire_waits_for_the_holder_to_release() {
        let path = temp_path("blocking");
        let (code, handle) = acquire(&path, 0, 0);
        assert_eq!(code, WENYI_LOCK_OK);

        let waiting = path.clone();
        let waiter = std::thread::spawn(move || acquire(&waiting, 1, 5000));
        std::thread::sleep(Duration::from_millis(120));
        assert_eq!(wenyi_lock_release(handle), WENYI_LOCK_OK);

        let (waited_code, waited_handle) = waiter.join().unwrap();
        assert_eq!(waited_code, WENYI_LOCK_OK);
        assert_eq!(wenyi_lock_release(waited_handle), WENYI_LOCK_OK);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn blocking_acquire_gives_up_at_the_deadline() {
        let path = temp_path("deadline");
        let (code, handle) = acquire(&path, 0, 0);
        assert_eq!(code, WENYI_LOCK_OK);
        let (busy, _) = acquire(&path, 1, 100);
        assert_eq!(busy, WENYI_LOCK_BUSY);
        assert_eq!(wenyi_lock_release(handle), WENYI_LOCK_OK);
        let _ = std::fs::remove_file(&path);
    }
}
