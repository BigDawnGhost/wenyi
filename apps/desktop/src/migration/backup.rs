//! Copy the data directory somewhere else and prove the copy is complete.
//!
//! The copy is deliberately dumb: bytes in, bytes out, plus a manifest of what
//! was written. It runs while the engine is stopped, so there is no writer to
//! race and no need to understand SQLite. Two consequences are load-bearing:
//!
//! - A database whose shutdown timed out can still hold committed rows in a
//!   write-ahead log file next to it. Those rows live in that file, not in the
//!   database file, so the log is copied and listed whenever it exists. Copying
//!   only the database would silently produce a backup that opens and is missing
//!   data, which is worse than having no backup at all.
//! - The source is only ever read. Nothing here creates, renames, or removes a
//!   file under it, because a failed upgrade is only survivable if the old
//!   workspace is exactly as the old version left it.
//!
//! What is left out is rebuilt rather than preserved: the BabelDOC plugin, which
//! is downloadable again, and the diagnostics folder, which holds logs about the
//! version the user is leaving.

use std::{
    collections::BTreeSet,
    fs::{self, File},
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::Error;

const MANIFEST: &str = "MANIFEST.json";
const SCHEMA_VERSION: u32 = 1;
const MULTI_SOURCE_SCHEMA_VERSION: u32 = 2;
/// Headroom over the source size. A copy is not the only thing the update does
/// to the same volume, and a backup that stops halfway is worthless.
const HEADROOM: f64 = 1.1;
/// Suffix of runtime lock files, which no restored copy should inherit.
const EXCLUDED_SUFFIX: &str = ".lock";

/// One file that was copied, keyed by its path relative to the source root.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Entry {
    pub path: String,
    pub size: u64,
    pub sha256: String,
}

/// What a completed snapshot knows about itself.
#[derive(Clone, Debug)]
pub struct Snapshot {
    /// Directory below the target that holds the copy and its manifest.
    pub root: PathBuf,
    /// Sorted by path, so two runs over the same source agree exactly.
    pub files: Vec<Entry>,
    pub sources: Vec<BackupSource>,
}

impl Snapshot {
    /// Read back a snapshot that a previous process wrote.
    ///
    /// The backup and the verification are not in the same process: the update
    /// path writes the snapshot just before the installer starts, and the first
    /// launch of the new version is what needs to trust it. Without this there
    /// would be no way to check a backup this run did not create.
    ///
    /// Unused in this build for that reason; the caller arrives with the first
    /// launch that has to adopt an upgrade's backup.
    #[allow(dead_code)]
    pub fn load(root: &Path) -> Result<Self, Error> {
        let manifest = read(&root.join(MANIFEST))?;
        if !valid_schema(&manifest) {
            return Err(mismatch(MANIFEST));
        }
        Ok(Self {
            root: root.to_path_buf(),
            files: manifest.files,
            sources: manifest.sources,
        })
    }
}

#[derive(Debug, Deserialize, Serialize)]
struct Manifest {
    schema_version: u32,
    source_path: String,
    created_at: String,
    version: String,
    files: Vec<Entry>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    sources: Vec<BackupSource>,
}

fn valid_schema(manifest: &Manifest) -> bool {
    manifest.schema_version
        == if manifest.sources.is_empty() {
            SCHEMA_VERSION
        } else {
            MULTI_SOURCE_SCHEMA_VERSION
        }
}

/// A narrowly owned source and its restore namespace, supplied by the frozen engine.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BackupSource {
    pub name: String,
    pub path: PathBuf,
}

