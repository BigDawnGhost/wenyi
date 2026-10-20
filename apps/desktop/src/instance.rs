//! Workspace-scoped admission before creating a WebView or starting an engine.
//!
//! The OS lock is the authority, not the presence of a file. A separate discovery
//! record advertises an activation-only loopback socket; it contains no API token.
use std::{
    fs::{self, File, OpenOptions, TryLockError},
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const LOCK: &str = ".desktop-instance.lock";
const ENDPOINT: &str = ".desktop-instance.json";
const MAGIC: &[u8; 16] = b"WENYI_ACTIVATE\0\x01";
const WAKE_TIMEOUT: Duration = Duration::from_secs(3);
const SOCKET_TIMEOUT: Duration = Duration::from_millis(250);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Workspace,
    Closing,
    Unreachable,
}

impl Error {
    pub fn message(self) -> &'static str {
        match self {
            Self::Workspace => "Could not initialize Wenyi's local instance. Check that the Desktop workspace is available and writable and local loopback connections are allowed, then reopen Wenyi.",
            Self::Closing => "The Desktop workspace is still in use. If Wenyi is running, open it from the system tray. If it is closing, wait a moment and reopen it.",
            Self::Unreachable => "Wenyi is already running, but its window could not be restored. Try Show Wenyi in the system tray. If Wenyi is closing, wait a moment and reopen it.",
        }
    }
}

/// Resolve once in the native parent and pass this exact directory to Python.
pub fn workspace(explicit: Option<PathBuf>) -> Result<PathBuf, Error> {
    let path = explicit.map(Ok).unwrap_or_else(default_workspace)?;
    fs::create_dir_all(&path).map_err(|_| Error::Workspace)?;
    path.canonicalize().map_err(|_| Error::Workspace)
}

fn default_workspace() -> Result<PathBuf, Error> {
    let env = |name| {
        std::env::var_os(name)
            .map(PathBuf::from)
            .ok_or(Error::Workspace)
    };
    #[cfg(windows)]
    let base = env("LOCALAPPDATA")?;
    #[cfg(target_os = "macos")]
    let base = env("HOME")?.join("Library/Application Support");
    #[cfg(not(any(windows, target_os = "macos")))]
    let base = match std::env::var_os("XDG_DATA_HOME") {
        Some(path) => PathBuf::from(path),
        None => env("HOME")?.join(".local/share"),
    };
    Ok(base.join("Wenyi Desktop"))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Reply {
    Starting = 1,
    Activated = 2,
    Closing = 3,
    Unreachable = 4,
}

type Handler = Arc<dyn Fn() -> Reply + Send + Sync>;

#[derive(Default)]
struct Dispatch {
    pending: bool,
    handler: Option<Handler>,
}

#[derive(Clone, Default)]
pub struct Activation(Arc<Mutex<Dispatch>>);

impl Activation {
    fn request(&self) -> Reply {
        let handler = {
            let mut dispatch = self.0.lock().unwrap();
            match dispatch.handler.clone() {
                Some(handler) => handler,
                None => {
                    dispatch.pending = true;
                    return Reply::Starting;
                }
            }
        };
        handler()
    }

    /// Installing the handler and consuming startup requests must be atomic.
    pub fn ready(&self, handler: Handler) -> bool {
        let mut dispatch = self.0.lock().unwrap();
        dispatch.handler = Some(handler);
        std::mem::take(&mut dispatch.pending)
    }
}

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Endpoint {
    protocol: u8,
    port: u16,
    // A generation ID rejects stale discovery records, not a service credential.
    instance: [u8; 16],
}

pub struct Instance {
    _lock: File,
    address: SocketAddr,
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
    pub activation: Activation,
}

impl Drop for Instance {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        // Release the AppHandle captured by the handler before joining the worker.
        self.activation.0.lock().unwrap().handler = Some(Arc::new(|| Reply::Closing));
        // Wake blocking accept without an idle polling loop.
        let _ = TcpStream::connect_timeout(&self.address, SOCKET_TIMEOUT);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
        // Never unlink the lock: waiters must keep referring to the same file.
        // A stale endpoint is harmless and is replaced only by the next owner.
    }
}

