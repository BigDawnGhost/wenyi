//! Native UI preference, scoped to the selected Desktop data root.

use std::{
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
};

use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt};
use cap_std::fs::{Dir, OpenOptions};
use serde::{Deserialize, Serialize};

const FILE: &str = "notifications.json";

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Document {
    enabled: bool,
}

pub(super) struct Preferences {
    root: Option<PathBuf>,
    pub enabled: Arc<AtomicBool>,
    writing: Mutex<()>,
}

impl Preferences {
    pub fn new(root: &Path) -> Self {
        let value = Self {
            root: Some(root.to_path_buf()),
            enabled: Arc::new(AtomicBool::new(false)),
            writing: Mutex::new(()),
        };
        // An unreadable saved preference must not unexpectedly re-enable alerts.
        let _ = value.read();
        value
    }

    #[cfg(test)]
    pub fn memory() -> Self {
        Self {
            root: None,
            enabled: Arc::new(AtomicBool::new(true)),
            writing: Mutex::new(()),
        }
    }

    pub fn read(&self) -> io::Result<bool> {
        let _guard = self.writing.lock().unwrap();
        let Some(root) = &self.root else {
            return Ok(self.enabled.load(Ordering::SeqCst));
        };
        let enabled = match read(root) {
            Ok(enabled) => enabled,
            Err(error) if error.kind() == io::ErrorKind::NotFound => true,
            Err(error) => {
                self.enabled.store(false, Ordering::SeqCst);
                return Err(error);
            }
        };
        self.enabled.store(enabled, Ordering::SeqCst);
        Ok(enabled)
    }

    pub fn save(&self, enabled: bool) -> io::Result<bool> {
        let _guard = self.writing.lock().unwrap();
        if let Some(root) = &self.root {
            let root = Dir::open_ambient_dir(root, cap_std::ambient_authority())?;
            root.create_dir_all("runtime")?;
            let directory = root.open_dir_nofollow("runtime")?;
            let mut random = [0; 16];
            getrandom::fill(&mut random).map_err(io::Error::other)?;
            let suffix: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
            let temporary = format!(".notifications-{suffix}.tmp");
            let mut options = OpenOptions::new();
            options
                .write(true)
                .create_new(true)
                .follow(FollowSymlinks::No);
            let mut file = directory.open_with(&temporary, &options)?;
            let result = (|| {
                file.write_all(&serde_json::to_vec(&Document { enabled })?)?;
                file.sync_all()?;
                drop(file);
                directory.rename(&temporary, &directory, FILE)
            })();
            if let Err(error) = result {
                let _ = directory.remove_file(&temporary);
                return Err(error);
            }
        }
        // This cannot be lost behind a full notification queue.
        self.enabled.store(enabled, Ordering::SeqCst);
        Ok(enabled)
    }
}

fn read(root: &Path) -> io::Result<bool> {
    let root = Dir::open_ambient_dir(root, cap_std::ambient_authority())?;
    let directory = root.open_dir_nofollow("runtime")?;
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    let file = directory.open_with(FILE, &options)?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::other("Invalid notification preference file"));
    }
    let mut bytes = Vec::new();
    file.take(1025).read_to_end(&mut bytes)?;
    if bytes.len() > 1024 {
        return Err(io::Error::other("Invalid notification preference size"));
    }
    Ok(serde_json::from_slice::<Document>(&bytes)?.enabled)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let mut random = [0; 16];
            getrandom::fill(&mut random).unwrap();
            let suffix: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
            let path = std::env::temp_dir().join(format!("wenyi-notifications-{suffix}"));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn default_and_saved_preference_survive_restart_with_separate_data_roots() {
        let first = Fixture::new();
        let second = Fixture::new();
        let preferences = Preferences::new(&first.0);
        assert!(preferences.read().unwrap());
        assert!(preferences.enabled.load(Ordering::SeqCst));
        preferences.save(false).unwrap();
        assert!(!Preferences::new(&first.0).read().unwrap());
        assert!(Preferences::new(&second.0).read().unwrap());
        preferences.save(true).unwrap();
        assert!(Preferences::new(&first.0).read().unwrap());
        assert_eq!(fs::read_dir(first.0.join("runtime")).unwrap().count(), 1);
    }

    #[test]
    fn invalid_or_unwritable_preferences_never_claim_a_successful_change() {
        let fixture = Fixture::new();
        let preferences = Preferences::new(&fixture.0);
        preferences.save(false).unwrap();
        fs::write(fixture.0.join("runtime").join(FILE), b"invalid").unwrap();
        assert!(preferences.read().is_err());
        assert!(!preferences.enabled.load(Ordering::SeqCst));
        fs::remove_file(fixture.0.join("runtime").join(FILE)).unwrap();
        fs::create_dir(fixture.0.join("runtime").join(FILE)).unwrap();
        assert!(preferences.save(true).is_err());
        assert!(!preferences.enabled.load(Ordering::SeqCst));
        assert_eq!(fs::read_dir(fixture.0.join("runtime")).unwrap().count(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn linked_preferences_are_not_read_or_written_through() {
        let fixture = Fixture::new();
        let outside = Fixture::new();
        fs::create_dir(fixture.0.join("runtime")).unwrap();
        let foreign = outside.0.join("private.json");
        fs::write(&foreign, b"private").unwrap();
        std::os::unix::fs::symlink(&foreign, fixture.0.join("runtime").join(FILE)).unwrap();
        let preferences = Preferences::new(&fixture.0);
        assert!(preferences.read().is_err());
        assert!(!preferences.enabled.load(Ordering::SeqCst));
        preferences.save(false).unwrap();
        assert_eq!(fs::read(&foreign).unwrap(), b"private");
        fs::rename(fixture.0.join("runtime"), fixture.0.join("original")).unwrap();
        std::os::unix::fs::symlink(&outside.0, fixture.0.join("runtime")).unwrap();
        assert!(preferences.read().is_err());
        assert!(preferences.save(true).is_err());
        assert_eq!(fs::read_dir(&outside.0).unwrap().count(), 1);
    }
}