/// Copy only the authoritative sources, not arbitrary chosen parent directories.
/// Source mappings in the manifest make a multi-root backup restorable.
pub fn snapshot_sources(
    sources: &[BackupSource],
    target: &Path,
    version: &str,
) -> Result<Snapshot, Error> {
    if sources.is_empty() {
        return Err(Error::Unsupported("empty backup plan".into()));
    }
    let mut planned = Vec::new();
    let mut names = BTreeSet::new();
    let mut bytes = 0u64;
    for source in sources {
        let namespace = Path::new(&source.name);
        if !source.path.is_absolute()
            || source.name.contains('\\')
            || source.name.is_empty()
            || !namespace
                .components()
                .all(|part| matches!(part, Component::Normal(_)))
            || namespace == Path::new(MANIFEST)
        {
            return Err(Error::Unsupported(source.name.clone()));
        }
        crate::paths::check_disjoint(&[("source", &source.path), ("backup", target)])
            .map_err(|_| Error::Unsupported("backup overlaps source".into()))?;
        let metadata = fs::symlink_metadata(&source.path).map_err(Error::Source)?;
        if metadata.is_symlink() {
            return Err(Error::Unsupported(source.name.clone()));
        }
        let entries = if metadata.is_dir() {
            // These are owned project/storage directories, not old workspaces:
            // a project may legitimately keep its own diagnostics or plugins.
            let plan = plan_with(&source.path, false)?;
            bytes = bytes.saturating_add(plan.bytes);
            plan.files
                .into_iter()
                .map(|relative| {
                    (
                        source.path.join(&relative),
                        format!("{}/{relative}", source.name),
                    )
                })
                .collect::<Vec<_>>()
        } else if metadata.is_file() {
            bytes = bytes.saturating_add(metadata.len());
            vec![(source.path.clone(), source.name.clone())]
        } else {
            return Err(Error::Unsupported(source.name.clone()));
        };
        for (path, name) in entries {
            if !names.insert(name.clone()) {
                return Err(Error::Unsupported("overlapping backup namespaces".into()));
            }
            planned.push((path, name));
        }
    }
    let available = free_space(target)?;
    let required = scaled(bytes);
    if available < required {
        return Err(Error::InsufficientSpace {
            required,
            available,
        });
    }
    planned.sort_by(|left, right| left.1.cmp(&right.1));
    let root = free_root(target, version);
    fs::create_dir_all(&root).map_err(Error::Target)?;
    let mut files = Vec::with_capacity(planned.len());
    for (path, name) in planned {
        files.push(copy_file(&path, &root, &name)?);
    }
    let manifest = Manifest {
        schema_version: MULTI_SOURCE_SCHEMA_VERSION,
        source_path: String::new(),
        created_at: chrono::Utc::now().to_rfc3339(),
        version: version.into(),
        files,
        sources: sources.to_vec(),
    };
    let encoded = serde_json::to_vec_pretty(&manifest)
        .map_err(|error| Error::Target(std::io::Error::other(error)))?;
    write(&root.join(MANIFEST), &encoded)?;
    let snapshot = Snapshot {
        root,
        files: manifest.files,
        sources: manifest.sources,
    };
    if let Err(error) = verify(&snapshot) {
        let _ = fs::remove_file(snapshot.root.join(MANIFEST));
        return Err(error);
    }
    Ok(snapshot)
}

/// Refuse a snapshot that the target volume cannot hold.
///
/// The check runs before anything is written, so a rejected upgrade leaves no
/// partial copy behind. Available space belongs to the volume rather than to the
/// directory: the caller may not have created the target yet.
pub fn preflight(source: &Path, target: &Path) -> Result<(), Error> {
    preflight_with(source, free_space(target)?)
}

/// The decision itself, with the volume's free space supplied by the caller so
/// that the refusal can be exercised without filling a drive.
fn preflight_with(source: &Path, available: u64) -> Result<(), Error> {
    let required = scaled(plan(source)?.bytes);
    if available < required {
        return Err(Error::InsufficientSpace {
            required,
            available,
        });
    }
    Ok(())
}

/// Copy the source directory below the target and write its manifest last.
///
/// The manifest is the completion marker: a copy that dies partway leaves files
/// but no manifest, so nothing can mistake it for a usable backup. The copy is
/// read back before returning, because an upgrade that proceeds on an unverified
/// backup is the exact failure this module exists to prevent.
pub fn snapshot(source: &Path, target: &Path, version: &str) -> Result<Snapshot, Error> {
    snapshot_into(source, &free_root(target, version), version)
}

