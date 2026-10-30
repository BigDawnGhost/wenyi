//! OS-drop-only, expiring capabilities. No command accepts a filesystem path.
use std::{
    collections::HashMap,
    fs::{File, Metadata, OpenOptions},
    io::{self, Read},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant, SystemTime},
};

use serde::Serialize;
use tauri::Manager;

const TTL: Duration = Duration::from_secs(15 * 60);
const MAX_GRANTS: usize = 2;
const CHUNK: usize = 64 * 1024;
type Result<T> = std::result::Result<T, String>;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Source {
    kind: &'static str,
    handle: String,
    name: String,
    size: u64,
}

#[derive(PartialEq, Eq)]
struct Stamp {
    size: u64,
    modified: SystemTime,
    #[cfg(unix)]
    changed: (i64, i64),
}

impl Stamp {
    fn read(meta: &Metadata) -> io::Result<Self> {
        #[cfg(unix)]
        use std::os::unix::fs::MetadataExt;
        Ok(Self {
            size: meta.len(),
            modified: meta.modified()?,
            #[cfg(unix)]
            changed: (meta.ctime(), meta.ctime_nsec()),
        })
    }
}

fn open_regular(path: &Path) -> io::Result<File> {
    if !path.symlink_metadata()?.file_type().is_file() {
        return Err(io::Error::other(
            "Select a regular file, not a directory or link.",
        ));
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x00200000); // FILE_FLAG_OPEN_REPARSE_POINT
    }
    let file = options.open(path)?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::other("Select a regular file."));
    }
    Ok(file)
}

struct Grant {
    source: Source,
    path: PathBuf,
    file: File,
    identity: same_file::Handle,
    stamp: Stamp,
    expires: Instant,
}

impl Grant {
    fn new(path: &Path) -> Result<Self> {
        let extension = path.extension().and_then(|s| s.to_str()).unwrap_or("");
        if !matches!(
            extension.to_ascii_lowercase().as_str(),
            "epub"
                | "fb2"
                | "txt"
                | "text"
                | "md"
                | "markdown"
                | "html"
                | "htm"
                | "pdf"
                | "docx"
                | "srt"
        ) {
            return Err("Unsupported input format.".into());
        }
        let file = open_regular(path).map_err(|_| "Select a readable regular file.")?;
        let stamp = Stamp::read(&file.metadata().map_err(|_| "Cannot inspect source file.")?)
            .map_err(|_| "Cannot inspect source file.")?;
        if stamp.size == 0 {
            return Err("Source file is empty.".into());
        }
        let mut random = [0; 32];
        getrandom::fill(&mut random).map_err(|_| "Secure randomness unavailable.")?;
        let identity = same_file::Handle::from_file(
            file.try_clone().map_err(|_| "Cannot retain source file.")?,
        )
        .map_err(|_| "Cannot identify source file.")?;
        let grant = Self {
            source: Source {
                kind: "native",
                handle: random.iter().map(|b| format!("{b:02x}")).collect(),
                name: path
                    .file_name()
                    .and_then(|s| s.to_str())
                    .ok_or("Source filename is not valid UTF-8.")?
                    .into(),
                size: stamp.size,
            },
            path: path.into(),
            file,
            identity,
            stamp,
            expires: Instant::now() + TTL,
        };
        grant
            .validate()
            .map_err(|_| "Source file changed. Drop it again.")?;
        Ok(grant)
    }

    fn validate(&self) -> io::Result<()> {
        let current = open_regular(&self.path)?;
        if Stamp::read(&self.file.metadata()?)? != self.stamp
            || same_file::Handle::from_file(current)? != self.identity
        {
            return Err(io::Error::other("Source file changed. Drop it again."));
        }
        Ok(())
    }
}

#[derive(Default)]
pub struct Grants(Arc<Mutex<HashMap<String, Grant>>>, Arc<AtomicBool>);

struct UploadGuard(Arc<AtomicBool>);

impl Drop for UploadGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

