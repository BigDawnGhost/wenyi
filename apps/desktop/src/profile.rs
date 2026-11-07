//! Shared engine admission and an exclusive upgrade lease over user-level data.
use std::{
    fs::{self, File, OpenOptions, TryLockError},
    path::{Path, PathBuf},
    sync::{Mutex, MutexGuard},
};

use crate::mutex::lock;

const WRITERS: &str = ".desktop-writers";

pub struct Profile {
    directory: PathBuf,
    file: Mutex<File>,
}

struct Gate(File);

impl Gate {
    fn acquire(directory: &Path) -> Result<Self, String> {
        let file = open(&directory.join(".desktop-profile-admission.lock"))?;
        file.try_lock()
            .map_err(|_| "This Desktop profile is preparing an update; retry after it ends.")?;
        Ok(Self(file))
    }
}

impl Drop for Gate {
    fn drop(&mut self) {
        // A concurrently forked child must not extend the caller's admission.
        let _ = self.0.unlock();
    }
}

fn open(path: &Path) -> Result<File, String> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .map_err(|_| "Could not open Desktop profile admission.".into())
}

fn linked(metadata: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        metadata.is_symlink()
    }
}

fn writer_name(name: &str) -> bool {
    let Some((generation, pid)) = name
        .strip_suffix(".lock")
        .and_then(|name| name.split_once('-'))
    else {
        return false;
    };
    generation.len() == 32
        && generation
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        && !pid.starts_with('0')
        && pid.bytes().all(|byte| byte.is_ascii_digit())
        && pid.parse::<u32>().is_ok_and(|pid| pid > 0)
}

/// Called only behind the admission gate. Never follow a registry entry or use
/// file existence as evidence that a writer has stopped.
fn writers(directory: &Path, owned: Option<&str>, refuse_live: bool) -> Result<(), String> {
    let root = directory.join(WRITERS);
    let metadata = match fs::symlink_metadata(&root) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err("Could not inspect Desktop writer leases.".into()),
    };
    if linked(&metadata) || !metadata.is_dir() {
        return Err("The Desktop writer lease directory is unsafe.".into());
    }
    for entry in fs::read_dir(&root).map_err(|_| "Could not inspect Desktop writer leases.")? {
        let entry = entry.map_err(|_| "Could not inspect Desktop writer leases.")?;
        let name = entry.file_name();
        let name = name
            .to_str()
            .ok_or("The Desktop writer lease name is invalid.")?;
        if !writer_name(name) {
            return Err("The Desktop writer lease name is invalid.".into());
        }
        let mut options = OpenOptions::new();
        options.read(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NOFOLLOW);
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            options.custom_flags(0x00200000); // FILE_FLAG_OPEN_REPARSE_POINT
        }
        let file = options
            .open(entry.path())
            .map_err(|_| "Could not inspect Desktop writer lease.")?;
        let metadata = file
            .metadata()
            .map_err(|_| "Could not inspect Desktop writer lease.")?;
        if !metadata.is_file() || linked(&metadata) {
            return Err("The Desktop writer lease is unsafe.".into());
        }
        if owned == Some(name) {
            continue;
        }
        match file.try_lock() {
            Ok(()) => {
                file.unlock()
                    .map_err(|_| "Could not release stale writer lease.")?;
                drop(file);
                fs::remove_file(entry.path()).map_err(|_| "Could not prune stale writer lease.")?;
            }
            Err(TryLockError::WouldBlock) if !refuse_live => {}
            Err(TryLockError::WouldBlock) => {
                return Err(
                    "A Desktop engine is still writing; close it and retry the update.".into(),
                );
            }
            Err(TryLockError::Error(_)) => {
                return Err("Could not inspect Desktop writer lease.".into())
            }
        }
    }
    Ok(())
}

impl Profile {
    pub fn enter(directory: &Path) -> Result<Self, String> {
        fs::create_dir_all(directory).map_err(|_| "Could not open Desktop profile storage.")?;
        let _gate = Gate::acquire(directory)?;
        let file = open(&directory.join(".desktop-profile.lock"))?;
        file.try_lock_shared()
            .map_err(|_| "This Desktop profile is preparing an update; retry after it ends.")?;
        writers(directory, None, false)?;
        Ok(Self {
            directory: directory.into(),
            file: Mutex::new(file),
        })
    }