fn snapshot_into(source: &Path, root: &Path, version: &str) -> Result<Snapshot, Error> {
    let plan = plan(source)?;
    // The manifest is written at the snapshot root, so a source file with that
    // name would be overwritten by it. Refusing keeps the copy honest.
    if plan.files.iter().any(|path| path == MANIFEST) {
        return Err(Error::Unsupported(MANIFEST.into()));
    }
    fs::create_dir_all(root).map_err(Error::Target)?;
    let mut files = Vec::with_capacity(plan.files.len());
    for relative in &plan.files {
        files.push(copy(source, root, relative)?);
    }
    let manifest = Manifest {
        schema_version: SCHEMA_VERSION,
        source_path: source.to_string_lossy().into_owned(),
        created_at: chrono::Utc::now().to_rfc3339(),
        version: version.to_owned(),
        files,
        sources: Vec::new(),
    };
    let encoded = serde_json::to_vec_pretty(&manifest)
        .map_err(|error| Error::Target(std::io::Error::other(error)))?;
    write(&root.join(MANIFEST), &encoded)?;
    let snapshot = Snapshot {
        root: root.to_path_buf(),
        files: manifest.files,
        sources: manifest.sources,
    };
    verify(&snapshot)?;
    Ok(snapshot)
}

/// Re-hash every copied file and compare it with the manifest.
///
/// The manifest is re-read from disk rather than taken from the in-memory value,
/// so an edited, truncated, or missing manifest is caught too.
pub fn verify(snapshot: &Snapshot) -> Result<(), Error> {
    let manifest = read(&snapshot.root.join(MANIFEST))?;
    if !valid_schema(&manifest)
        || manifest.files != snapshot.files
        || manifest.sources != snapshot.sources
    {
        return Err(mismatch(MANIFEST));
    }
    let listed: BTreeSet<&str> = manifest
        .files
        .iter()
        .map(|entry| entry.path.as_str())
        .collect();
    for entry in &manifest.files {
        let path = snapshot.root.join(&entry.path);
        // A link here would let the hash of one file vouch for another.
        let metadata = fs::symlink_metadata(&path).map_err(|_| mismatch(&entry.path))?;
        if !metadata.is_file() || metadata.len() != entry.size {
            return Err(mismatch(&entry.path));
        }
        if digest(&path)? != entry.sha256 {
            return Err(mismatch(&entry.path));
        }
    }
    // A file the manifest does not mention cannot be vouched for.
    for relative in plan(&snapshot.root)?.files {
        if relative != MANIFEST && !listed.contains(relative.as_str()) {
            return Err(mismatch(&relative));
        }
    }
    Ok(())
}

/// What a snapshot would contain, and how many bytes that is.
struct Plan {
    /// Relative paths, sorted, slash-separated, never empty.
    files: Vec<String>,
    bytes: u64,
}

/// Walk the source once and decide what a snapshot contains.
///
/// Reads and metadata only: the walk is also what verify uses to notice files
/// the manifest does not mention.
fn plan(source: &Path) -> Result<Plan, Error> {
    plan_with(source, true)
}

fn plan_with(source: &Path, rebuild: bool) -> Result<Plan, Error> {
    let metadata = fs::metadata(source).map_err(Error::Source)?;
    if !metadata.is_dir() {
        return Err(Error::Source(std::io::Error::other("not a directory")));
    }
    let mut plan = Plan {
        files: Vec::new(),
        bytes: 0,
    };
    walk(source, Path::new(""), &mut plan, rebuild)?;
    plan.files.sort();
    Ok(plan)
}