impl Grants {
    fn begin_upload(&self) -> Result<UploadGuard> {
        self.1
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| "A source upload is already running.")?;
        Ok(UploadGuard(self.1.clone()))
    }

    pub fn start_expiry(&self) {
        let grants = Arc::downgrade(&self.0);
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_secs(30));
            let Some(grants) = grants.upgrade() else {
                break;
            };
            grants
                .lock()
                .unwrap()
                .retain(|_, grant| grant.expires > Instant::now());
        });
    }

    // Only the Rust OS Drop event handler calls this method.
    fn drop_files(&self, paths: &[PathBuf]) -> Result<Source> {
        if self.1.load(Ordering::SeqCst) {
            return Err("A source upload is already running.".into());
        }
        if paths.len() != 1 {
            return Err("Select exactly one source file.".into());
        }
        let mut grants = self.0.lock().unwrap();
        grants.retain(|_, grant| grant.expires > Instant::now());
        if grants.len() >= MAX_GRANTS {
            return Err("Too many pending files. Clear the selection and try again.".into());
        }
        let grant = Grant::new(&paths[0])?;
        let source = grant.source.clone();
        grants.insert(source.handle.clone(), grant);
        Ok(source)
    }

    fn take(&self, handle: &str) -> Result<Grant> {
        let mut grants = self.0.lock().unwrap();
        grants.retain(|_, grant| grant.expires > Instant::now());
        let grant = grants
            .remove(handle)
            .ok_or("File selection expired. Drop it again.")?;
        grant
            .validate()
            .map_err(|_| "Source file changed. Drop it again.")?;
        Ok(grant)
    }

    pub fn clear(&self) {
        self.0.lock().unwrap().clear();
    }
}

struct SourceReader {
    grant: Grant,
    remaining: u64,
}

impl Read for SourceReader {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        self.grant.validate()?;
        let length = buffer.len().min(self.remaining.min(CHUNK as u64) as usize);
        if length == 0 {
            return Ok(0);
        }
        let count = self.grant.file.read(&mut buffer[..length])?;
        if count == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "Source file changed.",
            ));
        }
        self.grant.validate()?;
        self.remaining -= count as u64;
        Ok(count)
    }
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct Connection {
    api_base: String,
    token: String,
}

fn upload(grant: Grant, connection: &str, project: serde_json::Value) -> Result<serde_json::Value> {
    let connection: Connection =
        serde_json::from_str(connection).map_err(|_| "Local service is unavailable.")?;
    let base =
        reqwest::Url::parse(&connection.api_base).map_err(|_| "Invalid local service address.")?;
    if base.scheme() != "http"
        || base.host_str() != Some("127.0.0.1")
        || base.port().is_none()
        || !base.username().is_empty()
        || base.password().is_some()
        || base.path() != "/"
        || base.query().is_some()
        || base.fragment().is_some()
    {
        return Err("Invalid local service address.".into());
    }
    grant
        .validate()
        .map_err(|_| "Source file changed. Drop it again.")?;
    let size = grant.source.size;
    let name = grant.source.name.clone();
    let file = reqwest::blocking::multipart::Part::reader_with_length(
        SourceReader {
            grant,
            remaining: size,
        },
        size,
    )
    .file_name(name);
    let form = reqwest::blocking::multipart::Form::new()
        .text("project", project.to_string())
        .part("file", file);
    let client = reqwest::blocking::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .timeout(Duration::from_secs(60 * 60))
        .build()
        .map_err(|_| "Could not initialize local upload.")?;
    let response = client
        .post(base.join("/projects").unwrap())
        .bearer_auth(connection.token)
        .multipart(form)
        .send()
        .map_err(|_| "Local upload failed. Drop the source again to retry.")?;
    if !response.status().is_success() {
        return Err(format!(
            "Local upload failed (HTTP {}). Drop the source again to retry.",
            response.status().as_u16()
        ));
    }
    let mut bytes = Vec::new();
    response
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "Could not read local upload result.")?;
    if bytes.len() > 1024 * 1024 {
        return Err("Local upload response was too large.".into());
    }
    serde_json::from_slice(&bytes).map_err(|_| "Invalid local upload response.".into())
}

