//! The single place where Wenyi's directory roots are named and resolved.
//!
//! The isolation design defines three layers, and every one of them is resolved
//! here instead of at its call site:
//!
//! | layer | root | lifetime |
//! |---|---|---|
//! | one | ~/.wenyi/desktop | rebuilt with the application |
//! | two | ~/Wenyi | backed up, shared across projects |
//! | three | a sibling of the installation directory | user documents |
//!
//! Resolution is pure: nothing here creates, canonicalizes, or checks a
//! directory on disk. Diagnostics deliberately derives its roots from the
//! admitted workspace and the platform base only (never from layers one to
//! three), so a startup that later fails its layout assertions can still write
//! the log that explains why.
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

/// Product name shared by the desktop workspace and its fallback log directory.
const PRODUCT: &str = "Wenyi Desktop";
/// Fallback log directory. The spelling deliberately differs from PRODUCT:
/// logs have to stay available when the workspace itself is not writable.
const LOGS: &str = "Wenyi Desktop Logs";
/// Directory inside a workspace that owns the diagnostic log files.
const DIAGNOSTICS: &str = "diagnostics";
/// Layer one root under the user profile.
const RUNTIME: &str = ".wenyi";
/// Layer one entry for this application inside RUNTIME.
const ENTRY: &str = "desktop";
/// Layer two root under the user profile.
const USER: &str = "Wenyi";
/// Layer three directory created next to the installation directory.
const LIBRARY: &str = "Wenyi Desktop Data";
/// Environment override for layer one. Layers two and three are deliberately
/// not overridable: layer two is persisted configuration and layer three is
/// recorded per project in the workspace registry.
const RUNTIME_OVERRIDE: &str = "WENYI_RUNTIME_DIR";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// The platform does not expose the base directory these roots derive from.
    Base,
    /// The installation directory is a volume root, so it has no sibling.
    InstallDirAtVolumeRoot,
    /// Two roots are equal or one contains the other.
    Overlap,
}

impl Error {
    /// Shown once the batch that enforces the layout turns these into a refusal.
    #[allow(dead_code)]
    pub fn message(self) -> &'static str {
        match self {
            Self::Base => "Wenyi could not determine where to keep its files on this computer. Check the user profile environment and reopen Wenyi.",
            Self::InstallDirAtVolumeRoot => "Wenyi is installed at a drive root. Reinstall it into a folder so its data can live beside it.",
            Self::Overlap => "Wenyi's data folders overlap. Move one of them and reopen Wenyi.",
        }
    }
}

/// The workspace a native parent admits and hands to the engine.
///
/// An explicit --data-dir always wins; otherwise the platform default below
/// applies. This is the value the engine receives, so it must stay identical to
/// the Python standalone fallback in wenyi_desktop.desktop.
pub fn workspace(explicit: Option<PathBuf>) -> Result<PathBuf, Error> {
    match explicit {
        Some(path) => Ok(path),
        None => default_workspace(),
    }
}

/// %LOCALAPPDATA%\\Wenyi Desktop and its per-platform equivalents.
pub fn default_workspace() -> Result<PathBuf, Error> {
    Ok(data_base()?.join(PRODUCT))
}

/// Ordered diagnostic roots: the workspace copy first, then the user-level copy.
///
/// Both candidates are kept on purpose. The fallback serves the case that needs
/// logs most: a workspace that cannot be written to. The second entry uses the
/// workspace identity hash so that several workspaces never share one log file.
pub fn log_roots(workspace: &Path) -> Vec<PathBuf> {
    let mut candidates = vec![workspace.join(DIAGNOSTICS)];
    if let Some(base) = log_base().filter(|path| path.is_absolute()) {
        let reference = format!(
            "{:x}",
            Sha256::digest(workspace.to_string_lossy().as_bytes())
        );
        candidates.push(base.join(LOGS).join(&reference[..16]));
    }
    candidates
}

/// Layer one: application runtime that may be discarded and rebuilt.
pub fn runtime_root() -> Result<PathBuf, Error> {
    if let Some(explicit) = override_path(RUNTIME_OVERRIDE) {
        return Ok(explicit);
    }
    Ok(home_dir()?.join(RUNTIME).join(ENTRY))
}

/// Layer two: user-level data that is backed up and shared across projects.
#[allow(dead_code)]
pub fn user_root() -> Result<PathBuf, Error> {
    Ok(home_dir()?.join(USER))
}

/// Layer three: the default directory that holds one folder per project.
///
/// Nothing calls this from the startup path yet; layer three arrives with its
/// own batch. The volume-root defence is already here so that no caller can
/// invent a path instead: a drive root has no parent of its own, and a bare
/// drive spelling reports an empty parent, so both are refused rather than
/// silently resolved to the volume the installation sits in.
#[allow(dead_code)]
pub fn library_root(install_dir: &Path) -> Result<PathBuf, Error> {
    let parent = install_dir.parent().ok_or(Error::InstallDirAtVolumeRoot)?;
    if parent == install_dir || parent.as_os_str().is_empty() {
        return Err(Error::InstallDirAtVolumeRoot);
    }
    Ok(parent.join(LIBRARY))
}

