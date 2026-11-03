//! Desktop-only, content-free observations. One native writer owns all log files.
//! Producers never wait for disk I/O; untrusted records are projected before queueing.
use std::{
    collections::{BTreeMap, VecDeque},
    fs::File,
    io::{BufRead, BufReader, Cursor, Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, AtomicUsize, Ordering},
        mpsc, Arc, Mutex,
    },
    time::{Duration, Instant},
};

use crate::mutex::lock;
use cap_fs_ext::{FollowSymlinks, OpenOptionsFollowExt};
use cap_std::{ambient_authority, fs::Dir};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

pub const MAX_RECORD: usize = 16 * 1024;
const QUEUE: usize = 4096;
const NORMAL_QUEUE: usize = QUEUE - 512;
const BUNDLE_BYTES: usize = 20 * 1024 * 1024;
const RETENTION_SECONDS: i64 = 7 * 86400;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Event {
    AppStarted,
    AppStopped,
    EngineSpawned,
    EngineReady,
    EngineFailed,
    EngineBootstrap,
    EngineBootstrapFailed,
    EngineStopped,
    TaskStarted,
    TaskCompleted,
    TaskFinished,
    TaskFailed,
    TaskTimeout,
    TaskCancelled,
    HttpCompleted,
    HttpMutation,
    HttpFailed,
    HttpError,
    LlmRequestStarted,
    LlmRequestCompleted,
    LlmRequestFailed,
    LlmRetryWait,
    LlmRetryExhausted,
    LlmModelFailover,
    LlmUsage,
    FrontendReady,
    FrontendError,
    FrontendRejection,
    FrontendRenderFailed,
    FrontendRequestFailed,
    /// The shared client could not read a progress frame.
    ///
    /// Distinct from a failed request: the transport worked, and what arrived
    /// was unusable. The frame is never recorded, because it can carry
    /// translated text and provider messages.
    FrontendMessageFailed,
    ExportCompleted,
    ExportFailed,
    UpdateFailed,
    CredentialsDegraded,
    /// The startup repair rebuilt part of the catalog.
    ///
    /// Counts only: how many projects came back and how many folders were not
    /// adopted. A path or a title would put book identity in the log.
    CatalogRecovered,
    LoggerDropped,
    LogsCleared,
    DetailChanged,
    PreviousUnclean,
    NativePanic,
    InstallLocationChanged,
    /// The layers and the installation directory were not pairwise disjoint.
    ///
    /// Recorded by the native side only. It is a distinct event because the
    /// remedy is different from every other failed start: nothing is broken, the
    /// folders are arranged in a way this build refuses to run in.
    LayoutRefused,
}