fn walk(directory: &Path, prefix: &Path, plan: &mut Plan, rebuild: bool) -> Result<(), Error> {
    let entries = fs::read_dir(directory).map_err(Error::Source)?;
    for entry in entries {
        let entry = entry.map_err(Error::Source)?;
        let path = entry.path();
        // The spelling is normalised once, here, so that the manifest and the
        // path used to copy a file can never disagree about separators.
        let relative = prefix.join(entry.file_name());
        let Some(relative) = relative
            .to_str()
            .map(|name| PathBuf::from(name.replace('\\', "/")))
        else {
            return Err(Error::Unsupported(path.to_string_lossy().into_owned()));
        };
        if (rebuild && excluded(&relative))
            || relative
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.to_ascii_lowercase().ends_with(EXCLUDED_SUFFIX))
        {
            continue;
        }
        // Never a follow: a link under the source would copy its target, which
        // is neither the file the old version owned nor reproducible on restore.
        let metadata = fs::symlink_metadata(&path).map_err(Error::Source)?;
        if metadata.is_symlink() {
            return Err(Error::Unsupported(relative.to_string_lossy().into_owned()));
        }
        if metadata.is_dir() {
            walk(&path, &relative, plan, rebuild)?;
        } else if metadata.is_file() {
            plan.bytes = plan.bytes.saturating_add(metadata.len());
            plan.files.push(relative.to_string_lossy().into_owned());
        } else {
            return Err(Error::Unsupported(relative.to_string_lossy().into_owned()));
        }
    }
    Ok(())
}

/// Decide whether a relative path is rebuilt instead of preserved.
///
/// The two directories are matched by their top-level path component only, so a
/// project that keeps its own diagnostics folder keeps it. Lock files are
/// matched by file name, so a directory named lock is kept.
fn excluded(relative: &Path) -> bool {
    let mut components = relative
        .components()
        .filter_map(|component| match component {
            Component::Normal(name) => name.to_str(),
            _ => None,
        });
    let top = components.next().unwrap_or_default().to_ascii_lowercase();
    if top == "diagnostics" {
        return true;
    }
    if top == "plugins"
        && components
            .next()
            .is_some_and(|name| name.eq_ignore_ascii_case("babeldoc"))
    {
        return true;
    }
    relative
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.to_ascii_lowercase().ends_with(EXCLUDED_SUFFIX))
}

/// Copy one file and hash the bytes as they are written.
///
/// The hash comes from the same read that produced the copy, so it describes
/// what is on disk in the snapshot rather than what was expected.
fn copy(source: &Path, root: &Path, relative: &str) -> Result<Entry, Error> {
    copy_file(&source.join(relative), root, relative)
}

fn copy_file(source: &Path, root: &Path, relative: &str) -> Result<Entry, Error> {
    let to = root.join(relative);
    if let Some(parent) = to.parent() {
        fs::create_dir_all(parent).map_err(Error::Target)?;
    }
    let mut reader = File::open(source).map_err(Error::Source)?;
    let mut writer = File::create(&to).map_err(Error::Target)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 64 * 1024];
    let mut size = 0u64;
    loop {
        let read = reader.read(&mut buffer).map_err(Error::Source)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        writer.write_all(&buffer[..read]).map_err(Error::Target)?;
        size += read as u64;
    }
    writer.flush().map_err(Error::Target)?;
    Ok(Entry {
        path: relative.to_owned(),
        size,
        sha256: format!("{:x}", hasher.finalize()),
    })
}

fn write(path: &Path, bytes: &[u8]) -> Result<(), Error> {
    let mut file = File::create(path).map_err(Error::Target)?;
    file.write_all(bytes).map_err(Error::Target)?;
    file.flush().map_err(Error::Target)?;
    Ok(())
}

fn read(path: &Path) -> Result<Manifest, Error> {
    let bytes = fs::read(path).map_err(|_| mismatch(MANIFEST))?;
    serde_json::from_slice(&bytes).map_err(|_| mismatch(MANIFEST))
}

fn digest(path: &Path) -> Result<String, Error> {
    let mut file = File::open(path).map_err(|_| mismatch(MANIFEST))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer).map_err(|_| mismatch(MANIFEST))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn mismatch(path: &str) -> Error {
    Error::ManifestMismatch(path.to_owned())
}