fn trusted_window(window: &tauri::WebviewWindow) -> Result<()> {
    if window.label() != "main" || !window.url().is_ok_and(|url| crate::trusted(&url)) {
        return Err("Untrusted window.".into());
    }
    Ok(())
}

fn upload_protected(
    grant: Grant,
    connection: &str,
    project: serde_json::Value,
    saves: &crate::native_export::Saves,
) -> Result<serde_json::Value> {
    let source_path = grant
        .path
        .canonicalize()
        .map_err(|_| "Cannot identify source.")?;
    let source_file = grant
        .file
        .try_clone()
        .map_err(|_| "Cannot retain source.")?;
    saves.protect_source(source_path, source_file)?;
    upload(grant, connection, project)
}

#[tauri::command]
pub fn native_drop_release(
    window: tauri::WebviewWindow,
    grants: tauri::State<'_, Grants>,
    handle: String,
) -> Result<()> {
    trusted_window(&window)?;
    grants.0.lock().unwrap().remove(&handle);
    Ok(())
}

#[tauri::command]
pub async fn native_drop_upload(
    window: tauri::WebviewWindow,
    grants: tauri::State<'_, Grants>,
    state: tauri::State<'_, Arc<Mutex<crate::State>>>,
    handle: String,
    project: serde_json::Value,
) -> Result<serde_json::Value> {
    trusted_window(&window)?;
    let connection = {
        let state = state.lock().unwrap();
        if state.closing || state.error.is_some() {
            return Err("Local service is unavailable.".into());
        }
        state
            .connection
            .clone()
            .ok_or("Local service is unavailable.")?
    };
    let guard = grants.begin_upload()?;
    let grant = grants.take(&handle)?;
    let saves = window
        .state::<crate::native_export::Saves>()
        .inner()
        .clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = guard;
        upload_protected(grant, &connection, project, &saves)
    })
    .await
    .map_err(|_| "Local upload failed.")?
}

fn physical_drag_position(
    position: &tauri::PhysicalPosition<f64>,
    platform: &str,
    scale_factor: f64,
) -> tauri::PhysicalPosition<f64> {
    // Wry 0.57 supplies GTK widget/Cocoa point coordinates, but physical
    // WebView2 coordinates. Normalize once before the frontend divides by DPR.
    let scale = if matches!(platform, "linux" | "macos") {
        scale_factor
    } else {
        1.0
    };
    tauri::PhysicalPosition::new(position.x * scale, position.y * scale)
}