/// Some means primary; None means an existing primary acknowledged activation.
pub fn enter(workspace: &Path) -> Result<Option<Instance>, Error> {
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(workspace.join(LOCK))
        .map_err(|_| Error::Workspace)?;
    let deadline = Instant::now() + WAKE_TIMEOUT;
    loop {
        match lock.try_lock() {
            Ok(()) => return start(workspace, lock).map(Some),
            Err(TryLockError::Error(_)) => return Err(Error::Workspace),
            Err(TryLockError::WouldBlock) => {}
        }
        match forward(workspace, deadline) {
            Some(Reply::Starting | Reply::Activated) => return Ok(None),
            Some(Reply::Closing) => return Err(Error::Closing),
            Some(Reply::Unreachable) => return Err(Error::Unreachable),
            None if Instant::now() >= deadline => return Err(Error::Unreachable),
            None => thread::sleep(Duration::from_millis(25)),
        }
    }
}

fn start(workspace: &Path, lock: File) -> Result<Instance, Error> {
    // An orphaned engine can outlive its native parent briefly while checkpointing.
    // Probe, but do not remove or replace, the independent Python workspace lock.
    match OpenOptions::new()
        .read(true)
        .write(true)
        .open(workspace.join(".desktop.lock"))
    {
        Ok(backend) => match backend.try_lock() {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) => return Err(Error::Closing),
            Err(TryLockError::Error(_)) => return Err(Error::Workspace),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err(Error::Workspace),
    }
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).map_err(|_| Error::Workspace)?;
    let address = listener.local_addr().map_err(|_| Error::Workspace)?;
    let mut instance = [0u8; 16];
    getrandom::fill(&mut instance).map_err(|_| Error::Workspace)?;
    let endpoint = Endpoint {
        protocol: 1,
        port: address.port(),
        instance,
    };
    // A separate record is required: Windows denies reads of an exclusively locked file.
    let generation: String = instance.iter().map(|byte| format!("{byte:02x}")).collect();
    let temporary = workspace.join(format!(".desktop-instance-{generation}.tmp"));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|_| Error::Workspace)?;
    let publish = || -> std::io::Result<()> {
        file.write_all(&serde_json::to_vec(&endpoint)?)?;
        drop(file);
        fs::rename(&temporary, workspace.join(ENDPOINT))
    };
    let published = publish();
    if published.is_err() {
        let _ = fs::remove_file(&temporary);
        return Err(Error::Workspace);
    }
    let stop = Arc::new(AtomicBool::new(false));
    let activation = Activation::default();
    let requests = activation.clone();
    let stopped = stop.clone();
    let worker = thread::Builder::new()
        .name("wenyi-activation".into())
        .spawn(move || {
            while !stopped.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        if stopped.load(Ordering::SeqCst) {
                            break;
                        }
                        let _ = respond(stream, &instance, &requests);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(_) => break,
                }
            }
        })
        .map_err(|_| Error::Workspace)?;
    Ok(Instance {
        _lock: lock,
        address,
        stop,
        worker: Some(worker),
        activation,
    })
}

fn respond(
    mut stream: TcpStream,
    instance: &[u8; 16],
    activation: &Activation,
) -> std::io::Result<()> {
    stream.set_read_timeout(Some(SOCKET_TIMEOUT))?;
    stream.set_write_timeout(Some(SOCKET_TIMEOUT))?;
    let mut request = [0u8; 32];
    stream.read_exact(&mut request)?;
    // No HTTP, arguments, paths, credentials, or commands other than activation.
    if &request[..16] == MAGIC && &request[16..] == instance {
        stream.write_all(&[activation.request() as u8])?;
    }
    Ok(())
}

