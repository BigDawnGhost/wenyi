//! Optional current-user data removal, executed by the desktop binary before
//! opening any windows or databases. No caller-supplied workspace is accepted.
use std::{fs, io, path::Path};

use crate::{paths, profile};

#[derive(Debug, PartialEq)]
enum Error {
    Admission,
    Removal,
}

#[cfg(windows)]
pub fn run(selection: &std::ffi::OsStr) -> i32 {
    let (settings, projects) = match selection.to_str() {
        Some("settings") => (true, false),
        Some("projects") => (false, true),
        Some("both") => (true, true),
        _ => return 2,
    };
    let Some(local) = paths::local_app_data() else {
        return 3;
    };
    match cleanup(&local.join("Wenyi/Desktop"), settings, projects) {
        Ok(()) => 0,
        Err(Error::Admission) => 3,
        Err(Error::Removal) => 4,
    }
}

fn refuse_link(path: &Path) -> Result<(), Error> {
    match fs::symlink_metadata(path) {
        Ok(meta) if !profile::linked(&meta) => Ok(()),
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        _ => Err(Error::Admission),
    }
}

fn cleanup(root: &Path, settings: bool, projects: bool) -> Result<(), Error> {
    let locks = paths::locks(root);
    for parent in locks.ancestors() {
        refuse_link(parent)?;
    }
    if !root.try_exists().map_err(|_| Error::Admission)? {
        return Ok(());
    }
    for name in [".desktop-profile.lock", ".desktop-profile-admission.lock"] {
        refuse_link(&locks.join(name))?;
    }
    let profile = profile::Profile::enter(&locks).map_err(|_| Error::Admission)?;
    let _exclusive = profile.exclusive(None).map_err(|_| Error::Admission)?;

    // Leave the lock namespace intact, including both locks held above. Unlinking
    // it would let another installation acquire a new lock on a different file.
    if settings {
        for name in [
            "catalog.sqlite3",
            "catalog.sqlite3-wal",
            "catalog.sqlite3-shm",
        ] {
            remove_if_present(fs::remove_file(root.join(name)))?;
        }
        for name in ["logs", "runtime"] {
            remove_if_present(fs::remove_dir_all(root.join(name)))?;
        }
    }
    if projects {
        for name in ["projects", "backups"] {
            // std::fs::remove_dir_all does not traverse directory symlinks.
            remove_if_present(fs::remove_dir_all(root.join(name)))?;
        }
    }
    Ok(())
}

fn remove_if_present(result: io::Result<()>) -> Result<(), Error> {
    match result {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(Error::Removal),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn nested_directory_links_are_removed_without_traversing_the_target() {
        let base =
            std::env::temp_dir().join(format!("wenyi-uninstall-links-{}", std::process::id()));
        let root = base.join("data");
        fs::create_dir_all(root.join("backups")).unwrap();
        let outside = base.join("user-files");
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("book"), b"saved").unwrap();
        std::os::unix::fs::symlink(&outside, root.join("backups/alias")).unwrap();
        cleanup(&root, false, true).unwrap();
        assert_eq!(fs::read(outside.join("book")).unwrap(), b"saved");
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn another_copy_blocks_all_deletion_then_only_selected_data_is_removed() {
        let root = std::env::temp_dir().join(format!("wenyi-uninstall-{}", std::process::id()));
        fs::create_dir_all(root.join("projects")).unwrap();
        fs::write(root.join("projects/book"), b"saved").unwrap();
        fs::write(root.join("catalog.sqlite3"), b"settings").unwrap();
        let other = profile::Profile::enter(&paths::locks(&root)).unwrap();
        assert_eq!(cleanup(&root, true, true), Err(Error::Admission));
        assert_eq!(fs::read(root.join("projects/book")).unwrap(), b"saved");
        assert_eq!(fs::read(root.join("catalog.sqlite3")).unwrap(), b"settings");
        drop(other);
        cleanup(&root, false, true).unwrap();
        assert!(!root.join("projects").exists());
        assert!(root.join("catalog.sqlite3").exists());
        cleanup(&root, true, false).unwrap();
        assert!(!root.join("catalog.sqlite3").exists());
        assert!(paths::locks(&root).join(".desktop-profile.lock").is_file());
        fs::remove_dir_all(root).unwrap();
    }
}