pub fn event(window: &tauri::WebviewWindow, event: &tauri::DragDropEvent) {
    if trusted_window(window).is_err() {
        return;
    }
    if window
        .state::<Arc<Mutex<crate::State>>>()
        .lock()
        .unwrap()
        .closing
    {
        return;
    }
    let position = |p: &tauri::PhysicalPosition<f64>| {
        let p = physical_drag_position(
            p,
            std::env::consts::OS,
            window.scale_factor().unwrap_or(1.0),
        );
        serde_json::json!({"x": p.x, "y": p.y})
    };
    let detail = match event {
        tauri::DragDropEvent::Enter { position: p, .. } => {
            serde_json::json!({"kind": "enter", "position": position(p)})
        }
        tauri::DragDropEvent::Over { position: p } => {
            serde_json::json!({"kind": "over", "position": position(p)})
        }
        tauri::DragDropEvent::Leave => serde_json::json!({"kind": "leave"}),
        tauri::DragDropEvent::Drop { paths, position: p } => {
            match window.state::<Grants>().drop_files(paths) {
                Ok(source) => {
                    serde_json::json!({"kind": "drop", "source": source, "position": position(p)})
                }
                Err(message) => {
                    serde_json::json!({"kind": "error", "message": message, "position": position(p)})
                }
            }
        }
        _ => return,
    };
    let _ = window.eval(format!(
        "window.dispatchEvent(new CustomEvent('wenyi:native-drag',{{cancelable:true,detail:{detail}}}));"
    ));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::Write, net::TcpListener, thread};

    #[test]
    fn native_drag_positions_use_one_physical_pixel_contract() {
        let position = tauri::PhysicalPosition::new(320.0, 180.0);
        for platform in ["linux", "macos"] {
            assert_eq!(physical_drag_position(&position, platform, 1.0), position);
            assert_eq!(
                physical_drag_position(&position, platform, 2.0),
                tauri::PhysicalPosition::new(640.0, 360.0)
            );
        }
        assert_eq!(physical_drag_position(&position, "windows", 2.0), position);
    }

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let mut random = [0; 16];
            getrandom::fill(&mut random).unwrap();
            let root = std::env::temp_dir().join(format!("wenyi-drop-{random:02x?}"));
            std::fs::create_dir(&root).unwrap();
            Self(root)
        }
        fn file(&self, name: &str, data: &[u8]) -> PathBuf {
            let path = self.0.join(name);
            std::fs::write(&path, data).unwrap();
            path
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn only_single_nonempty_supported_regular_files_create_bounded_capabilities() {
        let fixture = Fixture::new();
        let valid = fixture.file("book.TXT", b"source");
        let empty = fixture.file("empty.txt", b"");
        let unknown = fixture.file("program.exe", b"source");
        let grants = Grants::default();
        for paths in [
            vec![],
            vec![valid.clone(), valid.clone()],
            vec![empty],
            vec![unknown],
            vec![fixture.0.clone()],
        ] {
            assert!(grants.drop_files(&paths).is_err());
        }
        assert!(grants.take("invented-handle").is_err());
        let source = grants.drop_files(std::slice::from_ref(&valid)).unwrap();
        assert_eq!(source.name, "book.TXT");
        assert_eq!(source.size, 6);
        assert_eq!(source.handle.len(), 64);
        assert!(!serde_json::to_string(&source)
            .unwrap()
            .contains(fixture.0.to_str().unwrap()));
        for _ in 1..MAX_GRANTS {
            grants.drop_files(std::slice::from_ref(&valid)).unwrap();
        }
        assert!(grants.drop_files(std::slice::from_ref(&valid)).is_err());
        grants.take(&source.handle).unwrap();
        assert!(grants.take(&source.handle).is_err());
        grants.clear();
        assert!(grants.0.lock().unwrap().is_empty());
    }

    #[test]
    fn expiry_and_source_changes_reject_upload() {
        let fixture = Fixture::new();
        let path = fixture.file("source.txt", b"original");
        let grants = Grants::default();
        let source = grants.drop_files(std::slice::from_ref(&path)).unwrap();
        grants
            .0
            .lock()
            .unwrap()
            .get_mut(&source.handle)
            .unwrap()
            .expires = Instant::now();
        assert!(grants.take(&source.handle).is_err());
        let source = grants.drop_files(std::slice::from_ref(&path)).unwrap();
        std::fs::write(&path, b"changed size").unwrap();
        assert!(grants.take(&source.handle).is_err());
        let source = grants.drop_files(std::slice::from_ref(&path)).unwrap();
        std::fs::rename(&path, fixture.0.join("old.txt")).unwrap();
        std::fs::write(&path, b"changed size").unwrap();
        assert!(grants.take(&source.handle).is_err());
    }

    #[test]
    fn uploading_blocks_more_uploads_and_new_grants_until_completion() {
        let fixture = Fixture::new();
        let path = fixture.file("source.txt", b"source");
        let grants = Grants::default();
        let guard = grants.begin_upload().unwrap();
        assert!(grants.begin_upload().is_err());
        assert!(grants.drop_files(std::slice::from_ref(&path)).is_err());
        drop(guard);
        grants.drop_files(std::slice::from_ref(&path)).unwrap();
        grants.begin_upload().unwrap();
    }

    #[test]
    #[cfg(unix)]
    fn symlinks_and_symlink_replacements_are_rejected() {
        let fixture = Fixture::new();
        let path = fixture.file("source.txt", b"original");
        let link = fixture.0.join("link.txt");
        std::os::unix::fs::symlink(&path, &link).unwrap();
        assert!(Grant::new(&link).is_err());
        let grant = Grant::new(&path).unwrap();
        std::fs::rename(&path, fixture.0.join("old.txt")).unwrap();
        std::os::unix::fs::symlink(fixture.0.join("old.txt"), &path).unwrap();
        assert!(grant.validate().is_err());
    }

    #[test]
    fn streaming_is_bounded_and_detects_midstream_changes() {
        let fixture = Fixture::new();
        let data = vec![b'x'; CHUNK * 2 + 7];
        let path = fixture.file("source.txt", &data);
        let grant = Grant::new(&path).unwrap();
        let mut reader = SourceReader {
            grant,
            remaining: data.len() as u64,
        };
        let mut buffer = vec![0; CHUNK * 3];
        assert_eq!(reader.read(&mut buffer).unwrap(), CHUNK);
        std::fs::write(&path, b"modified").unwrap();
        assert!(reader.read(&mut buffer).is_err());
    }

    #[test]
    fn readonly_source_streams_exactly_without_modification() {
        let fixture = Fixture::new();
        let data = vec![b'z'; CHUNK * 2 + 13];
        let path = fixture.file("source.txt", &data);
        let mut permissions = std::fs::metadata(&path).unwrap().permissions();
        permissions.set_readonly(true);
        std::fs::set_permissions(&path, permissions).unwrap();
        let grant = Grant::new(&path).unwrap();
        let mut reader = SourceReader {
            grant,
            remaining: data.len() as u64,
        };
        let mut received = Vec::new();
        reader.read_to_end(&mut received).unwrap();
        assert_eq!(received, data);
        assert_eq!(std::fs::read(&path).unwrap(), data);
    }

    fn fake_server(status: &str, body: &str, extra: &str) -> (String, thread::JoinHandle<Vec<u8>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!("http://{}", listener.local_addr().unwrap());
        let response = if status.is_empty() {
            // Consume the complete POST, then lose the response after commit.
            String::new()
        } else {
            format!(
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n{extra}\r\n{body}",
                body.len()
            )
        };
        let task = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(10)))
                .unwrap();
            let mut received = Vec::new();
            let mut buffer = [0; 4096];
            loop {
                let count = socket.read(&mut buffer).unwrap();
                assert!(count > 0);
                received.extend_from_slice(&buffer[..count]);
                if let Some(end) = received.windows(4).position(|w| w == b"\r\n\r\n") {
                    let headers = String::from_utf8_lossy(&received[..end]).to_ascii_lowercase();
                    let length: usize = headers
                        .lines()
                        .find_map(|line| line.strip_prefix("content-length:"))
                        .unwrap()
                        .trim()
                        .parse()
                        .unwrap();
                    if received.len() >= end + 4 + length {
                        break;
                    }
                }
            }
            socket.write_all(response.as_bytes()).unwrap();
            received
        });
        (address, task)
    }

    #[test]
    fn multipart_uses_existing_endpoint_and_private_credentials() {
        let fixture = Fixture::new();
        let path = fixture.file("source.txt", b"original source");
        let (address, server) = fake_server("201 Created", r#"{"id":"created"}"#, "");
        let connection =
            serde_json::json!({"apiBase": address, "token": "test-only-token"}).to_string();
        let result = upload(
            Grant::new(&path).unwrap(),
            &connection,
            serde_json::json!({"name":"Example"}),
        )
        .unwrap();
        assert_eq!(result["id"], "created");
        let request = String::from_utf8(server.join().unwrap()).unwrap();
        assert!(request.starts_with("POST /projects HTTP/1.1\r\n"));
        assert!(request
            .to_ascii_lowercase()
            .contains("authorization: bearer test-only-token"));
        assert!(request.contains("name=\"project\""));
        assert!(request.contains(r#"{"name":"Example"}"#));
        assert!(request.contains("name=\"file\"; filename=\"source.txt\""));
        assert!(request.contains("original source"));
    }

    #[test]
    fn errors_and_redirects_never_forward_credentials_or_file() {
        let fixture = Fixture::new();
        let path = fixture.file("source.txt", b"source");
        let forbidden = TcpListener::bind("127.0.0.1:0").unwrap();
        forbidden.set_nonblocking(true).unwrap();
        for status in ["422 Unprocessable Entity", "307 Temporary Redirect"] {
            let location = format!(
                "Location: http://{}/stolen\r\n",
                forbidden.local_addr().unwrap()
            );
            let (address, server) = fake_server(status, "untrusted private error", &location);
            let connection =
                serde_json::json!({"apiBase": address, "token": "test-only-token"}).to_string();
            let error = upload(
                Grant::new(&path).unwrap(),
                &connection,
                serde_json::json!({}),
            )
            .unwrap_err();
            assert!(error.contains(&status[..3]));
            assert!(!error.contains("private"));
            server.join().unwrap();
            assert_eq!(
                forbidden.accept().unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
        }
    }

    #[test]
    fn attempted_uploads_retain_deduplicated_save_protection() {
        let fixture = Fixture::new();
        let path = fixture.file("source.txt", b"source");
        let saves = crate::native_export::Saves::default();
        let unused = Grant::new(&path).unwrap();
        drop(unused);
        assert_eq!(saves.source_count(), 0);
        for (status, body, success) in [
            ("200 OK", "malformed committed response", false),
            ("", "", false),
            ("503 Service Unavailable", "unavailable", false),
            ("200 OK", r#"{"id":"created"}"#, true),
        ] {
            let (address, server) = fake_server(status, body, "");
            let raw = serde_json::json!({"apiBase":address, "token":"fixture"}).to_string();
            let result = upload_protected(
                Grant::new(&path).unwrap(),
                &raw,
                serde_json::json!({}),
                &saves,
            );
            assert_eq!(result.is_ok(), success);
            server.join().unwrap();
            assert_eq!(saves.source_count(), 1);
            #[cfg(target_os = "linux")]
            assert!(!std::fs::read_dir("/proc/self/fd").unwrap().any(|entry| {
                entry
                    .ok()
                    .and_then(|entry| std::fs::read_link(entry.path()).ok())
                    .is_some_and(|path| path.starts_with(&fixture.0))
            }));
        }
    }

    #[test]
    fn source_identity_and_alias_caps_reject_before_any_post() {
        use crate::native_export::{Saves, MAX_SOURCE_ALIASES, MAX_SOURCE_IDENTITIES};
        for aliases in [false, true] {
            let fixture = Fixture::new();
            let saves = Saves::default();
            let source = fixture.file("source.txt", b"source");
            let cap = if aliases {
                MAX_SOURCE_ALIASES
            } else {
                MAX_SOURCE_IDENTITIES
            };
            for i in 0..cap {
                let path = fixture.0.join(format!("{i}.txt"));
                if aliases {
                    std::fs::hard_link(&source, &path).unwrap();
                } else {
                    std::fs::write(&path, b"source").unwrap();
                }
                saves
                    .protect_source(path.clone(), File::open(&path).unwrap())
                    .unwrap();
                if aliases {
                    // The ledger retains paths after unlinking; avoid exceeding
                    // NTFS's per-file hard-link limit while filling its alias cap.
                    std::fs::remove_file(path).unwrap();
                }
            }
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            listener.set_nonblocking(true).unwrap();
            let raw = serde_json::json!({
                "apiBase": format!("http://{}", listener.local_addr().unwrap()),
                "token": "fixture"
            })
            .to_string();
            let error = upload_protected(
                Grant::new(&source).unwrap(),
                &raw,
                serde_json::json!({}),
                &saves,
            )
            .unwrap_err();
            assert!(error.contains("limit reached"));
            assert!(error.contains("No upload was started"));
            assert_eq!(
                listener.accept().unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
            assert_eq!(saves.source_count(), if aliases { 1 } else { cap });
            // Already reserved identity/alias pairs remain usable at capacity.
            let known = fixture.0.join("0.txt");
            if aliases {
                std::fs::hard_link(&source, &known).unwrap();
            }
            saves
                .protect_source(known.clone(), File::open(known).unwrap())
                .unwrap();
        }
    }
}