/// Roots must be pairwise disjoint: neither equal nor nested either way, because
/// cleaning, backing up, or uninstalling one must never reach another.
///
/// Refusing startup is the job of the batch that wires the layers together. The
/// rule stays here, and tested, so it exists in exactly one place.
#[allow(dead_code)]
pub fn check_disjoint(roots: &[(&'static str, &Path)]) -> Result<(), Error> {
    for (index, (_, path)) in roots.iter().enumerate() {
        for (_, other) in roots[index + 1..].iter() {
            if path.starts_with(other) || other.starts_with(path) {
                return Err(Error::Overlap);
            }
        }
    }
    Ok(())
}

/// Platform base the workspace and its log fallback derive from.
///
/// The chain is kept byte-for-byte as it was before this module existed, so the
/// paths an installed application uses do not move.
fn data_base() -> Result<PathBuf, Error> {
    #[cfg(windows)]
    {
        env_path("LOCALAPPDATA").ok_or(Error::Base)
    }
    #[cfg(target_os = "macos")]
    {
        let home = env_path("HOME").ok_or(Error::Base)?;
        Ok(home.join("Library/Application Support"))
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        match env_path("XDG_DATA_HOME") {
            Some(path) => Ok(path),
            None => {
                let home = env_path("HOME").ok_or(Error::Base)?;
                Ok(home.join(".local/share"))
            }
        }
    }
}

/// Platform base for the log fallback, matching the previous resolution order.
fn log_base() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        env_path("LOCALAPPDATA")
    }
    #[cfg(target_os = "macos")]
    {
        env_path("HOME").map(|path| path.join("Library/Logs"))
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        env_path("XDG_STATE_HOME")
            .or_else(|| env_path("HOME").map(|path| path.join(".local/state")))
    }
}

/// The user profile that owns layers one and two.
fn home_dir() -> Result<PathBuf, Error> {
    #[cfg(windows)]
    {
        env_path("USERPROFILE").ok_or(Error::Base)
    }
    #[cfg(not(windows))]
    {
        env_path("HOME").ok_or(Error::Base)
    }
}

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name).map(PathBuf::from)
}

/// An override is honoured only when it is a real, absolute path. A blank value
/// means "not set" so that no configuration can silently select the working
/// directory.
fn override_path(name: &str) -> Option<PathBuf> {
    let value = std::env::var_os(name)?;
    if value.to_string_lossy().trim().is_empty() {
        return None;
    }
    let path = PathBuf::from(value);
    path.is_absolute().then_some(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_workspace_name_is_spelled_once() {
        assert_eq!(
            Path::new("C:/profile").join(PRODUCT),
            Path::new("C:/profile/Wenyi Desktop")
        );
    }

    #[test]
    fn the_workspace_copy_of_the_logs_comes_first() {
        let workspace = Path::new("C:/profile/Wenyi Desktop");
        let roots = log_roots(workspace);
        assert_eq!(roots[0], workspace.join("diagnostics"));
        // The fallback depends on the platform base, so it may be absent here.
        assert!(roots.len() <= 2);
        let identity = format!(
            "{:x}",
            Sha256::digest(workspace.to_string_lossy().as_bytes())
        );
        for root in &roots[1..] {
            assert!(root.to_string_lossy().contains(LOGS));
            // One directory per workspace identity, never a shared file name.
            assert_eq!(root.file_name().unwrap().to_str().unwrap(), &identity[..16]);
        }
    }

    #[test]
    fn several_workspaces_do_not_share_one_log_directory() {
        let first = log_roots(Path::new("C:/profile/Wenyi Desktop"));
        let second = log_roots(Path::new("D:/work/Wenyi Desktop"));
        assert_eq!(first[0], Path::new("C:/profile/Wenyi Desktop/diagnostics"));
        assert_eq!(second[0], Path::new("D:/work/Wenyi Desktop/diagnostics"));
        assert_ne!(first, second);
    }

    #[test]
    fn an_installed_directory_gets_a_sibling_library_root() {
        assert_eq!(
            library_root(Path::new("D:/Apps/Wenyi")).unwrap(),
            PathBuf::from("D:/Apps/Wenyi Desktop Data")
        );
    }

    #[test]
    #[cfg(windows)]
    fn a_volume_root_is_refused_instead_of_invented() {
        for install in ["D:/", "D:"] {
            assert_eq!(
                library_root(Path::new(install)).err(),
                Some(Error::InstallDirAtVolumeRoot)
            );
        }
    }

    #[test]
    fn overlapping_roots_are_rejected() {
        for (first, second) in [
            ("D:/Apps", "D:/Apps"),
            ("D:/Apps", "D:/Apps/Wenyi"),
            ("D:/Apps/Wenyi", "D:/Apps"),
        ] {
            assert_eq!(
                check_disjoint(&[("first", Path::new(first)), ("second", Path::new(second))]).err(),
                Some(Error::Overlap)
            );
        }
        assert_eq!(
            check_disjoint(&[
                ("one", Path::new("C:/profile/.wenyi/desktop")),
                ("two", Path::new("C:/profile/Wenyi")),
                ("three", Path::new("D:/Apps/Wenyi Desktop Data")),
            ]),
            Ok(())
        );
    }
}
