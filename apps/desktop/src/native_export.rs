//! Native-owned save destinations. IPC supplies IDs/options, never paths or credentials.
use std::{
    fs::File,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{mpsc, Arc, Condvar, Mutex},
    time::Duration,
};

use cap_fs_ext::{FollowSymlinks, OpenOptionsFollowExt};
use cap_std::{ambient_authority, fs::Dir};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::Manager;

use crate::local_connection::Connection;

type Result<T> = std::result::Result<T, String>;
const CHUNK: usize = 64 * 1024;
pub(crate) const MAX_SOURCE_IDENTITIES: usize = 1024;
pub(crate) const MAX_SOURCE_ALIASES: usize = 4096;

/// Value identity read from the actual upload FD; the ledger never retains an FD.
#[derive(Clone, Copy, PartialEq, Eq)]
struct SourceIdentity {
    volume: u64,
    file: u128,
}

impl SourceIdentity {
    fn read(file: &File) -> Result<Self> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let metadata = file
                .metadata()
                .map_err(|_| "Cannot identify source file.")?;
            Ok(Self {
                volume: metadata.dev(),
                file: u128::from(metadata.ino()),
            })
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::Storage::FileSystem::{
                FileIdInfo, GetFileInformationByHandleEx, FILE_ID_INFO,
            };
            let mut info = std::mem::MaybeUninit::<FILE_ID_INFO>::uninit();
            if unsafe {
                GetFileInformationByHandleEx(
                    file.as_raw_handle(),
                    FileIdInfo,
                    info.as_mut_ptr().cast(),
                    std::mem::size_of::<FILE_ID_INFO>() as u32,
                )
            } == 0
            {
                return Err("Cannot identify source file.".into());
            }
            let info = unsafe { info.assume_init() };
            Ok(Self {
                volume: info.VolumeSerialNumber,
                file: u128::from_le_bytes(info.FileId.Identifier),
            })
        }
    }
}

fn same_alias(left: &Path, right: &Path) -> bool {
    #[cfg(not(windows))]
    {
        left == right
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        // Windows ordinal case comparison, not Rust Unicode/ASCII lowercasing.
        // Conservatively protects case variants even in case-sensitive directories.
        #[link(name = "kernel32")]
        extern "system" {
            fn CompareStringOrdinal(
                left: *const u16,
                left_len: i32,
                right: *const u16,
                right_len: i32,
                ignore_case: i32,
            ) -> i32;
        }
        let left: Vec<u16> = left.as_os_str().encode_wide().collect();
        let right: Vec<u16> = right.as_os_str().encode_wide().collect();
        unsafe {
            CompareStringOrdinal(
                left.as_ptr(),
                left.len() as i32,
                right.as_ptr(),
                right.len() as i32,
                1,
            ) == 2
        }
    }
}

#[derive(Default)]
struct Activity {
    closing: bool,
    active: usize,
    pending: usize,
    sources: Vec<SourceIdentity>,
    aliases: Vec<PathBuf>,
}

#[derive(Default, Clone)]
pub struct Saves(Arc<(Mutex<Activity>, Condvar)>);

impl Saves {
    #[cfg(test)]
    pub fn source_count(&self) -> usize {
        self.0 .0.lock().unwrap().sources.len()
    }

    /// Reserve protection before POST, including failed or uncertain uploads.
    /// Identity reuse can conservatively reject an unrelated file until app exit.
    pub fn protect_source(&self, path: PathBuf, file: File) -> Result<()> {
        let identity = SourceIdentity::read(&file)?;
        let parent = path.parent().ok_or("Cannot identify source path.")?;
        let path = parent
            .canonicalize()
            .map_err(|_| "Cannot identify source path.")?
            .join(path.file_name().ok_or("Cannot identify source path.")?);
        let mut state = self.0 .0.lock().unwrap();
        let new_identity = !state.sources.contains(&identity);
        let new_alias = !state.aliases.iter().any(|alias| same_alias(alias, &path));
        if (new_identity && state.sources.len() >= MAX_SOURCE_IDENTITIES)
            || (new_alias && state.aliases.len() >= MAX_SOURCE_ALIASES)
        {
            return Err("Source protection limit reached. No upload was started. Restart Wenyi before uploading more files.".into());
        }
        if state.closing {
            return Err("Wenyi is closing. No upload was started.".into());
        }
        if new_identity {
            state.sources.push(identity);
        }
        if new_alias {
            state.aliases.push(path);
        }
        Ok(())
    }

    pub fn close(&self) {
        self.0 .0.lock().unwrap().closing = true;
    }

    /// Updating must not interrupt a native dialog, transfer or atomic publish.
    pub fn close_if_idle(&self) -> bool {
        let mut state = self.0 .0.lock().unwrap();
        if state.closing || state.active != 0 || state.pending != 0 {
            return false;
        }
        state.closing = true;
        true
    }

    pub fn reopen(&self) {
        self.0 .0.lock().unwrap().closing = false;
    }

    pub fn wait(&self) {
        let mut state = self.0 .0.lock().unwrap();
        while state.active != 0 {
            state = self.0 .1.wait(state).unwrap();
        }
    }

    fn check(&self) -> Result<()> {
        if self.0 .0.lock().unwrap().closing {
            Err("Saving stopped because Wenyi is closing.".into())
        } else {
            Ok(())
        }
    }

    fn begin(&self) -> Result<Active> {
        let mut state = self.0 .0.lock().unwrap();
        if state.closing {
            return Err("Wenyi is closing.".into());
        }
        state.active += 1;
        Ok(Active(self.clone()))
    }

    /// Dialogs/planning block installation, but normal quit may cancel them.
    /// Only transfers/publishes need to be drained by `wait`.
    fn reserve(&self) -> Result<Pending> {
        let mut state = self.0 .0.lock().unwrap();
        if state.closing {
            return Err("Wenyi is closing.".into());
        }
        state.pending += 1;
        Ok(Pending(self.clone()))
    }

    fn protect(&self, path: &Path, roots: &[PathBuf]) -> Result<()> {
        Self::protect_locked(&self.0 .0.lock().unwrap(), path, roots)
    }

