//! Atomic publication of a staged export file.
//!
//! An export is written to a temporary sibling and only then moved into place.
//! The destination is never replaced blindly: a file that appeared after the
//! user confirmed the path must survive, which is why every platform branch
//! below refuses to overwrite.

use std::path::Path;

use cap_std::fs::Dir;

/// Removes the staged file unless publication already moved it.
pub(crate) struct Temporary<'a> {
    pub(crate) dir: &'a Dir,
    pub(crate) name: String,
}

impl Drop for Temporary<'_> {
    fn drop(&mut self) {
        let _ = self.dir.remove_file(&self.name);
    }
}

/// Move `temporary` to `name`, failing if the destination already exists.
///
/// Each platform branch reads a different subset of the arguments: the unix
/// renames use the directory handle, Windows renames by path, and the portable
/// fallback links inside the directory. The signature stays the same on every
/// target so call sites do not fork, which leaves some arguments unread on some
/// platforms.
#[allow(unused_variables)]
pub(crate) fn publish_new(
    dir: &Dir,
    name: &Path,
    path: &Path,
    temporary: &str,
) -> std::io::Result<()> {
    // No check-then-overwriting-rename fallback: a file appearing after the
    // confirmation/validation must remain untouched, including on FAT/exFAT.
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    {
        use std::{
            ffi::CString,
            os::unix::{ffi::OsStrExt, io::AsRawFd},
        };
        let from = CString::new(temporary)?;
        let to = CString::new(name.as_os_str().as_bytes())?;
        let directory = dir.as_raw_fd();
        #[cfg(target_os = "linux")]
        let result = unsafe {
            libc::renameat2(
                directory,
                from.as_ptr(),
                directory,
                to.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        };
        #[cfg(target_os = "macos")]
        let result = unsafe {
            libc::renameatx_np(
                directory,
                from.as_ptr(),
                directory,
                to.as_ptr(),
                libc::RENAME_EXCL,
            )
        };
        if result != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_WRITE_THROUGH};
        let from: Vec<u16> = path
            .parent()
            .unwrap()
            .join(temporary)
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect();
        let to: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        // Deliberately omit MOVEFILE_REPLACE_EXISTING and COPY_ALLOWED.
        let result = unsafe { MoveFileExW(from.as_ptr(), to.as_ptr(), MOVEFILE_WRITE_THROUGH) };
        if result == 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
    {
        // Safe fallback on other targets, without overwriting an unconfirmed file.
        dir.hard_link(temporary, dir, name)
    }
}