/// A fresh directory name below the target for this version.
///
/// An existing backup is never reused or replaced: two upgrades in the same
/// second must not be able to overwrite the only copy of the older one.
fn free_root(target: &Path, version: &str) -> PathBuf {
    let stamp = chrono::Utc::now().format("%Y%m%d-%H%M%S").to_string();
    let name = format!("{version}-{stamp}");
    let first = target.join(&name);
    if !first.exists() {
        return first;
    }
    for suffix in 2..1000 {
        let candidate = target.join(format!("{name}-{suffix}"));
        if !candidate.exists() {
            return candidate;
        }
    }
    // A thousand backups under one timestamp is a caller mistake, not a case
    // worth designing for; the last name still never overwrites anything.
    target.join(format!("{name}-{}", now_nanos()))
}

fn now_nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default()
}

/// The source size plus the headroom, rounded up and never allowed to wrap.
fn scaled(bytes: u64) -> u64 {
    (bytes as f64 * HEADROOM).ceil() as u64
}

/// The nearest directory at or above the path. The volume it sits on is the one
/// the copy will land on, even when the target does not exist yet.
fn volume_root(path: &Path) -> Result<PathBuf, Error> {
    let mut probe = path.to_path_buf();
    loop {
        if probe.is_dir() {
            return Ok(probe);
        }
        if !probe.pop() {
            return Err(Error::Target(std::io::Error::other("no volume")));
        }
    }
}