    fn protect_locked(state: &Activity, path: &Path, roots: &[PathBuf]) -> Result<()> {
        for root in roots {
            let root = root
                .canonicalize()
                .map_err(|_| "Internal workspace is unavailable.")?;
            if path.starts_with(root) {
                return Err("Choose a destination outside Wenyi's internal workspace.".into());
            }
        }
        let identity = match File::open(path) {
            Ok(file) => Some(SourceIdentity::read(&file)?),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(_) => return Err("Cannot inspect destination source protection.".into()),
        };
        // Resolve existing aliases as well as the canonical parent spelling.
        // On Windows this also normalizes DOS/extended and short-name paths.
        let canonical = path.canonicalize().ok();
        if state.aliases.iter().any(|alias| {
            same_alias(alias, path)
                || canonical
                    .as_ref()
                    .is_some_and(|canonical| same_alias(alias, canonical))
        }) || identity.is_some_and(|identity| state.sources.contains(&identity))
        {
            return Err("The original source cannot be overwritten.".into());
        }
        Ok(())
    }
}

struct Active(Saves);
impl Drop for Active {
    fn drop(&mut self) {
        self.0 .0 .0.lock().unwrap().active -= 1;
        self.0 .0 .1.notify_all();
    }
}

struct Pending(Saves);
impl Drop for Pending {
    fn drop(&mut self) {
        self.0 .0 .0.lock().unwrap().pending -= 1;
    }
}

#[derive(Deserialize)]
struct Plan {
    filename: String,
    extension: String,
    label: String,
    request: Option<Value>,
    protected_roots: Vec<PathBuf>,
}

impl Plan {
    fn destination(&self, mut path: PathBuf) -> PathBuf {
        let suffix = if self.filename.ends_with(".html.zip") {
            ".html.zip".to_string()
        } else {
            format!(".{}", self.extension)
        };
        if !path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_ascii_lowercase()
            .ends_with(&suffix)
        {
            let mut name = path.file_name().unwrap_or_default().to_os_string();
            name.push(suffix);
            path.set_file_name(name);
        }
        path
    }
}

#[derive(Debug, Serialize)]
pub struct Saved {
    path: String,
}

impl Connection {
    fn request(&self, path: &str, body: Option<&Value>) -> Result<reqwest::blocking::Response> {
        let url = format!("{}{path}", self.base);
        let request = match body {
            Some(body) => self.client.post(url).json(body),
            None => self.client.get(url),
        };
        let response = request
            .bearer_auth(&self.token)
            .send()
            .map_err(|_| "Local export connection failed. Retry saving.")?;
        if !response.status().is_success() {
            return Err(
                "The local engine could not complete this export. Check export history.".into(),
            );
        }
        Ok(response)
    }

    fn json(&self, path: &str, body: Option<&Value>) -> Result<Value> {
        let mut response = self.request(path, body)?.take(1024 * 1024 + 1);
        let mut bytes = Vec::new();
        response
            .read_to_end(&mut bytes)
            .map_err(|_| "Invalid export response.")?;
        if bytes.len() > 1024 * 1024 {
            return Err("Export response is too large.".into());
        }
        serde_json::from_slice(&bytes).map_err(|_| "Invalid export response.".into())
    }

    fn content(&self, path: String, saves: &Saves) -> Result<(u64, Download)> {
        let connection = self.clone();
        let (sender, receiver) = mpsc::sync_channel(1);
        // This worker owns HTTP only, never a destination or file handle. The one-slot
        // channel bounds read-ahead and allows the saving task to stop promptly even
        // while the server is still preparing a large HTML archive.
        std::thread::spawn(move || {
            let produce = || -> Result<()> {
                let mut response = connection
                    .client
                    .get(format!("{}{path}", connection.base))
                    .bearer_auth(&connection.token)
                    .timeout(Duration::from_secs(300))
                    .send()
                    .map_err(|_| "Export transfer failed.")?;
                if !response.status().is_success() {
                    return Err("The local engine could not provide this export.".into());
                }
                let length = response
                    .content_length()
                    .ok_or("Export response has no verified length.")?;
                sender
                    .send(Ok(DownloadPart::Length(length)))
                    .map_err(|_| "Save cancelled.")?;
                loop {
                    let mut buffer = vec![0; CHUNK];
                    let count = response
                        .read(&mut buffer)
                        .map_err(|_| "Export transfer interrupted.")?;
                    if count == 0 {
                        break;
                    }
                    buffer.truncate(count);
                    sender
                        .send(Ok(DownloadPart::Bytes(buffer)))
                        .map_err(|_| "Save cancelled.")?;
                }
                sender
                    .send(Ok(DownloadPart::End))
                    .map_err(|_| "Save cancelled.")?;
                Ok(())
            };
            if let Err(error) = produce() {
                let _ = sender.send(Err(error));
            }
        });
        let mut download = Download {
            receiver,
            saves: saves.clone(),
            pending: std::io::Cursor::new(Vec::new()),
            ended: false,
        };
        match download.next()? {
            DownloadPart::Length(length) => Ok((length, download)),
            _ => Err("Invalid export response.".into()),
        }
    }
}

enum DownloadPart {
    Length(u64),
    Bytes(Vec<u8>),
    End,
}
struct Download {
    receiver: mpsc::Receiver<Result<DownloadPart>>,
    saves: Saves,
    pending: std::io::Cursor<Vec<u8>>,
    ended: bool,
}
impl Download {
    fn next(&mut self) -> Result<DownloadPart> {
        loop {
            self.saves.check()?;
            match self.receiver.recv_timeout(Duration::from_millis(200)) {
                Ok(part) => return part,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(_) => return Err("Export transfer interrupted.".into()),
            }
        }
    }
}
impl Read for Download {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        loop {
            let count = self.pending.read(buffer)?;
            if count != 0 || self.ended {
                return Ok(count);
            }
            match self.next().map_err(std::io::Error::other)? {
                DownloadPart::Bytes(bytes) => self.pending = std::io::Cursor::new(bytes),
                DownloadPart::End => self.ended = true,
                _ => return Err(std::io::Error::other("Invalid export stream.")),
            }
        }
    }
}

trait Dialog {
    fn choose(&self, plan: &Plan) -> Option<PathBuf>;
    fn replace(&self, path: &Path) -> bool;
}

struct NativeDialog;
impl Dialog for NativeDialog {
    fn choose(&self, plan: &Plan) -> Option<PathBuf> {
        rfd::FileDialog::new()
            .set_title(format!("Save {}", plan.label))
            .set_file_name(&plan.filename)
            .add_filter(&plan.label, &[&plan.extension])
            .save_file()
    }

