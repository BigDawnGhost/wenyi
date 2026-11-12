//! The native half of the unified Desktop data-root contract.
use std::path::{Component, Path, PathBuf};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Base,
    Overlap,
    LibraryRootInvalid,
}

impl Error {
    pub fn message(self) -> &'static str {
        match self {
            Self::Base => "Wenyi could not determine its data folder. Check the user profile environment and reopen Wenyi.",
            Self::Overlap => "The Desktop data folder overlaps the program folder. Choose a separate data folder and reopen Wenyi.",
            Self::LibraryRootInvalid => "Choose a named project folder outside Wenyi's data, program and system folders.",
        }
    }
}

pub fn workspace(explicit: Option<PathBuf>) -> Result<PathBuf, Error> {
    let root = match explicit {
        Some(root) => std::path::absolute(root).map_err(|_| Error::Base)?,
        None => default_workspace()?,
    };
    if root.parent().is_none() || home_dir().is_ok_and(|home| same_directory(&home, &root)) {
        return Err(Error::LibraryRootInvalid);
    }
    Ok(root)
}

pub fn default_workspace() -> Result<PathBuf, Error> {
    let product = if cfg!(any(windows, target_os = "macos")) {
        "Wenyi"
    } else {
        "wenyi"
    };
    Ok(data_base()?.join(product).join("Desktop"))
}

pub fn locks(root: &Path) -> PathBuf {
    root.join("locks")
}

pub fn runtime_root(root: &Path) -> PathBuf {
    root.join("runtime")
}

pub fn webview(root: &Path) -> PathBuf {
    runtime_root(root).join("webview")
}

pub fn backups(root: &Path) -> PathBuf {
    root.join("backups")
}

/// Unwritable logs degrade to bounded memory, never another data directory.
pub fn log_roots(root: &Path) -> Vec<PathBuf> {
    vec![root.join("logs")]
}

pub fn running_install_dir() -> Result<PathBuf, Error> {
    std::env::current_exe()
        .map_err(|_| Error::Base)?
        .parent()
        .map(Path::to_path_buf)
        .ok_or(Error::Base)
}

fn normalized(path: &Path) -> String {
    // A not-yet-created destination still inherits its existing parent's
    // identity, including Windows short names and directory links.
    let canonical = path
        .ancestors()
        .find_map(|ancestor| {
            let root = ancestor.canonicalize().ok()?;
            Some(root.join(path.strip_prefix(ancestor).ok()?))
        })
        .unwrap_or_else(|| path.to_path_buf());
    let mut cleaned = PathBuf::new();
    for component in canonical.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                cleaned.pop();
            }
            part => cleaned.push(part.as_os_str()),
        }
    }
    let text = cleaned.to_string_lossy().replace('\\', "/");
    let text = text.strip_prefix("//?/").unwrap_or(&text);
    let text = text.trim_end_matches('/');
    if cfg!(windows) {
        text.to_lowercase()
    } else {
        text.to_owned()
    }
}

fn same_directory(left: &Path, right: &Path) -> bool {
    normalized(left) == normalized(right)
}

fn overlaps(left: &Path, right: &Path) -> bool {
    let left = normalized(left);
    let right = normalized(right);
    let contains =
        |outer: &str, inner: &str| outer == inner || inner.starts_with(&format!("{outer}/"));
    contains(&left, &right) || contains(&right, &left)
}