#[cfg(windows)]
fn free_space(path: &Path) -> Result<u64, Error> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;

    let wide: Vec<u16> = volume_root(path)?
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let mut available = 0u64;
    let ok = unsafe {
        GetDiskFreeSpaceExW(
            wide.as_ptr(),
            &mut available,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if ok == 0 {
        return Err(Error::Target(std::io::Error::last_os_error()));
    }
    Ok(available)
}

#[cfg(unix)]
fn free_space(path: &Path) -> Result<u64, Error> {
    use std::os::unix::ffi::OsStrExt;

    let directory = volume_root(path)?;
    let encoded = std::ffi::CString::new(directory.as_os_str().as_bytes())
        .map_err(|_| Error::Target(std::io::Error::other("unusable path")))?;
    let mut stats = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(encoded.as_ptr(), stats.as_mut_ptr()) } != 0 {
        return Err(Error::Target(std::io::Error::last_os_error()));
    }
    let stats = unsafe { stats.assume_init() };
    Ok((stats.f_bavail as u64).saturating_mul(stats.f_frsize as u64))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// A scratch root that owns itself, so a failing assertion cannot leak a
    /// directory under the user's temp folder.
    struct Scratch(PathBuf);

    impl Scratch {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let path = std::env::temp_dir().join(format!(
                "wenyi-migration-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::SeqCst)
            ));
            let _ = fs::remove_dir_all(&path);
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        fn path(&self, name: &str) -> PathBuf {
            self.0.join(name)
        }

        /// A data directory shaped like the real workspace.
        fn workspace(&self) -> PathBuf {
            let root = self.path("workspace");
            for relative in [
                "projects/abcd/manifest.json",
                "projects/abcd/state.sqlite3",
                "plugins/babeldoc/model.bin",
                "diagnostics/native-2026-11-02.log",
                "logs/wenyi.log",
            ] {
                let path = root.join(relative);
                fs::create_dir_all(path.parent().unwrap()).unwrap();
                fs::write(&path, relative.as_bytes()).unwrap();
            }
            fs::write(root.join("local.sqlite3"), b"database").unwrap();
            fs::write(root.join(".desktop.lock"), b"").unwrap();
            root
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    /// Name, length, and modification time of everything below a root.
    fn fingerprint(root: &Path) -> BTreeMap<String, (u64, SystemTime)> {
        fn visit(root: &Path, directory: &Path, into: &mut BTreeMap<String, (u64, SystemTime)>) {
            for entry in fs::read_dir(directory).unwrap() {
                let entry = entry.unwrap();
                let path = entry.path();
                let metadata = fs::symlink_metadata(&path).unwrap();
                let relative = path
                    .strip_prefix(root)
                    .unwrap()
                    .to_string_lossy()
                    .replace('\\', "/");
                into.insert(relative, (metadata.len(), metadata.modified().unwrap()));
                if metadata.is_dir() {
                    visit(root, &path, into);
                }
            }
        }
        let mut into = BTreeMap::new();
        into.insert(".".into(), {
            let metadata = fs::metadata(root).unwrap();
            (metadata.len(), metadata.modified().unwrap())
        });
        visit(root, root, &mut into);
        into
    }

    fn listed(snapshot: &Snapshot) -> Vec<&str> {
        snapshot
            .files
            .iter()
            .map(|entry| entry.path.as_str())
            .collect()
    }

    #[test]
    fn a_snapshot_copies_every_kept_file_and_verifies_itself() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        assert_eq!(
            listed(&snapshot),
            [
                "local.sqlite3",
                "logs/wenyi.log",
                "projects/abcd/manifest.json",
                "projects/abcd/state.sqlite3",
            ]
        );
        for entry in &snapshot.files {
            assert_eq!(
                fs::read(snapshot.root.join(&entry.path)).unwrap().len() as u64,
                entry.size
            );
        }
        assert!(snapshot.root.join(MANIFEST).is_file());
        verify(&snapshot).unwrap();
    }

    #[test]
    fn the_snapshot_directory_is_named_after_the_version_and_the_time() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        let name = snapshot
            .root
            .file_name()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        assert!(name.starts_with("1.4.0-"), "{name}");
        assert_eq!(
            name.len(),
            "1.4.0-".len() + "YYYYMMDD-HHMMSS".len(),
            "{name}"
        );
        assert_eq!(snapshot.root.parent().unwrap(), scratch.path("backup"));
    }

    #[test]
    fn the_source_directory_is_byte_for_byte_untouched() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let before = fingerprint(&source);
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        verify(&snapshot).unwrap();
        // The whole point of running this after the engine stopped is that the
        // old workspace is still exactly what the old version will need.
        assert_eq!(fingerprint(&source), before);
    }

    #[test]
    fn the_excluded_directories_and_lock_files_are_not_copied() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        fs::create_dir_all(source.join("projects/abcd/diagnostics")).unwrap();
        fs::write(source.join("projects/abcd/diagnostics/keep.txt"), b"kept").unwrap();
        fs::create_dir_all(source.join("plugins/other")).unwrap();
        fs::write(source.join("plugins/other/model.bin"), b"kept").unwrap();
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        let copied = listed(&snapshot);
        assert!(!copied
            .iter()
            .any(|path| path.starts_with("plugins/babeldoc")));
        assert!(!copied.iter().any(|path| path.starts_with("diagnostics")));
        assert!(!copied.iter().any(|path| path.ends_with(".lock")));
        // Only the top-level directory is excluded: a project may own one too.
        assert!(copied.contains(&"projects/abcd/diagnostics/keep.txt"));
        assert!(copied.contains(&"plugins/other/model.bin"));
    }

    #[test]
    fn exclusion_matching_ignores_case_and_separators() {
        for path in [
            "diagnostics/a.log",
            "Diagnostics/a.log",
            "plugins/babeldoc/model.bin",
            "Plugins/BabelDoc/model.bin",
            ".desktop.lock",
            "projects/abcd/.desktop-instance.lock",
        ] {
            assert!(excluded(Path::new(path)), "{path}");
        }
        for path in [
            "logs/wenyi.log",
            "plugins/other/model.bin",
            "local.sqlite3",
            "projects/abcd/lock",
        ] {
            assert!(!excluded(Path::new(path)), "{path}");
        }
    }

    #[test]
    fn a_live_write_ahead_log_is_copied_as_its_own_entry() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        // A shutdown that timed out leaves committed rows only in the log file.
        fs::write(source.join("local.sqlite3-wal"), b"uncheckpointed").unwrap();
        fs::write(source.join("local.sqlite3-shm"), b"shared memory").unwrap();
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        let copied = listed(&snapshot);
        assert!(copied.contains(&"local.sqlite3-wal"));
        assert!(copied.contains(&"local.sqlite3-shm"));
        assert_eq!(
            fs::read(snapshot.root.join("local.sqlite3-wal")).unwrap(),
            b"uncheckpointed"
        );
    }

    #[test]
    fn a_source_file_named_like_the_manifest_is_refused_instead_of_overwritten() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        fs::write(source.join(MANIFEST), b"{}").unwrap();
        match snapshot(&source, &scratch.path("backup"), "1.4.0") {
            Err(Error::Unsupported(path)) => assert_eq!(path, MANIFEST),
            other => panic!("{other:?}"),
        }
        // Nothing was written, so a retry against a fixed source still works.
        fs::remove_file(source.join(MANIFEST)).unwrap();
        snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
    }

    #[test]
    fn a_second_snapshot_never_replaces_the_first() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let target = scratch.path("backup");
        let first = snapshot(&source, &target, "1.4.0").unwrap();
        let first_manifest = fs::read(first.root.join(MANIFEST)).unwrap();
        let second = snapshot(&source, &target, "1.4.0").unwrap();
        assert_ne!(first.root, second.root);
        verify(&first).unwrap();
        verify(&second).unwrap();
        assert_eq!(fs::read(first.root.join(MANIFEST)).unwrap(), first_manifest);
    }

    #[test]
    fn a_copy_that_dies_partway_leaves_no_manifest_and_can_be_rerun() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let root = scratch.path("backup/1.4.0-20261102-120000");
        // Make the first file impossible to write, as a full or unwritable
        // volume would.
        fs::create_dir_all(root.join("local.sqlite3")).unwrap();
        let failure = snapshot_into(&source, &root, "1.4.0").unwrap_err();
        assert!(matches!(failure, Error::Target(_)), "{failure:?}");
        assert!(!root.join(MANIFEST).exists());

        fs::remove_dir_all(root.join("local.sqlite3")).unwrap();
        let snapshot = snapshot_into(&source, &root, "1.4.0").unwrap();
        verify(&snapshot).unwrap();
    }

    #[test]
    fn an_edited_manifest_is_rejected() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        let path = snapshot.root.join(MANIFEST);
        let text = fs::read_to_string(&path).unwrap();
        fs::write(&path, text.replace("\"size\": 8", "\"size\": 9")).unwrap();
        assert!(matches!(verify(&snapshot), Err(Error::ManifestMismatch(_))));
    }

    #[test]
    fn a_truncated_manifest_is_rejected() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        fs::write(snapshot.root.join(MANIFEST), b"{\"schema_version\": 1").unwrap();
        assert!(matches!(verify(&snapshot), Err(Error::ManifestMismatch(_))));
    }

    #[test]
    fn an_edited_file_is_rejected() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        fs::write(snapshot.root.join("local.sqlite3"), b"database!").unwrap();
        match verify(&snapshot) {
            Err(Error::ManifestMismatch(path)) => assert_eq!(path, "local.sqlite3"),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_file_the_manifest_does_not_list_is_rejected() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        fs::write(snapshot.root.join("projects/abcd/extra.json"), b"{}").unwrap();
        assert!(matches!(verify(&snapshot), Err(Error::ManifestMismatch(_))));
    }

    #[test]
    fn a_deleted_copy_is_rejected() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        fs::remove_file(snapshot.root.join("local.sqlite3")).unwrap();
        assert!(matches!(verify(&snapshot), Err(Error::ManifestMismatch(_))));
    }

    #[test]
    fn a_snapshot_written_by_another_process_can_still_be_verified() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let written = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        let files = written.files.clone();
        let root = written.root.clone();
        drop(written);
        // Nothing of the writing run survives except the directory on disk.
        let loaded = Snapshot::load(&root).unwrap();
        assert_eq!(loaded.files, files);
        verify(&loaded).unwrap();
        fs::write(root.join("local.sqlite3"), b"tampered").unwrap();
        assert!(matches!(verify(&loaded), Err(Error::ManifestMismatch(_))));
        assert!(matches!(
            Snapshot::load(&scratch.path("absent")),
            Err(Error::ManifestMismatch(_))
        ));
    }

    #[test]
    fn a_manifest_that_does_not_match_the_snapshot_is_rejected() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let mut snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        snapshot.files.pop();
        assert!(matches!(verify(&snapshot), Err(Error::ManifestMismatch(_))));
    }

    #[test]
    fn a_target_that_cannot_hold_the_copy_is_refused_before_writing() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let target = scratch.path("backup");
        let required = scaled(plan(&source).unwrap().bytes);
        assert!(required > 0);

        match preflight_with(&source, required - 1) {
            Err(Error::InsufficientSpace {
                required: want,
                available,
            }) => {
                assert_eq!(available, required - 1);
                assert!(want > available);
            }
            other => panic!("{other:?}"),
        }
        preflight_with(&source, required).unwrap();
        // Nothing was written by either decision.
        assert!(!target.exists());
        preflight(&source, &target).unwrap();
    }

    #[test]
    fn a_missing_source_is_a_read_failure_not_an_empty_backup() {
        let scratch = Scratch::new();
        let failure = snapshot(&scratch.path("absent"), &scratch.path("backup"), "1.4.0");
        assert!(matches!(failure, Err(Error::Source(_))));
        assert!(!scratch.path("backup").exists());
    }

    #[test]
    fn an_empty_source_still_produces_a_verifiable_snapshot() {
        let scratch = Scratch::new();
        let source = scratch.path("workspace");
        fs::create_dir_all(&source).unwrap();
        let snapshot = snapshot(&source, &scratch.path("backup"), "1.4.0").unwrap();
        assert!(snapshot.files.is_empty());
        verify(&snapshot).unwrap();
    }

    #[test]
    fn explicit_sources_refuse_recursion_and_preserve_project_owned_subdirectories() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        let sources = [BackupSource {
            name: "projects/0".into(),
            path: source.clone(),
        }];
        assert!(snapshot_sources(&sources, &source.join("backup"), "1.4.0").is_err());
        assert!(!source.join("backup").exists());
        let snapshot = snapshot_sources(&sources, &scratch.path("backup"), "1.4.0").unwrap();
        assert!(listed(&snapshot).contains(&"projects/0/diagnostics/native-2026-11-02.log"));
        assert!(listed(&snapshot).contains(&"projects/0/plugins/babeldoc/model.bin"));
        assert_eq!(Snapshot::load(&snapshot.root).unwrap().sources, sources);
        let mut altered = snapshot.clone();
        altered.sources[0].path = scratch.path("other");
        assert!(verify(&altered).is_err());
        let escaped = [BackupSource {
            name: "../escape".into(),
            path: source,
        }];
        assert!(snapshot_sources(&escaped, &scratch.path("another"), "1.4.0").is_err());
    }

    #[test]
    fn sizes_are_scaled_with_headroom_and_rounded_up() {
        assert_eq!(scaled(0), 0);
        assert_eq!(scaled(10), 11);
        assert_eq!(scaled(1024), 1127);
        assert_eq!(scaled(u64::MAX), u64::MAX);
    }

    #[cfg(unix)]
    #[test]
    fn a_link_in_the_source_is_refused_instead_of_followed() {
        let scratch = Scratch::new();
        let source = scratch.workspace();
        std::os::unix::fs::symlink(source.join("local.sqlite3"), source.join("alias")).unwrap();
        match snapshot(&source, &scratch.path("backup"), "1.4.0") {
            Err(Error::Unsupported(path)) => assert_eq!(path, "alias"),
            other => panic!("{other:?}"),
        }
    }
}