    fn replace(&self, path: &Path) -> bool {
        rfd::MessageDialog::new()
            .set_title("Replace existing file?")
            .set_description(format!(
                "Replace {}? The existing file is kept if saving fails.",
                path.display()
            ))
            .set_buttons(rfd::MessageButtons::YesNo)
            .show()
            == rfd::MessageDialogResult::Yes
    }
}

fn no_symlinks(path: &Path) -> Result<()> {
    if !path.is_absolute()
        || path
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err("Choose an absolute destination without parent traversal.".into());
    }
    for ancestor in path.ancestors() {
        let meta = std::fs::symlink_metadata(ancestor)
            .map_err(|_| "Destination folder is unavailable.")?;
        if meta.is_symlink() {
            return Err("Choose a destination without symbolic links.".into());
        }
    }
    Ok(())
}

type Existing = Option<(same_file::Handle, u64, std::time::SystemTime)>;

struct Target {
    path: PathBuf,
    dir: Dir,
    #[cfg(windows)]
    _parent_guards: Vec<File>,
    parent_identity: same_file::Handle,
    name: PathBuf,
    existing: Existing,
}

impl Target {
    fn new(path: PathBuf, saves: &Saves, roots: &[PathBuf]) -> Result<Self> {
        let parent = path.parent().ok_or("Choose a destination folder.")?;
        no_symlinks(parent)?;
        let parent = parent
            .canonicalize()
            .map_err(|_| "Destination folder is unavailable.")?;
        let name = PathBuf::from(path.file_name().ok_or("Choose a filename.")?);
        let path = parent.join(&name);
        saves.protect(&path, roots)?;
        #[cfg(windows)]
        let parent_guards = pin_windows_parents(&parent)?;
        let dir = Dir::open_ambient_dir(&parent, ambient_authority())
            .map_err(|_| "Cannot open destination folder.")?;
        // Identity comes from the retained directory, not a second path lookup.
        let parent_identity = same_file::Handle::from_file(
            dir.try_clone()
                .map_err(|_| "Cannot retain destination folder.")?
                .into_std_file(),
        )
        .map_err(|_| "Cannot identify destination folder.")?;
        let existing = Self::inspect(&dir, &name)?;
        let target = Self {
            path,
            dir,
            #[cfg(windows)]
            _parent_guards: parent_guards,
            parent_identity,
            name,
            existing,
        };
        target.validate(saves, roots)?;
        Ok(target)
    }

    fn inspect(dir: &Dir, name: &Path) -> Result<Existing> {
        match dir.symlink_metadata(name) {
            Ok(meta) => {
                if !meta.is_file() || meta.is_symlink() {
                    return Err("The destination must be a regular file, not a link.".into());
                }
                let mut options = cap_std::fs::OpenOptions::new();
                options.read(true).follow(FollowSymlinks::No);
                let file = dir
                    .open_with(name, &options)
                    .map_err(|_| "Cannot inspect destination.")?
                    .into_std();
                let metadata = file.metadata().map_err(|_| "Cannot inspect destination.")?;
                if !metadata.is_file()
                    || dir
                        .symlink_metadata(name)
                        .map_err(|_| "The destination changed.")?
                        .is_symlink()
                {
                    return Err("The destination changed.".into());
                }
                Ok(Some((
                    same_file::Handle::from_file(file)
                        .map_err(|_| "Cannot identify destination.")?,
                    metadata.len(),
                    metadata
                        .modified()
                        .map_err(|_| "Cannot inspect destination.")?,
                )))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(_) => Err("Cannot inspect destination.".into()),
        }
    }

    fn validate(&self, saves: &Saves, roots: &[PathBuf]) -> Result<()> {
        self.validate_locked(&saves.0 .0.lock().unwrap(), roots)
    }

    fn validate_locked(&self, state: &Activity, roots: &[PathBuf]) -> Result<()> {
        let parent = self.path.parent().unwrap();
        no_symlinks(parent)?;
        if same_file::Handle::from_path(parent).ok().as_ref() != Some(&self.parent_identity)
            || Self::inspect(&self.dir, &self.name)? != self.existing
        {
            return Err("The destination changed. Choose it again.".into());
        }
        Saves::protect_locked(state, &self.path, roots)
    }

    fn copy(
        &self,
        source: &mut impl Read,
        length: u64,
        saves: &Saves,
        roots: &[PathBuf],
    ) -> Result<()> {
        saves.check()?;
        self.validate(saves, roots)?;
        let mut random = [0; 16];
        getrandom::fill(&mut random).map_err(|_| "Secure randomness unavailable.")?;
        let name = format!(
            ".wenyi-{}.tmp",
            random
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        );
        let mut options = cap_std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        let mut file = self
            .dir
            .open_with(&name, &options)
            .map_err(|_| "Cannot create temporary export.")?;
        let temporary = Temporary {
            dir: &self.dir,
            name,
        };
        let mut buffer = [0; CHUNK];
        let mut copied = 0u64;
        loop {
            saves.check()?;
            let count = source
                .read(&mut buffer)
                .map_err(|_| "Export transfer interrupted; the destination was not changed.")?;
            if count == 0 {
                break;
            }
            copied += count as u64;
            if copied > length {
                return Err("Export length does not match.".into());
            }
            file.write_all(&buffer[..count])
                .map_err(|_| "Cannot write export; the destination was not changed.")?;
        }
        if copied != length {
            return Err("Export transfer was truncated; the destination was not changed.".into());
        }
        file.sync_all().map_err(|_| "Cannot flush export.")?;
        let identity = same_file::Handle::from_file(
            file.try_clone()
                .map_err(|_| "Cannot verify temporary export.")?
                .into_std(),
        )
        .map_err(|_| "Cannot verify temporary export.")?;
        drop(file);
        // Serialize all final checks and publication with other saves, source
        // reservations and shutdown. Never reacquire this mutex in validation.
        let state = saves.0 .0.lock().unwrap();
        if state.closing {
            return Err("Saving stopped because Wenyi is closing.".into());
        }
        self.validate_locked(&state, roots)?;
        if Self::inspect(&self.dir, Path::new(&temporary.name))?
            .as_ref()
            .map(|(handle, size, _)| (handle, *size))
            != Some((&identity, length))
        {
            return Err("The temporary export changed; the destination was not changed.".into());
        }
        // External namespace mutations are not serialized by this mutex.
        // An existing-file rename is not an atomic compare-and-swap: a hostile
        // process can still replace an entry after inspection. New destinations
        // use no-replace publication; no exchange-and-rollback is attempted.
        if self.existing.is_none() {
            self.publish_new(&temporary.name)
                .map_err(|_| "Cannot publish without replacing another file. Choose a different destination or filesystem.")?;
        } else {
            self.dir
                .rename(&temporary.name, &self.dir, &self.name)
                .map_err(|_| "Cannot publish export; the destination was not changed.")?;
        }
        Ok(())
    }

    fn publish_new(&self, temporary: &str) -> std::io::Result<()> {
        // No check-then-overwriting-rename fallback: a file appearing after the
        // confirmation/validation must remain untouched, including on FAT/exFAT.
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        {
            use std::{
                ffi::CString,
                os::unix::{ffi::OsStrExt, io::AsRawFd},
            };
            let from = CString::new(temporary)?;
            let to = CString::new(self.name.as_os_str().as_bytes())?;
            let directory = self.dir.as_raw_fd();
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
            let from: Vec<u16> = self
                .path
                .parent()
                .unwrap()
                .join(temporary)
                .as_os_str()
                .encode_wide()
                .chain(Some(0))
                .collect();
            let to: Vec<u16> = self.path.as_os_str().encode_wide().chain(Some(0)).collect();
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
            self.dir.hard_link(temporary, &self.dir, &self.name)
        }
    }
}

#[cfg(windows)]
fn pin_windows_parents(parent: &Path) -> Result<Vec<File>> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    parent
        .ancestors()
        .map(|path| {
            // Omit FILE_SHARE_DELETE so path-based MoveFileExW remains anchored:
            // neither the destination directory nor any ancestor can be renamed.
            let file = std::fs::OpenOptions::new()
                .access_mode(FILE_READ_ATTRIBUTES)
                .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
                .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
                .open(path)
                .map_err(|_| "Cannot retain destination folder.")?;
            if file
                .metadata()
                .map_err(|_| "Cannot inspect destination folder.")?
                .file_type()
                .is_symlink()
            {
                return Err("Choose a destination without symbolic links.".into());
            }
            Ok(file)
        })
        .collect()
}