fn forward(workspace: &Path, deadline: Instant) -> Option<Reply> {
    let mut record = Vec::new();
    File::open(workspace.join(ENDPOINT))
        .ok()?
        .take(256)
        .read_to_end(&mut record)
        .ok()?;
    let endpoint: Endpoint = serde_json::from_slice(&record).ok()?;
    if endpoint.protocol != 1 || endpoint.port == 0 {
        return None;
    }
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, endpoint.port));
    let remaining = deadline.checked_duration_since(Instant::now())?;
    let mut stream = TcpStream::connect_timeout(&address, remaining.min(SOCKET_TIMEOUT)).ok()?;
    stream.set_write_timeout(Some(SOCKET_TIMEOUT)).ok()?;
    stream
        .set_read_timeout(Some(deadline.checked_duration_since(Instant::now())?))
        .ok()?;
    stream.write_all(MAGIC).ok()?;
    stream.write_all(&endpoint.instance).ok()?;
    let mut reply = [0u8];
    stream.read_exact(&mut reply).ok()?;
    match reply[0] {
        1 => Some(Reply::Starting),
        2 => Some(Reply::Activated),
        3 => Some(Reply::Closing),
        4 => Some(Reply::Unreachable),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Workspace(PathBuf);

    impl Workspace {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let path = std::env::temp_dir().join(format!(
                "wenyi-instance-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::SeqCst)
            ));
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for Workspace {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.0).unwrap();
        }
    }

    #[test]
    fn second_launch_activates_original_instead_of_becoming_an_owner() {
        let workspace = Workspace::new();
        let original = enter(&workspace.0).unwrap().unwrap();
        let calls = Arc::new(AtomicUsize::new(0));
        let received = calls.clone();
        original.activation.ready(Arc::new(move || {
            received.fetch_add(1, Ordering::SeqCst);
            Reply::Activated
        }));

        assert!(enter(&workspace.0).unwrap().is_none());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn launch_during_startup_is_queued_and_consumed_once() {
        let workspace = Workspace::new();
        let original = enter(&workspace.0).unwrap().unwrap();
        for _ in 0..3 {
            assert!(enter(&workspace.0).unwrap().is_none());
        }
        assert!(original.activation.ready(Arc::new(|| Reply::Activated)));
        assert!(!original.activation.ready(Arc::new(|| Reply::Activated)));
    }

    #[test]
    fn concurrent_launches_elect_only_one_owner() {
        let workspace = Workspace::new();
        let barrier = Arc::new(std::sync::Barrier::new(8));
        let launches: Vec<_> = (0..8)
            .map(|_| {
                let path = workspace.0.clone();
                let barrier = barrier.clone();
                thread::spawn(move || {
                    barrier.wait();
                    enter(&path).unwrap()
                })
            })
            .collect();
        // Retain the winning instance while all other launches finish.
        let owners: Vec<_> = launches
            .into_iter()
            .filter_map(|launch| launch.join().unwrap())
            .collect();
        assert_eq!(owners.len(), 1);
    }

    #[test]
    fn separate_workspaces_have_independent_owners() {
        let first = Workspace::new();
        let second = Workspace::new();
        let _first_owner = enter(&first.0).unwrap().unwrap();
        let _second_owner = enter(&second.0).unwrap().unwrap();
        assert!(enter(&first.0).unwrap().is_none());
        assert!(enter(&second.0).unwrap().is_none());
    }

    #[test]
    fn path_aliases_share_the_same_owner() {
        let directory = Workspace::new();
        fs::create_dir(directory.0.join("child")).unwrap();
        let alias = directory.0.join("child/..");
        assert_eq!(
            workspace(Some(alias.clone())).unwrap(),
            workspace(Some(directory.0.clone())).unwrap()
        );
        let _owner = enter(&directory.0).unwrap().unwrap();
        assert!(enter(&alias).unwrap().is_none());
    }

    #[test]
    #[cfg(unix)]
    fn symlink_aliases_share_the_same_owner() {
        let directory = Workspace::new();
        let links = Workspace::new();
        let alias = links.0.join("alias");
        std::os::unix::fs::symlink(&directory.0, &alias).unwrap();
        let _owner = enter(&directory.0).unwrap().unwrap();
        assert!(enter(&alias).unwrap().is_none());
    }

    #[test]
    #[cfg(windows)]
    fn windows_case_aliases_share_the_same_owner() {
        let directory = Workspace::new();
        let alias = PathBuf::from(directory.0.to_string_lossy().to_uppercase());
        let _owner = enter(&directory.0).unwrap().unwrap();
        assert!(enter(&alias).unwrap().is_none());
    }

    #[test]
    fn shutdown_and_failed_restore_are_not_reported_as_success() {
        let workspace = Workspace::new();
        let original = enter(&workspace.0).unwrap().unwrap();
        for (reply, expected) in [
            (Reply::Closing, Error::Closing),
            (Reply::Unreachable, Error::Unreachable),
        ] {
            original.activation.ready(Arc::new(move || reply));
            assert_eq!(enter(&workspace.0).err(), Some(expected));
        }
    }

    #[test]
    fn dropping_owner_allows_restart_despite_stale_discovery_files() {
        let workspace = Workspace::new();
        let original = enter(&workspace.0).unwrap().unwrap();
        drop(original);
        assert!(workspace.0.join(LOCK).exists());
        assert!(workspace.0.join(ENDPOINT).exists());
        let _replacement = enter(&workspace.0).unwrap().unwrap();
        assert!(enter(&workspace.0).unwrap().is_none());
    }

    #[test]
    fn old_engine_lock_blocks_new_native_owner_without_modifying_it() {
        let workspace = Workspace::new();
        let path = workspace.0.join(".desktop.lock");
        fs::write(&path, b"\0").unwrap();
        let engine = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .unwrap();
        engine.try_lock().unwrap();
        assert_eq!(enter(&workspace.0).err(), Some(Error::Closing));
        drop(engine);
        assert_eq!(fs::read(&path).unwrap(), b"\0");
        let _replacement = enter(&workspace.0).unwrap().unwrap();
    }

    #[test]
    fn malformed_or_stale_activation_requests_cannot_activate_the_window() {
        let workspace = Workspace::new();
        let original = enter(&workspace.0).unwrap().unwrap();
        let calls = Arc::new(AtomicUsize::new(0));
        let received = calls.clone();
        original.activation.ready(Arc::new(move || {
            received.fetch_add(1, Ordering::SeqCst);
            Reply::Activated
        }));
        let endpoint: Endpoint =
            serde_json::from_slice(&fs::read(workspace.0.join(ENDPOINT)).unwrap()).unwrap();
        let mut stale = [0u8; 32];
        stale[..16].copy_from_slice(MAGIC);
        stale[16..].copy_from_slice(&endpoint.instance);
        stale[16] ^= 1;
        for request in [&b"GET / HTTP/1.1\r\nHost: localhost\r\n"[..32], &stale[..]] {
            let mut stream = TcpStream::connect((Ipv4Addr::LOCALHOST, endpoint.port)).unwrap();
            stream.set_read_timeout(Some(WAKE_TIMEOUT)).unwrap();
            stream.write_all(request).unwrap();
            let mut reply = [0u8];
            assert_eq!(stream.read(&mut reply).unwrap(), 0);
        }
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        assert!(enter(&workspace.0).unwrap().is_none());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn unavailable_owner_times_out_without_taking_its_lock() {
        let workspace = Workspace::new();
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(workspace.0.join(LOCK))
            .unwrap();
        lock.try_lock().unwrap();
        fs::write(workspace.0.join(ENDPOINT), b"invalid").unwrap();
        let started = Instant::now();
        assert_eq!(enter(&workspace.0).err(), Some(Error::Unreachable));
        assert!(started.elapsed() < WAKE_TIMEOUT + Duration::from_secs(1));
        drop(lock);
        let _replacement = enter(&workspace.0).unwrap().unwrap();
    }

    // Launch this test in another process to exercise OS-owned locks, not just Rust state.
    #[test]
    fn child_launch() {
        let Some(path) = std::env::var_os("WENYI_INSTANCE_TEST_WORKSPACE") else {
            return;
        };
        if std::env::var_os("WENYI_INSTANCE_TEST_OWNER").is_some() {
            let _owner = enter(Path::new(&path)).unwrap().unwrap();
            println!("WENYI_TEST_OWNER_READY");
            std::io::stdout().flush().unwrap();
            let _ = std::io::stdin().read(&mut [0u8]);
        } else {
            assert!(enter(Path::new(&path)).unwrap().is_none());
        }
    }

    fn await_owner(child: &mut std::process::Child) {
        use std::io::BufRead;
        let output = child.stdout.take().unwrap();
        let (tx, rx) = std::sync::mpsc::channel();
        thread::spawn(move || {
            let ready = std::io::BufReader::new(output)
                .lines()
                .any(|line| line.is_ok_and(|line| line == "WENYI_TEST_OWNER_READY"));
            let _ = tx.send(ready);
        });
        if rx.recv_timeout(Duration::from_secs(15)) != Ok(true) {
            let _ = child.kill();
            let _ = child.wait();
            panic!("child did not acquire the workspace lock");
        }
    }

    #[test]
    fn process_crash_releases_ownership_without_deleting_files() {
        let workspace = Workspace::new();
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "instance::tests::child_launch", "--nocapture"])
            .env("WENYI_INSTANCE_TEST_WORKSPACE", &workspace.0)
            .env("WENYI_INSTANCE_TEST_OWNER", "1")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        await_owner(&mut child);
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(workspace.0.join(LOCK).exists());
        assert!(workspace.0.join(ENDPOINT).exists());
        let _replacement = enter(&workspace.0).unwrap().unwrap();
        assert!(enter(&workspace.0).unwrap().is_none());
    }

    fn python() -> PathBuf {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        if cfg!(windows) {
            root.join(".venv/Scripts/python.exe")
        } else {
            root.join(".venv/bin/python")
        }
    }

    #[test]
    fn default_workspace_matches_existing_python_location() {
        let output = std::process::Command::new(python())
            .args([
                "-c",
                concat!(
                    "import json; ",
                    "from wenyi_desktop.desktop import default_workspace; ",
                    "print(json.dumps(str(default_workspace())))"
                ),
            ])
            .output()
            .unwrap();
        assert!(output.status.success());
        let existing: String = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(default_workspace().unwrap(), PathBuf::from(existing));
    }

    #[test]
    fn real_python_workspace_lock_prevents_native_admission() {
        let workspace = Workspace::new();
        // Desktop CI syncs this environment before cargo test on all platforms.
        let mut child = std::process::Command::new(python())
            .args([
                "-c",
                concat!(
                    "import sys; from pathlib import Path; ",
                    "from wenyi_desktop.local_runtime import WorkspaceLock; ",
                    "lock = WorkspaceLock(Path(sys.argv[1])); lock.acquire(); ",
                    "print('WENYI_TEST_OWNER_READY', flush=True); ",
                    "sys.stdin.read(1); lock.close()"
                ),
            ])
            .arg(&workspace.0)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        await_owner(&mut child);
        let error = enter(&workspace.0).err();
        drop(child.stdin.take());
        let status = child.wait().unwrap();
        assert_eq!(error, Some(Error::Closing));
        assert!(status.success());
        let _replacement = enter(&workspace.0).unwrap().unwrap();
    }

    #[test]
    fn independent_process_forwards_to_original() {
        let workspace = Workspace::new();
        let original = enter(&workspace.0).unwrap().unwrap();
        let calls = Arc::new(AtomicUsize::new(0));
        let received = calls.clone();
        original.activation.ready(Arc::new(move || {
            received.fetch_add(1, Ordering::SeqCst);
            Reply::Activated
        }));
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "instance::tests::child_launch", "--nocapture"])
            .env("WENYI_INSTANCE_TEST_WORKSPACE", &workspace.0)
            .env_remove("WENYI_INSTANCE_TEST_OWNER")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stdout)
        );
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }
}