impl Event {
    pub fn level(self) -> &'static str {
        use Event::*;
        match self {
            NativePanic | EngineBootstrapFailed => "CRITICAL",
            EngineFailed | TaskFailed | TaskTimeout | LlmRequestFailed | LlmRetryExhausted
            | FrontendError | FrontendRejection | FrontendRenderFailed | ExportFailed
            | UpdateFailed | HttpError => "ERROR",
            HttpFailed
            | FrontendRequestFailed
            | FrontendMessageFailed
            | LlmRetryWait
            | LlmModelFailover
            | CredentialsDegraded
            | LoggerDropped
            | PreviousUnclean
            | LayoutRefused
            | InstallLocationChanged => "WARN",
            HttpCompleted | LlmRequestStarted | LlmUsage => "DEBUG",
            _ => "INFO",
        }
    }

    fn allowed(self, source: Source) -> bool {
        use Event::*;
        match source {
            Source::Frontend => matches!(
                self,
                FrontendReady
                    | FrontendError
                    | FrontendRejection
                    | FrontendRenderFailed
                    | FrontendRequestFailed
                    | FrontendMessageFailed
            ),
            Source::Engine => matches!(
                self,
                EngineBootstrap
                    | EngineBootstrapFailed
                    | EngineStopped
                    | TaskStarted
                    | TaskCompleted
                    | TaskFinished
                    | TaskFailed
                    | TaskTimeout
                    | TaskCancelled
                    | HttpCompleted
                    | HttpMutation
                    | HttpFailed
                    | HttpError
                    | LlmRequestStarted
                    | LlmRequestCompleted
                    | LlmRequestFailed
                    | LlmRetryWait
                    | LlmRetryExhausted
                    | LlmModelFailover
                    | LlmUsage
                    | CredentialsDegraded
                    | CatalogRecovered
                    | LoggerDropped
            ),
            Source::Native => matches!(
                self,
                AppStarted
                    | AppStopped
                    | EngineSpawned
                    | EngineReady
                    | EngineFailed
                    | ExportCompleted
                    | ExportFailed
                    | UpdateFailed
                    | LoggerDropped
                    | LogsCleared
                    | DetailChanged
                    | PreviousUnclean
                    | NativePanic
                    | LayoutRefused
                    | InstallLocationChanged
            ),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    Native,
    Engine,
    Frontend,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub event: Event,
    #[serde(default)]
    pub attributes: BTreeMap<String, Value>,
}

pub fn identity(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn id() -> String {
    let mut bytes = [0u8; 16];
    if getrandom::fill(&mut bytes).is_err() {
        // Logging must not panic if OS randomness fails. This is an identifier, not a key.
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let digest = Sha256::digest(format!(
            "{}:{}",
            Utc::now(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        bytes.copy_from_slice(&digest[..16]);
    }
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn attributes(input: BTreeMap<String, Value>) -> BTreeMap<String, Value> {
    input
        .into_iter()
        .filter(|(key, value)| match key.as_str() {
            "duration_ms" | "status_code" | "attempt" | "wait_ms" | "tokens" | "count" | "line" => {
                value.as_u64().is_some_and(|n| n <= (1u64 << 53) - 1)
            }
            "project_ref" | "run_ref" | "call_ref" | "request_id" | "error_id" => {
                value.as_str().is_some_and(identity)
            }
            "category" => matches!(
                value.as_str(),
                Some("timeout" | "permission" | "io" | "invalid" | "dependency" | "unknown")
            ),
            "phase" => matches!(
                value.as_str(),
                Some(
                    "language"
                        | "analysis"
                        | "synopsis"
                        | "translation"
                        | "polish"
                        | "glossary"
                        | "terminology"
                        | "annotation"
                        | "review"
                        | "autofix"
                )
            ),
            "kind" => matches!(
                value.as_str(),
                Some(
                    "parse"
                        | "prepare"
                        | "translation"
                        | "chapter_translation"
                        | "review"
                        | "export"
                )
            ),
            "module" => matches!(
                value.as_str(),
                Some(
                    "desktop"
                        | "main"
                        | "local_runtime"
                        | "tasks"
                        | "router"
                        | "retrying"
                        | "translation"
                        | "sqlite"
                )
            ),
            _ => false,
        })
        .collect()
}

#[derive(Clone, Deserialize, Serialize)]
struct Record {
    schema_version: u8,
    app_version: [u64; 3],
    timestamp: DateTime<Utc>,
    event: Event,
    component: Source,
    session_id: String,
    event_id: String,
    seq: u64,
    attributes: BTreeMap<String, Value>,
}

#[derive(Clone, Copy)]
struct Limits {
    file: u64,
    total: u64,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            file: 10 * 1024 * 1024,
            total: 200 * 1024 * 1024,
        }
    }
}

struct Store {
    root: Option<(PathBuf, Dir, File)>,
    candidates: Vec<PathBuf>,
    file: Option<cap_std::fs::File>,
    file_opened: Instant,
    current_name: String,
    bytes: u64,
    part: u32,
    session: String,
    memory: VecDeque<Record>,
    degraded: bool,
    limits: Limits,
    maintained: Instant,
    retry_at: Instant,
    previous_unclean: bool,
}

/// One entry of the log directory, with what this application is allowed to do to it.
struct DiagFile {
    name: String,
    bytes: u64,
    modified: std::time::SystemTime,
    owner: Owner,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Owner {
    /// A log file this application wrote, in the shape `owned_name` accepts.
    /// Rotation and "clear" may remove these.
    Managed,
    /// This application's own directory bookkeeping, not a log.
    ///
    /// `.owner.lock` is the lock that makes this directory single-writer and
    /// `.session-active` is the unclean-shutdown marker. Both are this
    /// application's, so neither is reported as someone else's file, and
    /// neither may be removed by retention: the lock is held while it is read.
    Bookkeeping,
    /// Anything else in the directory. Counted, never read and never removed.
    Foreign,
}

impl Owner {
    /// Whether retention or "clear" may remove the file.
    fn removable(self) -> bool {
        matches!(self, Self::Managed)
    }

    /// Whether the file is part of the diagnostic record of this application.
    fn readable(self) -> bool {
        matches!(self, Self::Managed)
    }

    fn foreign(self) -> bool {
        matches!(self, Self::Foreign)
    }
}

fn bookkeeping_name(name: &str) -> bool {
    matches!(name, ".owner.lock" | ".session-active")
}

fn owner_of(name: &str) -> Owner {
    if owned_name(name) {
        Owner::Managed
    } else if bookkeeping_name(name) {
        Owner::Bookkeeping
    } else {
        Owner::Foreign
    }
}

fn owned_name(name: &str) -> bool {
    let Some(name) = name
        .strip_prefix("wenyi-")
        .and_then(|n| n.strip_suffix(".jsonl"))
    else {
        return false;
    };
    let Some((session, part)) = name.split_once('-') else {
        return false;
    };
    identity(session) && part.len() == 6 && part.bytes().all(|b| b.is_ascii_digit())
}

fn open_root(path: &Path) -> std::io::Result<(PathBuf, Dir, File)> {
    std::fs::create_dir_all(path)?;
    if std::fs::symlink_metadata(path)?.is_symlink() {
        return Err(std::io::Error::other("Linked diagnostic directory"));
    }
    let dir = Dir::open_ambient_dir(path, ambient_authority())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        dir.set_permissions(
            ".",
            cap_std::fs::Permissions::from_std(std::fs::Permissions::from_mode(0o700)),
        )?;
    }
    let mut options = cap_std::fs::OpenOptions::new();
    options
        .read(true)
        .write(true)
        .create(true)
        .follow(FollowSymlinks::No);
    let lock = dir.open_with(".owner.lock", &options)?.into_std();
    if !lock.metadata()?.is_file() {
        return Err(std::io::Error::other("Invalid diagnostic lock"));
    }
    lock.try_lock().map_err(std::io::Error::other)?;
    Ok((path.to_path_buf(), dir, lock))
}

impl Store {
    fn new(candidates: Vec<PathBuf>, session: String, limits: Limits) -> Self {
        let root = candidates.iter().find_map(|path| open_root(path).ok());
        let previous_unclean = root
            .as_ref()
            .is_some_and(|(_, dir, _)| dir.symlink_metadata(".session-active").is_ok());
        if let Some((_, dir, _)) = &root {
            let mut options = cap_std::fs::OpenOptions::new();
            options
                .write(true)
                .create(true)
                .truncate(true)
                .follow(FollowSymlinks::No);
            #[cfg(unix)]
            {
                use cap_std::fs::OpenOptionsExt;
                options.custom_flags(libc::O_NONBLOCK);
            }
            let _ = dir.open_with(".session-active", &options);
        }
        Self {
            degraded: root.is_none(),
            root,
            candidates,
            file: None,
            file_opened: Instant::now(),
            current_name: String::new(),
            bytes: 0,
            part: 0,
            session,
            memory: VecDeque::new(),
            limits,
            maintained: Instant::now() - Duration::from_secs(60),
            retry_at: Instant::now(),
            previous_unclean,
        }
    }

    /// Every regular file in the log directory, newest last.
    ///
    /// Foreign files are listed rather than skipped. A limit that only counts
    /// this application's own output is not a limit on the directory: another
    /// program writing a large file here would leave the reported usage near
    /// zero while the volume fills, and no rotation would ever take the file
    /// away. Counting them is what makes the reported size the real one.
    ///
    /// Being listed is not permission to delete. `Owner` travels with each name
    /// so the one caller that removes files can refuse the ones it did not
    /// write; a log directory is a shared folder, not a private one.
    fn files(&self) -> std::io::Result<Vec<DiagFile>> {
        let Some((_, dir, _)) = &self.root else {
            return Ok(vec![]);
        };
        let mut files = Vec::new();
        for entry in dir.entries()? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            let meta = dir.symlink_metadata(&name)?;
            if !meta.is_file() || meta.is_symlink() {
                continue;
            }
            let owner = owner_of(&name);
            files.push(DiagFile {
                name,
                bytes: meta.len(),
                modified: meta.modified()?.into_std(),
                owner,
            });
            if files.len() > 4096 {
                return Err(std::io::Error::other("Too many diagnostic files"));
            }
        }
        files.sort_by_key(|file| file.modified);
        Ok(files)
    }

    fn maintain(&mut self, incoming: u64) -> std::io::Result<()> {
        let files = self.files()?;
        // The total counts every file, because the limit is on the directory
        // rather than on what this application happens to have written there.
        let mut total: u64 = files.iter().map(|file| file.bytes).sum();
        let now = std::time::SystemTime::now();
        let cutoff = now - Duration::from_secs(RETENTION_SECONDS as u64);
        // A file touched in the last minute is one a reader may be inside right
        // now: an export opens its inputs under this lock and reads them after
        // it is released, and removal would turn a readable report into a
        // partial one for no reason. Waiting one maintenance interval costs
        // nothing, because the next pass runs within a minute anyway.
        let settled = now - Duration::from_secs(60);
        if files
            .iter()
            .any(|file| file.name == self.current_name && file.modified < cutoff)
        {
            self.file = None;
            self.current_name.clear();
        }
        if let Some((_, dir, _)) = &self.root {
            for file in files {
                if file.name == self.current_name || file.modified > settled {
                    continue;
                }
                // Only this application's own files are removed. A foreign file
                // counts toward the total and then stays where its owner put it,
                // which is why the settings page reports it separately.
                if !file.owner.removable() {
                    continue;
                }
                if file.modified < cutoff || total + incoming > self.limits.total {
                    dir.remove_file(file.name)?;
                    total = total.saturating_sub(file.bytes);
                }
            }
        }
        if total + incoming > self.limits.total {
            return Err(std::io::Error::other("Diagnostic storage limit reached"));
        }
        self.maintained = Instant::now();
        Ok(())
    }

    fn write(&mut self, record: Record) {
        // A 256-record ring is <1 MiB because projected records are capped at 4 KiB.
        self.memory.push_back(record.clone());
        while self.memory.len() > 256 {
            self.memory.pop_front();
        }
        if Instant::now() < self.retry_at {
            return;
        }
        if self.root.is_none() {
            self.root = self.candidates.iter().find_map(|path| open_root(path).ok());
        }
        let result = self.append(&record);
        self.degraded = result.is_err();
        if self.degraded {
            self.file = None;
            self.retry_at = Instant::now() + Duration::from_secs(5);
        } else if record.event == Event::AppStopped {
            if let Some((_, dir, _)) = &self.root {
                let _ = dir.remove_file(".session-active");
            }
        }
    }

    fn append(&mut self, record: &Record) -> std::io::Result<()> {
        let bytes = serde_json::to_vec(record)?;
        if self.root.is_none() {
            return Err(std::io::Error::other("No persistent log directory"));
        }
        if self.maintained.elapsed() > Duration::from_secs(60) {
            self.maintain(bytes.len() as u64 + 1)?;
        }
        if self.file.is_none()
            || self.bytes + bytes.len() as u64 + 1 > self.limits.file
            || self.file_opened.elapsed() >= Duration::from_secs(86400)
        {
            self.file = None;
            self.current_name.clear();
            self.maintain(self.limits.file)?;
            self.part += 1;
            if self.part > 999999 {
                return Err(std::io::Error::other("Rotation limit reached"));
            }
            let name = format!("wenyi-{}-{:06}.jsonl", self.session, self.part);
            let mut options = cap_std::fs::OpenOptions::new();
            options
                .write(true)
                .create_new(true)
                .follow(FollowSymlinks::No);
            #[cfg(unix)]
            {
                use cap_std::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            self.file = Some(self.root.as_ref().unwrap().1.open_with(&name, &options)?);
            self.file_opened = Instant::now();
            self.current_name = name;
            self.bytes = 0;
        }
        let file = self.file.as_mut().unwrap();
        file.write_all(&bytes)?;
        file.write_all(b"\n")?;
        // Unbuffered writes: no fsync per event, no user-visible task waits.
        self.bytes += bytes.len() as u64 + 1;
        Ok(())
    }

    fn clear(&mut self) -> std::io::Result<()> {
        self.file = None;
        self.current_name.clear();
        if let Some((_, dir, _)) = &self.root {
            for file in self.files()? {
                // "Clear application logs" removes this application's logs.
                // Another program's files in the same folder are not ours to
                // delete, and the settings page says so.
                if file.owner.removable() {
                    dir.remove_file(file.name)?;
                }
            }
        }
        self.memory.clear();
        Ok(())
    }
}

struct Inner {
    sender: mpsc::SyncSender<Record>,
    pending: Arc<AtomicUsize>,
    dropped: AtomicU64,
    seq: AtomicU64,
    detail: Mutex<Option<Instant>>,
    store: Arc<Mutex<Store>>,
    session: String,
    last_error: Mutex<Option<String>>,
    frontend_rate: Mutex<(Instant, u32)>,
}

#[derive(Clone)]
pub struct Diagnostics(Arc<Inner>);

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    pub version: &'static str,
    pub directory: Option<String>,
    pub bytes: u64,
    /// Files in the log folder this application did not write.
    ///
    /// Reported so the size above can be reconciled with the folder a person
    /// sees in Explorer. They are counted toward the storage limit and never
    /// removed by it, so the number explains a folder that is larger than this
    /// application's own limit.
    pub foreign_files: u64,
    pub foreign_bytes: u64,
    pub degraded: bool,
    pub dropped: u64,
    pub detail_seconds: u64,
    pub last_error_id: Option<String>,
}

impl Diagnostics {
    pub fn new(path: Option<&Path>) -> Self {
        Self::with_limits(path, Limits::default())
    }

    /// The workspace copy first, then the user-level fallback that keeps logging
    /// possible when the workspace is not writable. Both candidates are resolved
    /// by paths.rs, and neither depends on layers one to three, so this can start
    /// before any layout assertion runs.
    pub fn for_workspace(workspace: &Path) -> Self {
        Self::with_paths(crate::paths::log_roots(workspace), Limits::default())
    }

    fn with_limits(path: Option<&Path>, limits: Limits) -> Self {
        Self::with_paths(path.map(Path::to_path_buf).into_iter().collect(), limits)
    }

    fn with_paths(candidates: Vec<PathBuf>, limits: Limits) -> Self {
        let session = id();
        let store = Arc::new(Mutex::new(Store::new(candidates, session.clone(), limits)));
        let previous_unclean = lock(&store).previous_unclean;
        let pending = Arc::new(AtomicUsize::new(0));
        let (sender, receiver) = mpsc::sync_channel::<Record>(QUEUE);
        let target = store.clone();
        let count = pending.clone();
        std::thread::spawn(move || loop {
            match receiver.recv_timeout(Duration::from_secs(60)) {
                Ok(record) => {
                    target
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .write(record);
                    count.fetch_sub(1, Ordering::Release);
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    let mut store = target.lock().unwrap_or_else(|e| e.into_inner());
                    if store.maintain(0).is_err() {
                        store.degraded = true;
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        });
        let diagnostics = Self(Arc::new(Inner {
            sender,
            pending,
            store,
            session,
            dropped: AtomicU64::new(0),
            seq: AtomicU64::new(0),
            detail: Mutex::new(None),
            last_error: Mutex::new(None),
            frontend_rate: Mutex::new((Instant::now(), 0)),
        }));
        if previous_unclean {
            diagnostics.record(Source::Native, Event::PreviousUnclean, json!({}));
        }
        diagnostics
    }

    pub fn record(&self, source: Source, event: Event, values: Value) -> Option<String> {
        if !event.allowed(source) {
            return None;
        }
        if event.level() == "DEBUG" && self.detail_seconds() == 0 {
            return None;
        }
        if source == Source::Frontend {
            let mut rate = self
                .0
                .frontend_rate
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            if rate.0.elapsed() >= Duration::from_secs(60) {
                *rate = (Instant::now(), 0);
            }
            if rate.1 >= 60 {
                self.0.dropped.fetch_add(1, Ordering::Relaxed);
                return None;
            }
            rate.1 += 1;
        }
        let input = values
            .as_object()
            .map(|v| v.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default();
        let mut attrs = attributes(input);
        let event_id = id();
        let error = if matches!(event.level(), "ERROR" | "CRITICAL") {
            let error = attrs
                .entry("error_id".into())
                .or_insert_with(|| json!(event_id));
            let error = error.as_str().unwrap().to_string();
            *self.0.last_error.lock().unwrap_or_else(|e| e.into_inner()) = Some(error.clone());
            Some(error)
        } else {
            None
        };
        let record = Record {
            schema_version: 1,
            app_version: {
                let version = semver::Version::parse(env!("WENYI_DESKTOP_VERSION"))
                    .unwrap_or_else(|_| semver::Version::new(0, 0, 0));
                [version.major, version.minor, version.patch]
            },
            timestamp: Utc::now(),
            event,
            component: source,
            session_id: self.0.session.clone(),
            event_id,
            seq: self.0.seq.fetch_add(1, Ordering::Relaxed),
            attributes: attrs,
        };
        let limit = if matches!(event.level(), "INFO" | "DEBUG") {
            NORMAL_QUEUE
        } else {
            QUEUE
        };
        if self.0.pending.fetch_add(1, Ordering::AcqRel) >= limit {
            self.0.pending.fetch_sub(1, Ordering::Release);
            self.0.dropped.fetch_add(1, Ordering::Relaxed);
        } else if self.0.sender.try_send(record).is_err() {
            self.0.pending.fetch_sub(1, Ordering::Release);
            self.0.dropped.fetch_add(1, Ordering::Relaxed);
        }
        error
    }

    pub fn ingest(&self, source: Source, value: &str) -> bool {
        if value.len() <= MAX_RECORD {
            if let Ok(input) = serde_json::from_str::<Input>(value) {
                if input.event.allowed(source) {
                    self.record(source, input.event, json!(input.attributes));
                    return true;
                }
            }
        }
        self.0.dropped.fetch_add(1, Ordering::Relaxed);
        false
    }

    pub fn detail_seconds(&self) -> u64 {
        self.0
            .detail
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .and_then(|until| until.checked_duration_since(Instant::now()))
            .map_or(0, |d| d.as_secs())
    }

    pub fn detail(&self, enabled: bool) {
        *self.0.detail.lock().unwrap_or_else(|e| e.into_inner()) =
            enabled.then(|| Instant::now() + Duration::from_secs(1800));
        self.record(Source::Native, Event::DetailChanged, json!({}));
    }

    pub fn drain(&self) {
        let deadline = Instant::now() + Duration::from_secs(2);
        while self.0.pending.load(Ordering::Acquire) != 0 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    pub fn status(&self) -> Status {
        let store = self.0.store.lock().unwrap_or_else(|e| e.into_inner());
        let files = store.files().unwrap_or_default();
        Status {
            version: env!("WENYI_DESKTOP_VERSION"),
            directory: store
                .root
                .as_ref()
                .map(|(path, _, _)| path.to_string_lossy().into_owned()),
            bytes: files.iter().map(|file| file.bytes).sum(),
            foreign_files: files.iter().filter(|file| file.owner.foreign()).count() as u64,
            foreign_bytes: files
                .iter()
                .filter(|file| file.owner.foreign())
                .map(|file| file.bytes)
                .sum(),
            degraded: store.degraded,
            dropped: self.0.dropped.load(Ordering::Relaxed),
            detail_seconds: self.detail_seconds(),
            last_error_id: self
                .0
                .last_error
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .clone(),
        }
    }

    pub fn clear(&self) -> Result<(), String> {
        self.drain();
        self.0
            .store
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clear()
            .map_err(|_| "Could not clear application logs.".to_string())?;
        self.record(Source::Native, Event::LogsCleared, json!({}));
        Ok(())
    }

    pub fn bundle(&self, range: Range, cancelled: impl Fn() -> bool) -> Result<Vec<u8>, String> {
        self.drain();
        let started = Instant::now();
        let now = Utc::now();
        // Open FD snapshots under the writer lock. Compression holds no store/project locks.
        let (mut inputs, memory, mut partial) = {
            let store = self.0.store.lock().unwrap_or_else(|e| e.into_inner());
            let mut inputs = Vec::new();
            let mut partial = store.degraded;
            if let Some((_, dir, _)) = &store.root {
                let mut files = match store.files() {
                    Ok(files) => files,
                    Err(_) => {
                        partial = true;
                        Vec::new()
                    }
                };
                // Only this application's own files are log records. A file that
                // merely happens to sit in the folder is not part of the report,
                // and reading it would put someone else's content in an archive
                // the user then sends to someone.
                files.retain(|file| file.owner.readable());
                files.reverse();
                for file in files {
                    let name = file.name;
                    let mut options = cap_std::fs::OpenOptions::new();
                    options.read(true).follow(FollowSymlinks::No);
                    #[cfg(unix)]
                    {
                        use cap_std::fs::OpenOptionsExt;
                        options.custom_flags(libc::O_NONBLOCK);
                    }
                    match dir.open_with(name, &options).and_then(|f| {
                        let metadata = f.metadata()?;
                        if !metadata.is_file() {
                            return Err(std::io::Error::other("Not a log file"));
                        }
                        Ok((f.into_std(), metadata.len()))
                    }) {
                        Ok(input) => inputs.push(input),
                        Err(_) => partial = true,
                    }
                }
            }
            (inputs, store.memory.clone(), partial)
        };
        let mut selected = BTreeMap::new();
        let mut bytes = 0;
        let mut omitted = 0u64;
        let mut malformed = 0u64;
        let cutoff = now
            - chrono::Duration::seconds(if range == Range::Day {
                86400
            } else {
                RETENTION_SECONDS
            });
        let mut select = |mut record: Record| {
            if record.schema_version != 1
                || !record.event.allowed(record.component)
                || !identity(&record.session_id)
                || !identity(&record.event_id)
            {
                malformed += 1;
                return;
            }
            if record.timestamp < cutoff
                || record.timestamp > now
                || (range == Range::Session && record.session_id != self.0.session)
            {
                return;
            }
            record.attributes = attributes(record.attributes);
            if selected.contains_key(&record.event_id) {
                return;
            }
            let length = serde_json::to_vec(&record).unwrap_or_default().len();
            if bytes + length > BUNDLE_BYTES {
                omitted += 1;
                return;
            }
            bytes += length;
            selected.insert(record.event_id.clone(), record);
        };
        for record in memory {
            select(record);
        }
        let mut stopped = false;
        let mut bad_lines = 0u64;
        for (file, size) in inputs.drain(..) {
            let mut reader = BufReader::new(file.take(size));
            loop {
                if cancelled() {
                    return Err("Diagnostic export canceled.".into());
                }
                if started.elapsed() > Duration::from_secs(30) {
                    stopped = true;
                    break;
                }
                let mut line = Vec::new();
                let count = match reader
                    .by_ref()
                    .take(MAX_RECORD as u64 + 1)
                    .read_until(b'\n', &mut line)
                {
                    Ok(count) => count,
                    Err(_) => {
                        partial = true;
                        bad_lines += 1;
                        break;
                    }
                };
                if count == 0 {
                    break;
                }
                if line.len() > MAX_RECORD || !line.ends_with(b"\n") {
                    partial = true;
                    bad_lines += 1;
                    // Never interpret the remainder of an oversized line as another record.
                    break;
                }
                match serde_json::from_slice::<Record>(&line) {
                    Ok(record) => select(record),
                    Err(_) => {
                        partial = true;
                        bad_lines += 1;
                    }
                }
            }
            if stopped {
                break;
            }
        }
        drop(select);
        let mut records: Vec<_> = selected.into_values().collect();
        records.sort_by_key(|r| (r.timestamp, r.seq));
        let coverage_start = records.first().map(|r| r.timestamp);
        let coverage_end = records.last().map(|r| r.timestamp);
        let mut salt = [0u8; 32];
        getrandom::fill(&mut salt).map_err(|_| "Cannot safely anonymize diagnostic identities.")?;
        let alias = |value: &str| {
            let mut hash = Sha256::new();
            hash.update(salt);
            hash.update(value.as_bytes());
            format!("{:x}", hash.finalize())[..32].to_string()
        };
        let mut sources: BTreeMap<&str, Vec<u8>> = BTreeMap::new();
        let mut errors = VecDeque::new();
        let mut producer_dropped = 0u64;
        for mut record in records {
            record.session_id = alias(&record.session_id);
            for (key, value) in &mut record.attributes {
                if matches!(key.as_str(), "project_ref" | "run_ref" | "call_ref") {
                    *value = json!(alias(value.as_str().unwrap()));
                }
            }
            if let Some(error) = record.attributes.get("error_id") {
                errors.push_back(json!({"event": record.event, "error_id": error}));
                if errors.len() > 50 {
                    errors.pop_front();
                }
            }
            if record.event == Event::LoggerDropped {
                producer_dropped = producer_dropped.saturating_add(
                    record
                        .attributes
                        .get("count")
                        .and_then(Value::as_u64)
                        .unwrap_or(0),
                );
            }
            let name = match record.component {
                Source::Native => "logs/native.jsonl",
                Source::Engine => "logs/engine.jsonl",
                Source::Frontend => "logs/frontend.jsonl",
            };
            let mut value =
                serde_json::to_value(&record).map_err(|_| "Cannot encode diagnostics.")?;
            value["level"] = json!(record.event.level());
            let stream = sources.entry(name).or_default();
            serde_json::to_writer(&mut *stream, &value)
                .map_err(|_| "Cannot encode diagnostics.")?;
            stream.push(b'\n');
        }
        let missing: Vec<_> = [
            "logs/native.jsonl",
            "logs/engine.jsonl",
            "logs/frontend.jsonl",
        ]
        .into_iter()
        .filter(|name| !sources.contains_key(name))
        .collect();
        partial |= stopped
            || omitted != 0
            || malformed != 0
            || producer_dropped != 0
            || !missing.is_empty()
            || self.0.dropped.load(Ordering::Relaxed) != 0;
        let system = json!({
            "app_version": env!("WENYI_DESKTOP_VERSION"), "os": std::env::consts::OS,
            "arch": std::env::consts::ARCH, "schema_version": 1
        });
        sources.insert("system.json", serde_json::to_vec_pretty(&system).unwrap());
        let report = format!(
            "# Wenyi Desktop diagnostic report\n\nVersion: {}\nPlatform: {} / {}\n\
            Capture: {:?}\nCompleteness: {}\n\n## Failure references\n\n{}\n\n\
            ## Please describe\n\n- Steps to reproduce:\n- Expected behavior:\n- Actual behavior:\n\n\
            No document text, prompts, credentials, raw configuration or project databases are included.\n\
            Timing and platform metadata may still identify you. Review this archive before sharing.\n\
            A timeout alone does not establish its root cause.\n",
            env!("WENYI_DESKTOP_VERSION"), std::env::consts::OS, std::env::consts::ARCH,
            range, if partial {"partial"} else {"complete"}, serde_json::to_string_pretty(&errors).unwrap()
        );
        sources.insert("report.md", report.into_bytes());
        let files: Vec<_> = sources.iter().map(|(name, data)| json!({
            "name": name, "bytes": data.len(), "sha256": format!("{:x}", Sha256::digest(data))
        })).collect();
        let manifest = json!({
            "schema_version": 1, "bundle_id": id(), "created_at": now, "range": range,
            "status": if partial {"partial"} else {"complete"}, "files": files,
            "missing_sources": missing, "omitted_records": omitted, "malformed_records": malformed + bad_lines,
            "collection_timed_out": stopped, "dropped_records": self.0.dropped.load(Ordering::Relaxed),
            "producer_dropped_records": producer_dropped,
            "coverage_start": coverage_start, "coverage_end": coverage_end,
            "retention_days": 7, "selection_bytes_limit": BUNDLE_BYTES,
            "detail_seconds": self.detail_seconds(),
            "excluded": ["document_content", "credentials", "configuration", "project_events", "usage_ledger"],
            "snapshot": "Per-file complete-line watermarks, not a project backup"
        });
        sources.insert(
            "manifest.json",
            serde_json::to_vec_pretty(&manifest).unwrap(),
        );
        let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);
        for (name, data) in sources {
            if cancelled() {
                return Err("Diagnostic export canceled.".into());
            }
            archive
                .start_file(name, options)
                .map_err(|_| "Cannot create diagnostic ZIP.")?;
            archive
                .write_all(&data)
                .map_err(|_| "Cannot write diagnostic ZIP.")?;
        }
        let bytes = archive
            .finish()
            .map_err(|_| "Cannot finish diagnostic ZIP.")?
            .into_inner();
        if bytes.len() > 25 * 1024 * 1024 {
            return Err("Diagnostic ZIP exceeds the size limit.".into());
        }
        // Read every entry before publication: this verifies the ZIP's CRCs and lengths.
        let mut verified = zip::ZipArchive::new(Cursor::new(&bytes))
            .map_err(|_| "Cannot verify diagnostic ZIP.")?;
        for index in 0..verified.len() {
            if cancelled() {
                return Err("Diagnostic export canceled.".into());
            }
            let mut entry = verified
                .by_index(index)
                .map_err(|_| "Cannot verify diagnostic ZIP.")?;
            std::io::copy(&mut entry, &mut std::io::sink())
                .map_err(|_| "Cannot verify diagnostic ZIP.")?;
        }
        Ok(bytes)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Range {
    Session,
    Day,
    Week,
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("wenyi-diagnostics-{}", id()));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn engine_and_native_share_privacy_fixtures() {
        let fixtures: Value =
            serde_json::from_str(include_str!("../diagnostic-fixtures.json")).unwrap();
        for case in fixtures.as_array().unwrap() {
            let input: BTreeMap<String, Value> =
                serde_json::from_value(case["input"].clone()).unwrap();
            assert_eq!(json!(attributes(input)), case["expected"]);
        }
    }

    #[test]
    fn full_queue_never_blocks_and_reserves_capacity_for_failures() {
        let diagnostics = Diagnostics::new(None);
        let store = lock(&diagnostics.0.store);
        for _ in 0..QUEUE {
            diagnostics.record(Source::Native, Event::AppStarted, json!({}));
        }
        assert!(diagnostics.0.pending.load(Ordering::Relaxed) <= NORMAL_QUEUE);
        diagnostics.record(Source::Native, Event::EngineFailed, json!({}));
        assert!(diagnostics.0.pending.load(Ordering::Relaxed) > NORMAL_QUEUE);
        assert!(diagnostics.0.dropped.load(Ordering::Relaxed) > 0);
        drop(store);
        diagnostics.drain();
        assert!(diagnostics.status().last_error_id.is_some());
    }

    #[test]
    fn fallback_directory_and_unclean_markers_are_independent_of_project_data() {
        let fixture = Fixture::new();
        let bad = fixture.0.join("not-a-directory");
        std::fs::write(&bad, b"keep").unwrap();
        let root = fixture.0.join("logs");
        let mut store = Store::new(vec![bad.clone(), root.clone()], id(), Limits::default());
        assert!(!store.degraded);
        assert!(!store.previous_unclean);
        let current = store.root.as_ref().unwrap().0.clone();
        assert_eq!(current, root);
        drop(store);
        store = Store::new(vec![root.clone()], id(), Limits::default());
        assert!(store.previous_unclean);
        store.write(Record {
            schema_version: 1,
            app_version: [1, 2, 3],
            timestamp: Utc::now(),
            event: Event::AppStopped,
            component: Source::Native,
            session_id: id(),
            event_id: id(),
            seq: 1,
            attributes: BTreeMap::new(),
        });
        drop(store);
        assert!(!Store::new(vec![root], id(), Limits::default()).previous_unclean);
        assert_eq!(std::fs::read(bad).unwrap(), b"keep");
    }

    #[test]
    fn retention_removes_only_expired_owned_files() {
        let fixture = Fixture::new();
        let name = format!("wenyi-{}-000001.jsonl", id());
        let path = fixture.0.join(name);
        std::fs::write(&path, b"old").unwrap();
        std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .unwrap()
            .set_times(
                std::fs::FileTimes::new()
                    .set_modified(std::time::SystemTime::now() - Duration::from_secs(8 * 86400)),
            )
            .unwrap();
        let mut store = Store::new(vec![fixture.0.clone()], id(), Limits::default());
        store.maintain(0).unwrap();
        assert!(!path.exists());
        assert!(fixture.0.join(".session-active").exists());
    }

    #[test]
    fn projection_discards_secrets_and_rejects_forged_sources() {
        let diagnostics = Diagnostics::new(None);
        assert!(diagnostics.ingest(Source::Engine, r#"{"event":"llm_retry_wait","attributes":{"status_code":429,"attempt":2,"provider":"fake-secret","request_id":"fake-secret","error":{"key":"fake-secret"}}}"#));
        assert!(!diagnostics.ingest(Source::Frontend, r#"{"event":"engine_failed"}"#));
        assert!(!diagnostics.ingest(Source::Engine, &"x".repeat(MAX_RECORD + 1)));
        diagnostics.drain();
        let memory = lock(&diagnostics.0.store).memory.clone();
        let text = serde_json::to_string(&memory).unwrap();
        assert!(!text.contains("fake-secret"));
        assert!(text.contains("429"));
    }

    #[test]
    fn debug_is_temporary_and_memory_fallback_is_bounded() {
        let diagnostics = Diagnostics::new(None);
        diagnostics.record(Source::Engine, Event::HttpCompleted, json!({}));
        diagnostics.drain();
        assert!(lock(&diagnostics.0.store).memory.is_empty());
        diagnostics.detail(true);
        assert!(diagnostics.detail_seconds() > 1700);
        for _ in 0..300 {
            diagnostics.record(Source::Engine, Event::HttpCompleted, json!({}));
        }
        diagnostics.drain();
        assert_eq!(lock(&diagnostics.0.store).memory.len(), 256);
        diagnostics.detail(false);
        assert_eq!(diagnostics.detail_seconds(), 0);
        assert!(diagnostics.status().degraded);
    }

    fn age(path: &Path, seconds: u64) {
        std::fs::OpenOptions::new()
            .write(true)
            .open(path)
            .unwrap()
            .set_times(
                std::fs::FileTimes::new()
                    .set_modified(std::time::SystemTime::now() - Duration::from_secs(seconds)),
            )
            .unwrap();
    }

    #[test]
    fn rotation_and_clear_touch_only_owned_files() {
        let fixture = Fixture::new();
        std::fs::write(fixture.0.join("private-book.txt"), "untouched").unwrap();
        let diagnostics = Diagnostics::with_limits(
            Some(&fixture.0),
            Limits {
                file: 1024,
                total: 4096,
            },
        );
        for _ in 0..100 {
            diagnostics.record(Source::Native, Event::AppStarted, json!({}));
        }
        diagnostics.drain();
        assert!(diagnostics.status().bytes <= 4096);
        assert!(diagnostics.status().bytes > 0);
        diagnostics.clear().unwrap();
        diagnostics.drain();
        assert_eq!(
            std::fs::read_to_string(fixture.0.join("private-book.txt")).unwrap(),
            "untouched"
        );
    }

    #[test]
    fn foreign_files_are_counted_and_never_removed() {
        // A log folder is an ordinary directory: another program can leave a
        // large file in it. If the size ignored such files the limit would be
        // decorative, so the reported total has to include them -- while the
        // removal below still refuses to touch what this application did not
        // write.
        let fixture = Fixture::new();
        let foreign = fixture.0.join("other-program.bin");
        std::fs::write(&foreign, vec![0u8; 8192]).unwrap();
        age(&foreign, 90);
        let diagnostics = Diagnostics::with_limits(
            Some(&fixture.0),
            Limits {
                file: 1024,
                total: 4096,
            },
        );
        let status = diagnostics.status();
        assert_eq!(status.foreign_files, 1);
        assert_eq!(status.foreign_bytes, 8192);
        assert!(status.bytes >= 8192);
        // The limit is already exceeded by a file this process may not remove,
        // so logging degrades instead of deleting someone else's data.
        for _ in 0..20 {
            diagnostics.record(Source::Native, Event::AppStarted, json!({}));
        }
        diagnostics.drain();
        assert!(foreign.exists(), "a foreign file was removed");
        assert_eq!(std::fs::metadata(&foreign).unwrap().len(), 8192);
        diagnostics.clear().unwrap();
        diagnostics.drain();
        assert!(foreign.exists(), "clearing removed a foreign file");
        assert_eq!(diagnostics.status().foreign_files, 1);
    }

    #[test]
    fn eviction_skips_a_file_opened_within_the_last_minute() {
        // Export opens its inputs under the store lock and reads them after the
        // lock is released. Removing a file that recent would turn a readable
        // report into a partial one for no gain: the next pass runs anyway.
        let fixture = Fixture::new();
        let fresh = fixture.0.join(format!("wenyi-{}-000001.jsonl", id()));
        std::fs::write(&fresh, vec![b'x'; 8192]).unwrap();
        age(&fresh, 8 * 86400);
        // Aged past retention, then touched: the touch is what has to save it.
        std::fs::OpenOptions::new()
            .write(true)
            .open(&fresh)
            .unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(std::time::SystemTime::now()))
            .unwrap();
        let mut store = Store::new(vec![fixture.0.clone()], id(), Limits::default());
        store.maintain(0).unwrap();
        assert!(fresh.exists(), "a recently opened log was evicted");
        // Without the touch it is past retention and does go.
        age(&fresh, 8 * 86400);
        store.maintain(0).unwrap();
        assert!(!fresh.exists());
    }

    #[test]
    fn archive_revalidates_records_and_works_without_an_engine() {
        let fixture = Fixture::new();
        let diagnostics = Diagnostics::new(Some(&fixture.0));
        let error = diagnostics
            .record(
                Source::Native,
                Event::EngineFailed,
                json!({"raw":"fake-secret"}),
            )
            .unwrap();
        diagnostics.drain();
        let name = format!("wenyi-{}-000001.jsonl", "a".repeat(32));
        std::fs::write(fixture.0.join(name), b"fake-secret\n").unwrap();
        let bytes = diagnostics.bundle(Range::Day, || false).unwrap();
        let mut archive = zip::ZipArchive::new(Cursor::new(bytes)).unwrap();
        let mut manifest = String::new();
        archive
            .by_name("manifest.json")
            .unwrap()
            .read_to_string(&mut manifest)
            .unwrap();
        let manifest: Value = serde_json::from_str(&manifest).unwrap();
        assert_eq!(manifest["status"], "partial");
        for file in manifest["files"].as_array().unwrap() {
            let mut data = Vec::new();
            archive
                .by_name(file["name"].as_str().unwrap())
                .unwrap()
                .read_to_end(&mut data)
                .unwrap();
            assert!(!String::from_utf8_lossy(&data).contains("fake-secret"));
            assert_eq!(file["sha256"], format!("{:x}", Sha256::digest(&data)));
        }
        let mut report = String::new();
        archive
            .by_name("report.md")
            .unwrap()
            .read_to_string(&mut report)
            .unwrap();
        assert!(report.contains(&error));
        assert!(diagnostics.bundle(Range::Day, || true).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn linked_log_directory_is_not_followed() {
        let fixture = Fixture::new();
        std::os::unix::fs::symlink(&fixture.0, fixture.0.join("linked")).unwrap();
        let diagnostics = Diagnostics::new(Some(&fixture.0.join("linked")));
        assert!(diagnostics.status().degraded);
    }
}