struct Temporary<'a> {
    dir: &'a Dir,
    name: String,
}
impl Drop for Temporary<'_> {
    fn drop(&mut self) {
        let _ = self.dir.remove_file(&self.name);
    }
}

fn run(
    connection: Connection,
    saves: Saves,
    pid: String,
    options: Value,
    export_id: Option<u64>,
    dialog: &impl Dialog,
) -> Result<Option<Saved>> {
    let _pending = saves.reserve()?;
    if pid.is_empty()
        || !pid
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err("Invalid project identity.".into());
    }
    let desktop = format!("/desktop/projects/{pid}/exports");
    let value = match export_id {
        Some(id) => connection.json(&format!("{desktop}/{id}/plan"), None)?,
        None => connection.json(&format!("{desktop}/plan"), Some(&options))?,
    };
    let plan: Plan = serde_json::from_value(value).map_err(|_| "Invalid export plan.")?;
    if plan.filename.is_empty()
        || plan.filename.contains(['/', '\\', '\0'])
        || plan.extension.is_empty()
        || !plan.extension.bytes().all(|b| b.is_ascii_alphanumeric())
        || plan.protected_roots.is_empty()
    {
        return Err("Invalid export plan.".into());
    }
    let Some(path) = dialog.choose(&plan) else {
        return Ok(None);
    };
    let path = plan.destination(path);
    let target = Target::new(path, &saves, &plan.protected_roots)?;
    if target.existing.is_some() && !dialog.replace(&target.path) {
        return Ok(None);
    }
    let _active = saves.begin()?;
    target.validate(&saves, &plan.protected_roots)?;
    let id = match export_id {
        Some(id) => id,
        None => {
            let request = plan.request.as_ref().ok_or("Invalid export plan.")?;
            // This POST is not retried or deduplicated. Generate requests a new
            // snapshot; uncertain responses must be reconciled through history.
            let uncertain = |error| {
                format!("{error} Export creation may have succeeded. Check export history before generating again.")
            };
            let created = connection
                .json(&format!("/projects/{pid}/exports"), Some(request))
                .map_err(uncertain)?;
            created["export_id"]
                .as_u64()
                .ok_or_else(|| uncertain("Invalid export job.".into()))?
        }
    };
    save_created_export(&connection, &saves, &pid, &desktop, id, &target, &plan).map_err(
        |error| {
            format!("{error} Export ID {id}: check export history and use Save as to recover this export; Generate creates a new snapshot.")
        },
    )?;
    Ok(Some(Saved {
        path: target.path.to_string_lossy().into_owned(),
    }))
}

fn save_created_export(
    connection: &Connection,
    saves: &Saves,
    pid: &str,
    desktop: &str,
    id: u64,
    target: &Target,
    plan: &Plan,
) -> Result<()> {
    loop {
        saves.check()?;
        let list = connection.json(&format!("/projects/{pid}/exports"), None)?;
        let entry = list
            .as_array()
            .and_then(|items| items.iter().find(|item| item["id"].as_u64() == Some(id)))
            .ok_or("Export is no longer available.")?;
        match entry["status"].as_str() {
            Some("done") => break,
            Some("pending" | "queued" | "running") => {
                std::thread::sleep(Duration::from_millis(500))
            }
            _ => return Err("Export generation failed. Check export history.".into()),
        }
    }
    let (length, mut response) = connection.content(format!("{desktop}/{id}/content"), saves)?;
    target.copy(&mut response, length, saves, &plan.protected_roots)
}