    pub fn exclusive(&self, owned_writer: Option<&str>) -> Result<Upgrade<'_>, String> {
        let gate = Gate::acquire(&self.directory)?;
        let file = lock(&self.file);
        file.unlock()
            .map_err(|_| "Could not prepare Desktop profile admission.")?;
        if file.try_lock().is_err() {
            // The gate excludes new startups until our shared lease is restored.
            file.lock_shared()
                .map_err(|_| "Could not restore Desktop profile admission.")?;
            return Err("Close the other Desktop instances before installing this update.".into());
        }
        let upgrade = Upgrade {
            file,
            _gate: gate,
            directory: &self.directory,
        };
        writers(&self.directory, owned_writer, true)?;
        Ok(upgrade)
    }
}

impl Drop for Profile {
    fn drop(&mut self) {
        // Release explicitly: a concurrent fork may temporarily inherit the open
        // file description until exec, even though the descriptor is CLOEXEC.
        let _ = lock(&self.file).unlock();
    }
}

pub struct Upgrade<'a> {
    file: MutexGuard<'a, File>,
    _gate: Gate,
    directory: &'a Path,
}

impl Upgrade<'_> {
    pub fn quiescent(&self) -> Result<(), String> {
        writers(self.directory, None, true)
    }
}

impl Drop for Upgrade<'_> {
    fn drop(&mut self) {
        // Keep the admission gate until the ordinary engine lease is restored.
        // Windows does not convert an already-held exclusive lock to shared.
        self.file.unlock().expect("Could not release upgrade lease");
        self.file
            .lock_shared()
            .expect("Could not restore profile lease");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn other_instances_block_upgrade_but_normal_workspaces_can_coexist() {
        let directory = std::env::temp_dir().join(format!("wenyi-profile-{}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        let first = Profile::enter(&directory).unwrap();
        let other = Profile::enter(&directory).unwrap();
        assert!(first.exclusive(None).is_err());
        drop(other);
        {
            let _upgrade = first.exclusive(None).unwrap();
            assert!(Profile::enter(&directory).is_err());
        }
        let other = Profile::enter(&directory).unwrap();
        assert!(first.exclusive(None).is_err());
        drop(other);
        drop(first);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn writer_registry_prunes_only_unlocked_valid_files_and_requires_quiescence() {
        let directory = std::env::temp_dir().join(format!("wenyi-writers-{}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        let profile = Profile::enter(&directory).unwrap();
        let writers = directory.join(WRITERS);
        fs::create_dir_all(&writers).unwrap();
        let live_name = format!("{}-1.lock", "a".repeat(32));
        let stale = writers.join(format!("{}-2.lock", "b".repeat(32)));
        let live = open(&writers.join(&live_name)).unwrap();
        live.try_lock().unwrap();
        fs::write(&stale, []).unwrap();
        drop(Profile::enter(&directory).unwrap());
        assert!(!stale.exists());
        assert!(writers.join(&live_name).exists());
        assert!(profile.exclusive(None).is_err());
        let update = profile.exclusive(Some(&live_name)).unwrap();
        assert!(update.quiescent().is_err());
        live.unlock().unwrap();
        drop(live);
        update.quiescent().unwrap();
        assert!(!writers.join(&live_name).exists());
        drop(update);
        fs::write(writers.join("unexpected.json"), b"do not delete").unwrap();
        assert!(Profile::enter(&directory).is_err());
        assert!(profile.exclusive(None).is_err());
        assert_eq!(
            fs::read(writers.join("unexpected.json")).unwrap(),
            b"do not delete"
        );
        drop(profile);
        fs::remove_dir_all(directory).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn writer_registry_refuses_links_without_touching_their_targets() {
        let directory =
            std::env::temp_dir().join(format!("wenyi-writer-links-{}", std::process::id()));
        fs::create_dir_all(directory.join(WRITERS)).unwrap();
        let target = directory.join("private");
        fs::write(&target, b"untouched").unwrap();
        let link = directory
            .join(WRITERS)
            .join(format!("{}-1.lock", "a".repeat(32)));
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(Profile::enter(&directory).is_err());
        assert_eq!(fs::read(&target).unwrap(), b"untouched");
        fs::remove_file(link).unwrap();
        fs::remove_dir(directory.join(WRITERS)).unwrap();
        std::os::unix::fs::symlink(&directory, directory.join(WRITERS)).unwrap();
        assert!(Profile::enter(&directory).is_err());
        fs::remove_dir_all(directory).unwrap();
    }

    fn data_locks(workspace: &Path) -> PathBuf {
        crate::paths::locks(workspace)
    }

    #[test]
    fn python_and_native_share_profile_leases_and_upgrade_admission() {
        use std::{
            io::{BufRead, BufReader, Write},
            process::{Command, Stdio},
        };
        let root =
            std::env::temp_dir().join(format!("wenyi-profile-python-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let repository = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let python = repository.join(if cfg!(windows) {
            ".venv/Scripts/python.exe"
        } else {
            ".venv/bin/python"
        });
        let mut child = Command::new(&python)
            .args([
                "-c",
                r#"
import sys
from pathlib import Path
from wenyi_desktop.ownership import profile_lock
with profile_lock(Path(sys.argv[1]), None):
    print("ready", flush=True)
    sys.stdin.readline()
"#,
            ])
            .arg(root.join("workspace"))
            .env("HOME", &root)
            .env("USERPROFILE", &root)
            .env("XDG_DATA_HOME", root.join("data"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut line = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut line)
            .unwrap();
        assert_eq!(line.trim(), "ready");
        let native = Profile::enter(&data_locks(&root.join("workspace"))).unwrap();
        assert!(native.exclusive(None).is_err());
        writeln!(child.stdin.as_mut().unwrap()).unwrap();
        assert!(child.wait().unwrap().success());
        let attempt = || {
            Command::new(&python)
                .args([
                    "-c",
                    r#"
import sys
from pathlib import Path
from wenyi_desktop.ownership import profile_lock
try:
    with profile_lock(Path(sys.argv[1]), None):
        sys.exit(0)
except RuntimeError:
    sys.exit(9)
"#,
                ])
                .arg(root.join("workspace"))
                .env("HOME", &root)
                .env("USERPROFILE", &root)
                .env("XDG_DATA_HOME", root.join("data"))
                .status()
                .unwrap()
                .code()
                .unwrap()
        };
        {
            let _upgrade = native.exclusive(None).unwrap();
            assert_eq!(attempt(), 9);
        }
        // Both cancellation and failed installation drop this same guard.
        assert_eq!(attempt(), 0);
        drop(native);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn native_parent_with_live_writer() {
        use std::io::{BufRead, BufReader, Read, Write};
        use std::process::{Command, Stdio};
        let Some(root) = std::env::var_os("WENYI_ORPHAN_TEST_ROOT") else {
            return;
        };
        let root = PathBuf::from(root);
        let home = root.join("profile");
        let workspace = root.join("workspace");
        let owner = crate::instance::enter(&workspace).unwrap().unwrap();
        let profile = Profile::enter(&data_locks(&workspace)).unwrap();
        let python = Path::new(env!("CARGO_MANIFEST_DIR")).join(if cfg!(windows) {
            "../../.venv/Scripts/python.exe"
        } else {
            "../../.venv/bin/python"
        });
        #[cfg(windows)]
        let mut command = crate::process::windows_python_command(&python).unwrap();
        #[cfg(not(windows))]
        let mut command = Command::new(python);
        let mut child = command
            .args([
                "-c",
                r#"
import json, socket, sqlite3, sys
from contextlib import closing
from pathlib import Path
from wenyi_desktop import paths
from wenyi_desktop.ownership import NativeOwner, profile_lock
workspace = Path(sys.argv[1])
owner = NativeOwner.read(sys.stdin, workspace)
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    with profile_lock(workspace, owner):
        # This is deliberately before the runtime/workspace lock exists.
        assert not paths.instance_lock(workspace).exists()
        with closing(sqlite3.connect(workspace / "orphan-write.sqlite3")) as db:
            db.execute("CREATE TABLE writes(value INTEGER)")
            db.execute("INSERT INTO writes VALUES(1)")
            db.commit()
            print(json.dumps({"port": listener.getsockname()[1]}), flush=True)
            assert sys.stdin.readline() == ""  # Parent death or normal shutdown closes the pipe.
            control, _ = listener.accept()
            with control:
                assert control.recv(1) == b"w"
                db.execute("INSERT INTO writes VALUES(2)")
                db.commit()
                control.sendall(b"w")
                assert control.recv(1) == b"q"
                # Closing all writers and releasing the lease precedes the reply.
                completed = control.dup()
    with completed:
        completed.sendall(b"x")
"#,
            ])
            .arg(&workspace)
            .env("HOME", &home)
            .env("USERPROFILE", &home)
            .env("XDG_DATA_HOME", home.join("data"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        writeln!(
            child.stdin.as_mut().unwrap(),
            "{}",
            serde_json::to_string(&owner.owner).unwrap()
        )
        .unwrap();
        let mut ready = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut ready)
            .unwrap();
        assert!(!ready.is_empty());
        // The owner's own update remains possible while its verified child lives.
        let writer = owner.owner.writer_name(child.id());
        let own_update = profile.exclusive(Some(&writer)).unwrap();
        assert!(own_update.quiescent().is_err());
        drop(own_update);
        println!("WENYI_WRITER_READY {}", ready.trim());
        std::io::stdout().flush().unwrap();
        let mut request = [0u8];
        std::io::stdin().read_exact(&mut request).unwrap();
        assert_eq!(&request, b"q");
        let update = profile.exclusive(Some(&writer)).unwrap();
        drop(child.stdin.take());
        let ready: serde_json::Value = serde_json::from_str(&ready).unwrap();
        let mut control =
            std::net::TcpStream::connect(("127.0.0.1", ready["port"].as_u64().unwrap() as u16))
                .unwrap();
        control
            .set_read_timeout(Some(std::time::Duration::from_secs(5)))
            .unwrap();
        control.write_all(b"w").unwrap();
        control.read_exact(&mut request).unwrap();
        assert_eq!(&request, b"w");
        assert!(update.quiescent().is_err());
        control.write_all(b"q").unwrap();
        control.read_exact(&mut request).unwrap();
        assert_eq!(&request, b"x");
        assert!(child.wait().unwrap().success());
        update.quiescent().unwrap();
        drop(update);
        drop(Profile::enter(&data_locks(&workspace)).unwrap());
    }

    #[test]
    fn normal_native_update_reaps_its_writer_before_snapshot_admission() {
        use std::io::Write;
        use std::process::{Command, Stdio};
        let root = std::env::temp_dir().join(format!("wenyi-normal-update-{}", std::process::id()));
        fs::create_dir_all(root.join("workspace")).unwrap();
        fs::create_dir_all(root.join("profile")).unwrap();
        let mut parent = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "profile::tests::native_parent_with_live_writer",
                "--nocapture",
            ])
            .env("WENYI_ORPHAN_TEST_ROOT", &root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        parent.stdin.as_mut().unwrap().write_all(b"q").unwrap();
        let output = parent.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stdout)
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn dead_native_parent_cannot_leave_an_unleased_python_writer() {
        use std::io::{BufRead, BufReader, Read, Write};
        use std::net::TcpStream;
        use std::process::{Command, Stdio};
        use std::time::Duration;
        let root = std::env::temp_dir().join(format!("wenyi-orphan-{}", std::process::id()));
        fs::create_dir_all(root.join("workspace")).unwrap();
        fs::create_dir_all(root.join("profile")).unwrap();
        let mut parent = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "profile::tests::native_parent_with_live_writer",
                "--nocapture",
            ])
            .env("WENYI_ORPHAN_TEST_ROOT", &root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let output = parent.stdout.take().unwrap();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(output)
                .lines()
                .map_while(std::result::Result::ok)
            {
                if let Some(ready) = line.strip_prefix("WENYI_WRITER_READY ") {
                    let _ = tx.send(ready.to_owned());
                    break;
                }
            }
        });
        let ready = rx.recv_timeout(Duration::from_secs(15));
        parent.kill().unwrap();
        parent.wait().unwrap();
        let ready: serde_json::Value = serde_json::from_str(&ready.unwrap()).unwrap();
        let mut control =
            TcpStream::connect(("127.0.0.1", ready["port"].as_u64().unwrap() as u16)).unwrap();
        control
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        control
            .set_write_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        control.write_all(b"w").unwrap();
        let mut reply = [0u8];
        control.read_exact(&mut reply).unwrap();
        assert_eq!(&reply, b"w");
        let other = Profile::enter(&data_locks(&root.join("workspace"))).unwrap();
        let refused = other.exclusive(None).is_err();
        control.write_all(b"q").unwrap();
        control.read_exact(&mut reply).unwrap();
        assert_eq!(&reply, b"x");
        drop(other.exclusive(None).unwrap());
        drop(other);
        fs::remove_dir_all(root).unwrap();
        assert!(
            refused,
            "another updater admitted a still-writing orphan engine"
        );
    }
}