/// Used by layout admission and by exact-source backup planning.
pub fn check_disjoint(roots: &[(&str, &Path)]) -> Result<(), Error> {
    for (index, (_, path)) in roots.iter().enumerate() {
        for (_, other) in &roots[index + 1..] {
            if overlaps(path, other) {
                return Err(Error::Overlap);
            }
        }
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LayoutCheck {
    Consistent,
    Overlap,
}

pub fn check_layout(install: &Path, root: &Path) -> LayoutCheck {
    match check_disjoint(&[("install", install), ("data", root)]) {
        Ok(()) => LayoutCheck::Consistent,
        Err(_) => LayoutCheck::Overlap,
    }
}

impl LayoutCheck {
    pub fn message(self) -> Option<&'static str> {
        match self {
            Self::Consistent => None,
            Self::Overlap => Some(Error::Overlap.message()),
        }
    }
}

fn data_base() -> Result<PathBuf, Error> {
    #[cfg(windows)]
    {
        env_path("LOCALAPPDATA")
            .or_else(local_app_data)
            .ok_or(Error::Base)
    }
    #[cfg(target_os = "macos")]
    {
        Ok(home_dir()?.join("Library/Application Support"))
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        env_path("XDG_DATA_HOME")
            .map(Ok)
            .unwrap_or_else(|| Ok(home_dir()?.join(".local/share")))
    }
}

fn home_dir() -> Result<PathBuf, Error> {
    env_path(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).ok_or(Error::Base)
}

fn env_path(name: &str) -> Option<PathBuf> {
    let value = std::env::var_os(name)?;
    if value.to_string_lossy().trim().is_empty() {
        return None;
    }
    let path = PathBuf::from(value);
    path.is_absolute().then_some(path)
}

#[cfg(windows)]
fn local_app_data() -> Option<PathBuf> {
    use std::ffi::OsString;
    use std::os::windows::ffi::OsStringExt;
    use windows_sys::Win32::System::Com::CoTaskMemFree;
    use windows_sys::Win32::UI::Shell::{FOLDERID_LocalAppData, SHGetKnownFolderPath};

    let mut raw: *mut u16 = std::ptr::null_mut();
    // SAFETY: the shell returns a NUL-terminated allocation, freed on every path.
    let status =
        unsafe { SHGetKnownFolderPath(&FOLDERID_LocalAppData, 0, std::ptr::null_mut(), &mut raw) };
    if status < 0 || raw.is_null() {
        if !raw.is_null() {
            unsafe { CoTaskMemFree(raw.cast()) };
        }
        return None;
    }
    let length = (0..32768).find(|offset| unsafe { *raw.add(*offset) == 0 });
    let result = length.map(|length| {
        let units = unsafe { std::slice::from_raw_parts(raw, length) };
        PathBuf::from(OsString::from_wide(units))
    });
    unsafe { CoTaskMemFree(raw.cast()) };
    result.filter(|path| path.is_absolute())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_root_is_not_the_legacy_workspace_or_installation() {
        let root = default_workspace().unwrap();
        assert_eq!(root.file_name().unwrap(), "Desktop");
        assert!(root.is_absolute());
        assert_ne!(root.file_name().unwrap(), "Wenyi Desktop");
    }

    #[test]
    fn every_data_path_follows_one_explicit_root() {
        let root = Path::new("/chosen/data");
        assert_eq!(locks(root), root.join("locks"));
        assert_eq!(runtime_root(root), root.join("runtime"));
        assert_eq!(webview(root), root.join("runtime/webview"));
        assert_eq!(backups(root), root.join("backups"));
        assert_eq!(log_roots(root), vec![root.join("logs")]);
    }

    #[test]
    fn layout_checks_the_actual_explicit_root_not_a_global_profile() {
        let install = Path::new("/chosen/program");
        assert_eq!(check_layout(install, install), LayoutCheck::Overlap);
        assert_eq!(
            check_layout(install, &install.join("data")),
            LayoutCheck::Overlap
        );
        assert_eq!(
            check_layout(install, Path::new("/chosen/data")),
            LayoutCheck::Consistent
        );
        assert_eq!(
            check_layout(install, Path::new("/chosen/program2")),
            LayoutCheck::Consistent
        );
    }

    #[test]
    fn lexical_parent_components_cannot_bypass_overlap_checks() {
        assert!(check_disjoint(&[
            ("install", Path::new("/chosen/program")),
            ("data", Path::new("/chosen/data/../program/data")),
        ])
        .is_err());
    }

    #[test]
    fn nonexistent_descendants_keep_the_existing_parents_identity() {
        let parent = std::env::temp_dir();
        let canonical = parent.canonicalize().unwrap();
        let missing = parent.join(format!(
            "wenyi-missing-path-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        assert!(!missing.exists());
        assert!(check_disjoint(&[("source", &canonical), ("backup", &missing)]).is_err());
        assert!(check_disjoint(&[("source", &missing), ("backup", &canonical)]).is_err());
        assert_eq!(
            normalized(&missing),
            normalized(&canonical.join(missing.file_name().unwrap()))
        );
        assert!(!missing.exists());
    }
}