#[tauri::command]
pub async fn native_export_save(
    window: tauri::WebviewWindow,
    project_id: String,
    options: Value,
    export_id: Option<u64>,
) -> Result<Option<Saved>> {
    if window.label() != "main" || !window.url().is_ok_and(|url| crate::trusted(&url)) {
        return Err("Native saving is unavailable.".into());
    }
    let raw = {
        let state = window.state::<Arc<Mutex<crate::State>>>();
        let state = state.lock().unwrap();
        if state.closing || state.error.is_some() {
            return Err("Local engine unavailable.".into());
        }
        state
            .connection
            .clone()
            .ok_or("Local engine unavailable.")?
    };
    let saves = window.state::<Saves>().inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        run(
            Connection::new(&raw)?,
            saves,
            project_id,
            options,
            export_id,
            &NativeDialog,
        )
    })
    .await
    .map_err(|_| "Native saving stopped unexpectedly.".to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io, net::TcpListener, thread};

    #[test]
    fn updates_reserve_idle_saves_without_interrupting_active_operations() {
        let saves = Saves::default();
        let active = saves.begin().unwrap();
        assert!(!saves.close_if_idle());
        assert!(saves.check().is_ok());
        drop(active);
        assert!(saves.close_if_idle());
        assert!(saves.begin().is_err());
        assert!(saves.check().is_err());
        saves.reopen();
        assert!(saves.begin().is_ok());
    }

    #[test]
    fn quit_can_cancel_planning_without_waiting_for_a_dialog() {
        let saves = Saves::default();
        let pending = saves.reserve().unwrap();
        assert!(!saves.close_if_idle());
        saves.close();
        saves.wait();
        assert!(saves.begin().is_err());
        drop(pending);
    }

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let mut bytes = [0; 16];
            getrandom::fill(&mut bytes).unwrap();
            let path = std::env::temp_dir().join(format!("wenyi-save-{:x?}", bytes));
            std::fs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
        fn file(&self, name: &str, content: &[u8]) -> PathBuf {
            let path = self.0.join(name);
            std::fs::write(&path, content).unwrap();
            path
        }
        fn clean(&self) {
            assert!(std::fs::read_dir(&self.0).unwrap().all(|e| !e
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".wenyi-")));
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    struct FakeDialog {
        path: Option<PathBuf>,
        replace: bool,
    }
    impl Dialog for FakeDialog {
        fn choose(&self, _: &Plan) -> Option<PathBuf> {
            self.path.clone()
        }
        fn replace(&self, _: &Path) -> bool {
            self.replace
        }
    }

    fn response(status: &str, body: &str, extra: &str) -> String {
        format!(
            "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n{extra}\r\n{body}",
            body.len()
        )
    }

    fn server(responses: Vec<String>) -> (Connection, thread::JoinHandle<Vec<String>>) {
        server_with_hook(responses, |_| {})
    }

    fn server_with_hook(
        responses: Vec<String>,
        hook: impl Fn(&str) + Send + 'static,
    ) -> (Connection, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let connection = Connection::new(
            &serde_json::json!({
                "apiBase": base, "token": "fixture-only"
            })
            .to_string(),
        )
        .unwrap();
        let task = thread::spawn(move || {
            let mut requests = Vec::new();
            for response in responses {
                let (mut socket, _) = listener.accept().unwrap();
                socket
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                let mut request = Vec::new();
                loop {
                    let mut buffer = [0; 4096];
                    let count = socket.read(&mut buffer).unwrap();
                    assert!(count > 0);
                    request.extend_from_slice(&buffer[..count]);
                    if let Some(end) = request.windows(4).position(|w| w == b"\r\n\r\n") {
                        let headers = String::from_utf8_lossy(&request[..end]).to_ascii_lowercase();
                        let length: usize = headers
                            .lines()
                            .find_map(|l| l.strip_prefix("content-length:"))
                            .unwrap_or("0")
                            .trim()
                            .parse()
                            .unwrap();
                        if request.len() >= end + 4 + length {
                            break;
                        }
                    }
                }
                let request = String::from_utf8(request).unwrap();
                hook(&request);
                requests.push(request);
                socket.write_all(response.as_bytes()).unwrap();
            }
            requests
        });
        (connection, task)
    }

    fn plan(root: &Path) -> String {
        serde_json::json!({
            "filename": "book.zh-bi.html.zip", "extension": "zip",
            "label": "HTML + assets (ZIP)",
            "protected_roots": [root],
            "request": {"format": "html", "bilingual": true}
        })
        .to_string()
    }

    #[test]
    fn cancel_and_declined_overwrite_do_not_create_jobs_or_files() {
        let fixture = Fixture::new();
        let protected = Fixture::new();
        let old = fixture.file("existing.html.zip", b"old");
        for path in [None, Some(old.clone())] {
            let (connection, task) = server(vec![response("200 OK", &plan(&protected.0), "")]);
            let result = run(
                connection,
                Saves::default(),
                "project-1".into(),
                serde_json::json!({}),
                None,
                &FakeDialog {
                    path,
                    replace: false,
                },
            )
            .unwrap();
            assert!(result.is_none());
            let requests = task.join().unwrap();
            assert_eq!(requests.len(), 1);
            assert!(requests[0].starts_with("POST /desktop/projects/project-1/exports/plan "));
            assert_eq!(std::fs::read(&old).unwrap(), b"old");
            fixture.clean();
        }
    }

    #[test]
    fn open_native_dialog_prevents_update_installation() {
        struct HeldDialog {
            opened: mpsc::Sender<()>,
            release: mpsc::Receiver<()>,
        }
        impl Dialog for HeldDialog {
            fn choose(&self, _: &Plan) -> Option<PathBuf> {
                self.opened.send(()).unwrap();
                self.release.recv_timeout(Duration::from_secs(5)).unwrap();
                None
            }
            fn replace(&self, _: &Path) -> bool {
                unreachable!()
            }
        }
        let protected = Fixture::new();
        let (connection, server) = server(vec![response("200 OK", &plan(&protected.0), "")]);
        let saves = Saves::default();
        let worker_saves = saves.clone();
        let (opened, visible) = mpsc::channel();
        let (release, dismissed) = mpsc::channel();
        let worker = thread::spawn(move || {
            run(
                connection,
                worker_saves,
                "project-1".into(),
                serde_json::json!({}),
                None,
                &HeldDialog {
                    opened,
                    release: dismissed,
                },
            )
        });
        visible.recv_timeout(Duration::from_secs(5)).unwrap();
        let install_allowed = saves.close_if_idle();
        release.send(()).unwrap();
        assert!(worker.join().unwrap().unwrap().is_none());
        assert_eq!(server.join().unwrap().len(), 1);
        assert!(
            !install_allowed,
            "An open native save dialog must block updates"
        );
        assert!(saves.close_if_idle());
    }

    #[test]
    fn uncertain_creation_is_not_retried_and_points_to_history() {
        for creation in [
            String::new(),
            response("200 OK", "invalid JSON", ""),
            response("200 OK", r#"{"unexpected":true}"#, ""),
        ] {
            let fixture = Fixture::new();
            let protected = Fixture::new();
            let path = fixture.file("saved.html.zip", b"old");
            let (connection, task) =
                server(vec![response("200 OK", &plan(&protected.0), ""), creation]);
            let error = run(
                connection,
                Saves::default(),
                "project-1".into(),
                serde_json::json!({}),
                None,
                &FakeDialog {
                    path: Some(path.clone()),
                    replace: true,
                },
            )
            .err()
            .unwrap();
            assert!(error.contains("may have succeeded"));
            assert!(error.contains("Check export history"));
            assert_eq!(task.join().unwrap().len(), 2);
            assert_eq!(std::fs::read(path).unwrap(), b"old");
        }
    }

    #[test]
    fn known_export_failures_recover_by_id_without_generating_again() {
        for failure in ["poll", "download", "local"] {
            let fixture = Fixture::new();
            let protected = Fixture::new();
            let path = fixture.file("saved.html.zip", b"old");
            let mut responses = vec![
                response("200 OK", &plan(&protected.0), ""),
                response("200 OK", r#"{"export_id":7}"#, ""),
            ];
            if failure == "poll" {
                responses.push(response("503 Unavailable", "", ""));
            } else {
                responses.push(response("200 OK", r#"[{"id":7,"status":"done"}]"#, ""));
                responses.push(if failure == "download" {
                    response("503 Unavailable", "", "")
                } else {
                    response("200 OK", "export", "")
                });
            }
            let modified = path.clone();
            let (connection, task) = server_with_hook(responses, move |request| {
                if failure == "local"
                    && request.starts_with("GET /desktop/projects/project-1/exports/7/content ")
                {
                    std::fs::write(&modified, b"externally changed").unwrap();
                }
            });
            let error = run(
                connection,
                Saves::default(),
                "project-1".into(),
                serde_json::json!({}),
                None,
                &FakeDialog {
                    path: Some(path.clone()),
                    replace: true,
                },
            )
            .err()
            .unwrap();
            assert!(error.contains("Export ID 7"));
            assert!(error.contains("Save as"));
            assert!(error.contains("Generate creates a new snapshot"));
            let requests = task.join().unwrap();
            assert_eq!(
                requests
                    .iter()
                    .filter(|r| r.starts_with("POST /projects/"))
                    .count(),
                1
            );
            assert_eq!(
                std::fs::read(path).unwrap(),
                if failure == "local" {
                    &b"externally changed"[..]
                } else {
                    &b"old"[..]
                }
            );
            fixture.clean();
        }
    }

    #[test]
    fn confirmed_generation_and_history_save_use_scoped_routes() {
        for history in [false, true] {
            let fixture = Fixture::new();
            let protected = Fixture::new();
            let path = fixture.file("saved.html.zip", b"old");
            let mut responses = vec![response("200 OK", &plan(&protected.0), "")];
            if !history {
                responses.push(response("200 OK", r#"{"export_id":7}"#, ""));
            }
            responses.push(response("200 OK", r#"[{"id":7,"status":"done"}]"#, ""));
            responses.push(response("200 OK", "complete", ""));
            let (connection, task) = server(responses);
            let saved = run(
                connection,
                Saves::default(),
                "project-1".into(),
                serde_json::json!({}),
                history.then_some(7),
                &FakeDialog {
                    path: Some(path.clone()),
                    replace: true,
                },
            )
            .unwrap()
            .unwrap();
            assert_eq!(PathBuf::from(saved.path), path);
            assert_eq!(std::fs::read(&path).unwrap(), b"complete");
            let requests = task.join().unwrap();
            assert_eq!(requests.len(), if history { 3 } else { 4 });
            assert!(requests.iter().all(|r| r
                .to_ascii_lowercase()
                .contains("authorization: bearer fixture-only")));
            assert!(requests
                .last()
                .unwrap()
                .starts_with("GET /desktop/projects/project-1/exports/7/content "));
            if history {
                assert!(requests.iter().all(|r| r.starts_with("GET ")));
            }
            fixture.clean();
        }
    }

    #[test]
    fn bounded_copy_keeps_old_destination_on_truncation_interruption_and_close() {
        let fixture = Fixture::new();
        let saves = Saves::default();
        let path = fixture.file("saved.txt", b"old");
        let target = Target::new(path.clone(), &saves, &[]).unwrap();
        assert!(target.copy(&mut &b"short"[..], 99, &saves, &[]).is_err());
        assert!(target.copy(&mut &b"too long"[..], 1, &saves, &[]).is_err());
        struct Interrupt;
        impl Read for Interrupt {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::other("interrupted"))
            }
        }
        assert!(target.copy(&mut Interrupt, 10, &saves, &[]).is_err());
        struct Closing(Saves);
        impl Read for Closing {
            fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
                assert!(buffer.len() <= CHUNK);
                self.0.close();
                buffer[0] = b'x';
                Ok(1)
            }
        }
        assert!(target
            .copy(&mut Closing(saves.clone()), 1, &saves, &[])
            .is_err());
        assert_eq!(std::fs::read(&path).unwrap(), b"old");
        fixture.clean();
        let saves = Saves::default();
        let target = Target::new(path.clone(), &saves, &[]).unwrap();
        let bytes = vec![b'a'; CHUNK * 3 + 1];
        struct Bounded<'a>(&'a [u8]);
        impl Read for Bounded<'_> {
            fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
                assert!(buffer.len() <= CHUNK);
                self.0.read(buffer)
            }
        }
        target
            .copy(&mut Bounded(&bytes), bytes.len() as u64, &saves, &[])
            .unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), bytes);
    }

    #[test]
    fn changed_destination_internal_workspace_and_source_identity_are_protected() {
        let fixture = Fixture::new();
        let saves = Saves::default();
        let path = fixture.file("source.txt", b"source");
        let retained = File::open(&path).unwrap();
        saves
            .protect_source(path.clone(), retained.try_clone().unwrap())
            .unwrap();
        saves.protect_source(path.clone(), retained).unwrap();
        assert_eq!(saves.0 .0.lock().unwrap().sources.len(), 1);
        assert!(Target::new(path.clone(), &saves, &[]).is_err());
        let alias = fixture.0.join("alias.txt");
        std::fs::hard_link(&path, &alias).unwrap();
        assert!(Target::new(alias, &saves, &[]).is_err());
        assert!(Target::new(
            fixture.0.join("new.txt"),
            &saves,
            std::slice::from_ref(&fixture.0)
        )
        .is_err());
        let other = fixture.file("other.txt", b"old");
        let target = Target::new(other.clone(), &saves, &[]).unwrap();
        std::fs::rename(&other, fixture.0.join("moved.txt")).unwrap();
        std::fs::write(&other, b"replacement").unwrap();
        assert!(target.copy(&mut &b"export"[..], 6, &saves, &[]).is_err());
        assert_eq!(std::fs::read(other).unwrap(), b"replacement");
        fixture.clean();
    }

    #[test]
    #[cfg(unix)]
    fn links_and_parent_replacement_are_rejected() {
        let fixture = Fixture::new();
        let saves = Saves::default();
        let path = fixture.file("original.txt", b"original");
        let link = fixture.0.join("link.txt");
        std::os::unix::fs::symlink(&path, &link).unwrap();
        assert!(Target::new(link, &saves, &[]).is_err());
        let dir = fixture.0.join("folder");
        std::fs::create_dir(&dir).unwrap();
        let target = Target::new(dir.join("out.txt"), &saves, &[]).unwrap();
        std::fs::rename(&dir, fixture.0.join("old-folder")).unwrap();
        std::fs::create_dir(&dir).unwrap();
        assert!(target.copy(&mut &b"x"[..], 1, &saves, &[]).is_err());
        assert!(!dir.join("out.txt").exists());
        assert!(!fixture.0.join("old-folder/out.txt").exists());
    }

    #[test]
    fn redirects_and_truncated_http_preserve_destination() {
        let forbidden = TcpListener::bind("127.0.0.1:0").unwrap();
        forbidden.set_nonblocking(true).unwrap();
        let extra = format!(
            "Location: http://{}/secret\r\n",
            forbidden.local_addr().unwrap()
        );
        let (connection, task) = server(vec![response("307 Temporary Redirect", "", &extra)]);
        assert!(connection.request("/export", None).is_err());
        task.join().unwrap();
        let (connection, task) = server(vec![response("307 Temporary Redirect", "", &extra)]);
        assert!(connection
            .content("/export".into(), &Saves::default())
            .is_err());
        task.join().unwrap();
        assert_eq!(
            forbidden.accept().unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        let fixture = Fixture::new();
        let path = fixture.file("saved.txt", b"old");
        let saves = Saves::default();
        let target = Target::new(path.clone(), &saves, &[]).unwrap();
        let (connection, task) = server(vec![
            "HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\nshort".into(),
        ]);
        let mut response = connection.request("/content", None).unwrap();
        assert!(target.copy(&mut response, 100, &saves, &[]).is_err());
        task.join().unwrap();
        assert_eq!(std::fs::read(path).unwrap(), b"old");
        fixture.clean();
    }

    #[test]
    fn only_private_loopback_connection_and_project_ids_are_accepted() {
        for base in [
            "https://127.0.0.1:80",
            "http://example.com:80",
            "http://127.0.0.1:8080/other",
            "http://user@127.0.0.1:8080",
            "http://127.0.0.1:8080/?q=x",
        ] {
            assert!(Connection::new(
                &serde_json::json!({"apiBase":base,"token":"test"}).to_string()
            )
            .is_err());
        }
        let raw = r#"{"apiBase":"http://127.0.0.1:12345","token":"test"}"#;
        for pid in ["", "../other", "a/b", "a?b"] {
            assert!(run(
                Connection::new(raw).unwrap(),
                Saves::default(),
                pid.into(),
                serde_json::json!({}),
                None,
                &FakeDialog {
                    path: None,
                    replace: false
                }
            )
            .is_err());
        }
    }

    #[test]
    fn selected_names_keep_real_format_extensions() {
        for (extension, filename) in [
            ("docx", "book.zh.docx"),
            ("epub", "book.en-bi.epub"),
            ("md", "book.en.md"),
            ("pdf", "book.zh-bi.pdf"),
            ("txt", "book.en.txt"),
            ("srt", "book.zh.srt"),
            ("zip", "book.zh-bi.html.zip"),
        ] {
            let plan = Plan {
                filename: filename.into(),
                extension: extension.into(),
                label: String::new(),
                request: None,
                protected_roots: vec![],
            };
            assert_eq!(
                plan.destination(PathBuf::from(filename)),
                PathBuf::from(filename)
            );
            let suffix = if extension == "zip" {
                "html.zip"
            } else {
                extension
            };
            assert_eq!(
                plan.destination(PathBuf::from("chosen")),
                PathBuf::from(format!("chosen.{suffix}"))
            );
        }
    }

    #[test]
    fn no_replace_publication_preserves_a_file_that_appears_after_validation() {
        let fixture = Fixture::new();
        let saves = Saves::default();
        let path = fixture.0.join("new.txt");
        let target = Target::new(path.clone(), &saves, &[]).unwrap();
        fixture.file("temporary", b"complete");
        std::fs::write(&path, b"arrived later").unwrap();
        assert!(target.publish_new("temporary").is_err());
        assert_eq!(std::fs::read(path).unwrap(), b"arrived later");
        assert_eq!(
            std::fs::read(fixture.0.join("temporary")).unwrap(),
            b"complete"
        );
        let new = fixture.0.join("other.txt");
        let target = Target::new(new.clone(), &saves, &[]).unwrap();
        target.publish_new("temporary").unwrap();
        assert_eq!(std::fs::read(new).unwrap(), b"complete");
    }

    #[test]
    fn close_does_not_wait_for_content_headers() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let raw = serde_json::json!({
            "apiBase":format!("http://{}", listener.local_addr().unwrap()), "token":"fixture"
        })
        .to_string();
        let (started, received) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let server = thread::spawn(move || {
            let (_socket, _) = listener.accept().unwrap();
            started.send(()).unwrap();
            released.recv_timeout(Duration::from_secs(5)).unwrap();
        });
        let saves = Saves::default();
        let worker_saves = saves.clone();
        let (done, completed) = mpsc::channel();
        let worker = thread::spawn(move || {
            let result = Connection::new(&raw)
                .unwrap()
                .content("/content".into(), &worker_saves);
            done.send(result.is_err()).unwrap();
        });
        received.recv_timeout(Duration::from_secs(5)).unwrap();
        saves.close();
        assert!(completed.recv_timeout(Duration::from_secs(1)).unwrap());
        release.send(()).unwrap();
        server.join().unwrap();
        worker.join().unwrap();
    }

    #[test]
    fn proxy_probe() {
        if std::env::var_os("WENYI_NATIVE_EXPORT_PROXY_TEST").is_none() {
            return;
        }
        let (connection, task) = server(vec![response("200 OK", "{}", "")]);
        assert_eq!(
            connection.json("/probe", None).unwrap(),
            serde_json::json!({})
        );
        task.join().unwrap();
        let (connection, task) = server(vec![response("200 OK", "content", "")]);
        let (length, mut stream) = connection
            .content("/probe".into(), &Saves::default())
            .unwrap();
        assert_eq!(length, 7);
        let mut content = String::new();
        stream.read_to_string(&mut content).unwrap();
        assert_eq!(content, "content");
        task.join().unwrap();
    }

    #[test]
    fn changed_temporary_file_is_never_published() {
        let fixture = Fixture::new();
        let path = fixture.file("saved.txt", b"old");
        let saves = Saves::default();
        let target = Target::new(path.clone(), &saves, &[]).unwrap();
        struct Tamper<'a> {
            root: &'a Path,
            done: bool,
        }
        impl Read for Tamper<'_> {
            fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
                if self.done {
                    return Ok(0);
                }
                self.done = true;
                let temporary = std::fs::read_dir(self.root)?
                    .map(|e| e.unwrap().path())
                    .find(|p| {
                        p.file_name()
                            .unwrap()
                            .to_string_lossy()
                            .starts_with(".wenyi-")
                    })
                    .unwrap();
                std::fs::rename(&temporary, self.root.join("displaced.tmp"))?;
                std::fs::write(temporary, b"planted")?;
                buffer[0] = b'x';
                Ok(1)
            }
        }
        assert!(target
            .copy(
                &mut Tamper {
                    root: &fixture.0,
                    done: false
                },
                1,
                &saves,
                &[]
            )
            .is_err());
        assert_eq!(std::fs::read(path).unwrap(), b"old");
        fixture.clean();
    }

    #[test]
    fn retained_source_identity_does_not_reopen_a_replaced_path() {
        let fixture = Fixture::new();
        let path = fixture.file("source.txt", b"source");
        let file = File::open(&path).unwrap();
        let moved = fixture.0.join("moved.txt");
        std::fs::rename(&path, &moved).unwrap();
        std::fs::write(&path, b"replacement").unwrap();
        let saves = Saves::default();
        saves.protect_source(path.clone(), file).unwrap();
        assert!(Target::new(path, &saves, &[]).is_err());
        assert!(Target::new(moved, &saves, &[]).is_err());
    }

    #[test]
    fn competing_confirmed_targets_publish_only_one_snapshot() {
        struct Ready<'a> {
            barrier: &'a std::sync::Barrier,
            bytes: &'a [u8],
        }
        impl Read for Ready<'_> {
            fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
                if self.bytes.is_empty() {
                    self.barrier.wait();
                    return Ok(0);
                }
                self.bytes.read(buffer)
            }
        }
        for _ in 0..16 {
            let fixture = Fixture::new();
            let path = fixture.file("destination.txt", b"old");
            let saves = Saves::default();
            // Both targets represent confirmation of exactly the same old file.
            let first = Target::new(path.clone(), &saves, &[]).unwrap();
            let second = Target::new(path.clone(), &saves, &[]).unwrap();
            let barrier = std::sync::Barrier::new(2);
            let (a, b) = std::thread::scope(|scope| {
                let save = |target: Target, bytes: &[u8]| {
                    target.copy(
                        &mut Ready {
                            barrier: &barrier,
                            bytes,
                        },
                        1,
                        &saves,
                        &[],
                    )
                };
                let a = scope.spawn(move || save(first, b"a"));
                let b = scope.spawn(move || save(second, b"b"));
                (a.join().unwrap(), b.join().unwrap())
            });
            assert_ne!(a.is_ok(), b.is_ok());
            assert_eq!(
                std::fs::read(path).unwrap(),
                if a.is_ok() { b"a" } else { b"b" }
            );
            let error = if let Err(error) = a {
                error
            } else {
                b.unwrap_err()
            };
            assert!(error.contains("destination changed"));
            fixture.clean();
        }
    }

    #[test]
    fn hardlink_aliases_remain_protected_after_replacement() {
        let fixture = Fixture::new();
        let first = fixture.file("first.txt", b"source");
        let second = fixture.0.join("second.txt");
        std::fs::hard_link(&first, &second).unwrap();
        let saves = Saves::default();
        for path in [&first, &second] {
            saves
                .protect_source(path.clone(), File::open(path).unwrap())
                .unwrap();
        }
        assert_eq!(saves.source_count(), 1);
        assert_eq!(saves.0 .0.lock().unwrap().aliases.len(), 2);
        std::fs::remove_file(&second).unwrap();
        std::fs::write(&second, b"replacement").unwrap();
        assert!(Target::new(second, &saves, &[]).is_err());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn source_ledger_does_not_retain_file_descriptors() {
        let fixture = Fixture::new();
        let saves = Saves::default();
        for i in 0..64 {
            let path = fixture.file(&format!("source-{i}.txt"), b"source");
            saves
                .protect_source(path.clone(), File::open(path).unwrap())
                .unwrap();
        }
        assert_eq!(saves.source_count(), 64);
        // Scope the assertion to our files so parallel tests cannot affect it.
        assert!(!std::fs::read_dir("/proc/self/fd").unwrap().any(|entry| {
            entry
                .ok()
                .and_then(|entry| std::fs::read_link(entry.path()).ok())
                .is_some_and(|path| path.starts_with(&fixture.0))
        }));
    }

    #[cfg(windows)]
    #[test]
    fn windows_case_variants_protect_replaced_source_aliases() {
        let fixture = Fixture::new();
        let path = fixture.file("Source.TXT", b"source");
        let saves = Saves::default();
        saves
            .protect_source(path.clone(), File::open(&path).unwrap())
            .unwrap();
        std::fs::remove_file(&path).unwrap();
        let replacement = fixture.file("source.txt", b"replacement");
        assert!(Target::new(replacement, &saves, &[]).is_err());
        assert!(same_alias(
            Path::new(r"\\?\C:\Folder\É.txt"),
            Path::new(r"\\?\c:\FOLDER\é.TXT")
        ));
    }

    #[test]
    fn system_proxy_cannot_observe_local_requests() {
        let proxy = TcpListener::bind("127.0.0.1:0").unwrap();
        proxy.set_nonblocking(true).unwrap();
        let address = format!("http://{}", proxy.local_addr().unwrap());
        // Use a subprocess instead of mutating process-global environment while
        // other HTTP tests are running in parallel.
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "native_export::tests::proxy_probe"])
            .env("WENYI_NATIVE_EXPORT_PROXY_TEST", "1")
            .env("HTTP_PROXY", &address)
            .env("http_proxy", &address)
            .env("ALL_PROXY", &address)
            .env("all_proxy", &address)
            .env("NO_PROXY", "")
            .env("no_proxy", "")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(
            proxy.accept().unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }
}
