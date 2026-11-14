//! A single owner for the private server and its anonymous protocol pipes.
use std::{
    io::{BufRead, BufReader, Read, Write},
    path::Path,
    process::{Child, Command, ExitStatus, Stdio},
    sync::mpsc::{self, Receiver},
    time::{Duration, Instant},
};

use crate::diagnostics::{Diagnostics, Event, Source};
use serde_json::json;

#[derive(Debug, PartialEq, Eq)]
pub enum Failure {
    Setup(&'static str),
    Spawn(Option<i32>),
    InvalidProtocol,
    ReadProtocol,
    ProtocolEof,
    ReadyTimeout,
    ChannelClosed,
}

impl Failure {
    fn reason(&self) -> &'static str {
        match self {
            Self::Setup(_) => "setup_failed",
            Self::Spawn(_) => "spawn_failed",
            Self::InvalidProtocol => "protocol_invalid",
            Self::ReadProtocol => "protocol_read_failed",
            Self::ProtocolEof => "protocol_eof",
            Self::ReadyTimeout => "ready_timeout",
            Self::ChannelClosed => "channel_closed",
        }
    }

    pub fn report(&self, diagnostics: &Diagnostics) -> &'static str {
        diagnostics.record(
            Source::Native,
            Event::EngineFailed,
            json!({
                "stage": "bootstrap", "reason": self.reason(),
                "os_error": match self { Self::Spawn(code) => *code, _ => None },
            }),
        );
        match self {
            Self::Setup(message) => message,
            Self::Spawn(_) => "Could not start local engine.",
            Self::ReadyTimeout => {
                "The local engine timed out during startup. Export a diagnostic report."
            }
            _ => "The local engine did not become ready. Export a diagnostic report.",
        }
    }
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Ready {
    protocol: u8,
    port: u16,
    pid: u32,
}

fn ready(line: &str, pid: u32) -> Result<u16, &'static str> {
    let payload = line
        .strip_prefix("WENYI_READY ")
        .ok_or("Invalid ready protocol")?;
    let value: Ready = serde_json::from_str(payload).map_err(|_| "Invalid ready payload")?;
    if value.protocol != 1 || value.port == 0 || value.pid != pid {
        return Err("Invalid ready identity");
    }
    Ok(value.port)
}

fn token() -> Result<String, &'static str> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| "Secure randomness unavailable")?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// Match CPython's Windows venv redirector without creating that extra process.
///
/// PC/launcher.c (VENV_REDIRECT) starts `home/python.exe` and passes the original
/// executable as __PYVENV_LAUNCHER__. CPython's getpath uses it for sys.executable
/// and venv discovery, while loading the runtime from the base executable.
/// Keeping the interpreter as our direct child preserves both PID authentication
/// and forced shutdown ownership; readiness never authorizes opening another PID.
#[cfg(any(windows, test))]
pub(crate) fn windows_python_command(python: &Path) -> Result<Command, &'static str> {
    // Do not canonicalize: resolving a venv symlink would lose its environment.
    let python = std::path::absolute(python).map_err(|_| "Invalid development Python path")?;
    if !python.is_file() {
        return Err("Development Python must be an existing executable path");
    }
    let directory = python.parent().ok_or("Invalid development Python path")?;
    for directory in [Some(directory), directory.parent()].into_iter().flatten() {
        let config = match std::fs::read_to_string(directory.join("pyvenv.cfg")) {
            Ok(config) => config,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => return Err("Could not read development Python venv configuration"),
        };
        let home = config
            .lines()
            .filter_map(|line| line.split_once('='))
            .find_map(|(key, value)| (key.trim() == "home").then_some(value.trim()))
            .filter(|home| !home.is_empty())
            .ok_or("Missing development Python venv home")?;
        let home = Path::new(home);
        if !home.is_absolute() {
            return Err("Development Python venv home must be absolute");
        }
        let base = home.join("python.exe");
        if !base.is_file() || base == python {
            return Err("Invalid development Python base executable");
        }
        let mut command = Command::new(base);
        command.env("__PYVENV_LAUNCHER__", &python);
        return Ok(command);
    }
    let mut command = Command::new(python);
    // An inherited launcher hint must not select an unrelated environment.
    command.env_remove("__PYVENV_LAUNCHER__");
    Ok(command)
}

pub struct Backend {
    child: Child,
    diagnostics: Diagnostics,
    ready: bool,
    exit_recorded: bool,
    wait_failed: bool,
    exit_reason: &'static str,
}

pub type ReadySignal = Receiver<Result<String, Failure>>;

pub fn wait_ready(signal: &ReadySignal, timeout: Duration) -> Result<String, Failure> {
    match signal.recv_timeout(timeout) {
        Ok(result) => result,
        Err(mpsc::RecvTimeoutError::Timeout) => Err(Failure::ReadyTimeout),
        Err(mpsc::RecvTimeoutError::Disconnected) => Err(Failure::ChannelClosed),
    }
}

impl Backend {
    fn new(child: Child, diagnostics: Diagnostics) -> Self {
        Self {
            child,
            diagnostics,
            ready: false,
            exit_recorded: false,
            wait_failed: false,
            exit_reason: "process_exited",
        }
    }

    fn launch(command: &mut Command, diagnostics: Diagnostics) -> Result<Self, Failure> {
        let child = command
            .spawn()
            .map_err(|error| Failure::Spawn(error.raw_os_error()))?;
        diagnostics.record(Source::Native, Event::EngineSpawned, json!({}));
        Ok(Self::new(child, diagnostics))
    }

    pub fn spawn(
        resources: &Path,
        workspace: Option<&Path>,
        runtime: Option<&Path>,
        library: Option<&Path>,
        native_owner: &crate::instance::NativeOwner,
        diagnostics: Diagnostics,
        notifications: &crate::notifications::Center,
    ) -> Result<(Self, ReadySignal), Failure> {
        let token = token().map_err(Failure::Setup)?;
        let mut command = Command::new(resources.join("sidecar/wenyi-engine").join(engine_name()));
        command.env_remove("__PYVENV_LAUNCHER__");
        if cfg!(debug_assertions) {
            if let Some(python) = std::env::var_os("WENYI_DESKTOP_PYTHON") {
                #[cfg(windows)]
                {
                    command = windows_python_command(Path::new(&python)).map_err(Failure::Setup)?;
                }
                #[cfg(not(windows))]
                {
                    command = Command::new(python);
                    command.env_remove("__PYVENV_LAUNCHER__");
                }
                command.args(["-m", "wenyi_desktop"]);
            }
        }
        // Inject instead of letting the engine derive: the parent owns the
        // resolution, and the engine only falls back when it runs standalone.
        if let Some(workspace) = workspace {
            command.arg("--data-dir").arg(workspace);
        }
        if let Some(runtime) = runtime {
            command.arg("--runtime-dir").arg(runtime);
        }
        // Layer three is passed only when there is an installation to put it
        // beside. A development run has no installer record, and a sibling of
        // target/debug is a directory the developer never chose; leaving the
        // argument out makes the engine keep using the workspace it was given.
        if let Some(library) = library {
            command.arg("--library-root").arg(library);
            // The engine cannot derive this: as a bundled sidecar its own
            // executable lives beside the runtime it loads, not beside the
            // installed application, so the parent names the installation.
            if let Ok(install) = crate::paths::running_install_dir() {
                command.arg("--install-root").arg(install);
            }
        }
        command
            .arg("--native-startup")
            .env("WENYI_API_TOKEN", &token)
            .env("WENYI_DIAGNOSTICS_PROTOCOL", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // Never forward arbitrary backend output (which might contain credentials).
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let mut backend = Self::launch(&mut command, diagnostics.clone())?;
        let proof = serde_json::to_string(native_owner).map_err(|_| Failure::InvalidProtocol)?;
        writeln!(
            backend
                .child
                .stdin
                .as_mut()
                .ok_or(Failure::InvalidProtocol)?,
            "{proof}"
        )
        .map_err(|_| Failure::ReadProtocol)?;
        let output = backend
            .child
            .stdout
            .take()
            .ok_or(Failure::Setup("Missing protocol pipe"))?;
        let pid = backend.child.id();
        let (tx, rx) = mpsc::channel();
        let notifications = notifications.clone();
        std::thread::spawn(move || {
            receive_protocol(output, pid, &token, &diagnostics, &notifications, tx);
        });
        Ok((backend, rx))
    }

    pub fn mark_ready(&mut self) {
        self.ready = true;
    }

    pub fn is_ready(&self) -> bool {
        self.ready
    }

    pub fn id(&self) -> u32 {
        self.child.id()
    }

    fn stage(&self) -> &'static str {
        if self.ready {
            "running"
        } else {
            "bootstrap"
        }
    }

    fn record_exit(&mut self, status: ExitStatus) {
        if self.exit_recorded {
            return;
        }
        self.exit_recorded = true;
        #[cfg(unix)]
        let signal = {
            use std::os::unix::process::ExitStatusExt;
            status.signal()
        };
        #[cfg(not(unix))]
        let signal: Option<i32> = None;
        self.diagnostics.record(
            Source::Native,
            Event::EngineExited,
            json!({
                "stage": self.stage(), "reason": self.exit_reason,
                "exit_code": status.code(), "signal": signal,
            }),
        );
    }

    pub fn exited(&mut self) -> bool {
        match self.child.try_wait() {
            Ok(None) => false,
            Ok(Some(status)) => {
                self.record_exit(status);
                true
            }
            Err(error) => {
                if !self.wait_failed {
                    self.wait_failed = true;
                    self.diagnostics.record(Source::Native, Event::EngineFailed, json!({
                        "stage": self.stage(), "reason": "wait_failed", "os_error": error.raw_os_error(),
                    }));
                }
                // Failure to inspect a process is not evidence that it exited.
                false
            }
        }
    }

    pub fn shutdown(&mut self) {
        self.shutdown_with_timeout(Duration::from_secs(30));
    }

    fn shutdown_with_timeout(&mut self, grace: Duration) {
        // Observe natural termination before cleanup; never report our later kill
        // as the original startup failure.
        if self.exited() {
            return;
        }
        self.exit_reason = "shutdown_requested";
        if let Some(mut input) = self.child.stdin.take() {
            let _ = input.write_all(b"{\"command\":\"shutdown\"}\n");
        }
        let until = Instant::now() + grace;
        while Instant::now() < until {
            if self.exited() {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        if self.child.kill().is_ok() {
            self.exit_reason = "forced";
            if let Ok(status) = self.child.wait() {
                self.record_exit(status);
            }
        } else {
            // A failed kill is not a stopped process. Keep the Child handle so
            // callers can refuse replacement/snapshot instead of assuming exit.
            self.exited();
        }
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.shutdown();
    }
}

fn receive_protocol(
    output: impl Read,
    pid: u32,
    token: &str,
    diagnostics: &Diagnostics,
    notifications: &crate::notifications::Center,
    tx: mpsc::Sender<Result<String, Failure>>,
) {
    let mut reader = BufReader::new(output);
    let mut sent = false;
    let mut discard = false;
    loop {
        let mut bytes = Vec::new();
        let count = match reader
            .by_ref()
            .take(crate::diagnostics::MAX_RECORD as u64 + 1)
            .read_until(b'\n', &mut bytes)
        {
            Ok(count) => count,
            Err(_) => {
                if !sent {
                    let _ = tx.send(Err(Failure::ReadProtocol));
                }
                return;
            }
        };
        if count == 0 {
            break;
        }
        let complete = bytes.ends_with(b"\n");
        if discard || !complete || bytes.len() > crate::diagnostics::MAX_RECORD {
            if !sent {
                let _ = tx.send(Err(Failure::InvalidProtocol));
                sent = true;
            }
            discard = !complete;
            continue;
        }
        let Ok(line) = std::str::from_utf8(&bytes) else {
            if !sent {
                let _ = tx.send(Err(Failure::InvalidProtocol));
                sent = true;
            }
            continue;
        };
        if let Some(payload) = line.strip_prefix("WENYI_DIAGNOSTIC ") {
            // Only typed content-free observations cross the persistence boundary.
            diagnostics.ingest(crate::diagnostics::Source::Engine, payload);
        } else if let Some(payload) = line.strip_prefix("WENYI_NOTICE ") {
            // A notice cannot exist before a task could start; readiness is the
            // first moment the native owner is willing to deliver one.
            if sent {
                notifications.submit_payload(payload);
            }
        } else if !sent {
            let result = ready(line, pid).map_err(|_| Failure::InvalidProtocol).map(|port| {
                serde_json::json!({"apiBase": format!("http://127.0.0.1:{port}"), "token": token}).to_string()
            });
            let _ = tx.send(result);
            sent = true;
        }
        // Unknown post-ready output is discarded, never saved as raw text.
    }
    if !sent {
        let _ = tx.send(Err(Failure::ProtocolEof));
    }
}

fn engine_name() -> &'static str {
    if cfg!(windows) {
        "wenyi-engine.exe"
    } else {
        "wenyi-engine"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn native_records(diagnostics: &Diagnostics) -> Vec<serde_json::Value> {
        diagnostics.drain();
        let bytes = diagnostics
            .bundle(crate::diagnostics::Range::Session, || false)
            .unwrap();
        let mut archive = zip::ZipArchive::new(std::io::Cursor::new(bytes)).unwrap();
        let mut text = String::new();
        archive
            .by_name("logs/native.jsonl")
            .unwrap()
            .read_to_string(&mut text)
            .unwrap();
        assert!(!text.contains("fake-private"));
        text.lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    #[test]
    fn readiness_failure_reasons_are_distinct_and_never_include_protocol_content() {
        let diagnostics = Diagnostics::new(None);
        let notifications = crate::notifications::Center::silent(diagnostics.clone());
        for (input, expected) in [
            (Vec::new(), Failure::ProtocolEof),
            (b"fake-private-token\n".to_vec(), Failure::InvalidProtocol),
            (vec![0xff, b'\n'], Failure::InvalidProtocol),
            (
                vec![b'x'; crate::diagnostics::MAX_RECORD + 1],
                Failure::InvalidProtocol,
            ),
            (
                b"WENYI_READY {\"protocol\":1,\"pid\":8,\"port\":1234}\n".to_vec(),
                Failure::InvalidProtocol,
            ),
        ] {
            let (tx, rx) = mpsc::channel();
            receive_protocol(
                input.as_slice(),
                9,
                "fake-private-token",
                &diagnostics,
                &notifications,
                tx,
            );
            assert_eq!(wait_ready(&rx, Duration::ZERO), Err(expected));
            assert!(rx.try_recv().is_err());
        }
        struct BrokenReader;
        impl Read for BrokenReader {
            fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                Err(std::io::Error::other("fake-private-pipe"))
            }
        }
        let (tx, rx) = mpsc::channel();
        receive_protocol(
            BrokenReader,
            9,
            "fake-private-token",
            &diagnostics,
            &notifications,
            tx,
        );
        assert_eq!(wait_ready(&rx, Duration::ZERO), Err(Failure::ReadProtocol));
        let (tx, rx) = mpsc::channel();
        assert_eq!(wait_ready(&rx, Duration::ZERO), Err(Failure::ReadyTimeout));
        drop(tx);
        assert_eq!(wait_ready(&rx, Duration::ZERO), Err(Failure::ChannelClosed));
        for failure in [
            Failure::ProtocolEof,
            Failure::ReadProtocol,
            Failure::InvalidProtocol,
            Failure::ReadyTimeout,
            Failure::ChannelClosed,
        ] {
            failure.report(&diagnostics);
        }
        let records = native_records(&diagnostics);
        assert_eq!(
            records
                .iter()
                .map(|row| row["attributes"]["reason"].as_str().unwrap())
                .collect::<Vec<_>>(),
            [
                "protocol_eof",
                "protocol_read_failed",
                "protocol_invalid",
                "ready_timeout",
                "channel_closed"
            ],
        );
    }

    #[test]
    fn spawn_failure_reports_os_code_without_the_executable_path() {
        let fixture = PythonFixture::new();
        let diagnostics = Diagnostics::new(None);
        let failure = match Backend::launch(
            &mut Command::new(fixture.0.join("fake-private-missing")),
            diagnostics.clone(),
        ) {
            Ok(_) => panic!("Missing executable unexpectedly started"),
            Err(failure) => failure,
        };
        assert!(matches!(failure, Failure::Spawn(Some(_))));
        failure.report(&diagnostics);
        let records = native_records(&diagnostics);
        assert_eq!(records.len(), 1);
        assert_eq!(records[0]["attributes"]["reason"], "spawn_failed");
        assert!(records[0]["attributes"]["os_error"].is_number());
    }

    #[test]
    fn early_process_exit_retains_the_actual_exit_code_once() {
        #[cfg(unix)]
        let child = Command::new("sh").args(["-c", "exit 3"]).spawn().unwrap();
        #[cfg(windows)]
        let child = Command::new("cmd")
            .args(["/D", "/C", "exit 3"])
            .spawn()
            .unwrap();
        let diagnostics = Diagnostics::new(None);
        let mut backend = Backend::new(child, diagnostics.clone());
        backend.child.wait().unwrap();
        assert!(backend.exited());
        assert!(backend.exited());
        backend.shutdown();
        let records = native_records(&diagnostics);
        assert_eq!(records.len(), 1);
        assert_eq!(records[0]["event"], "engine_exited");
        assert_eq!(
            records[0]["attributes"],
            json!({
                "stage": "bootstrap", "reason": "process_exited", "exit_code": 3,
            })
        );
    }

    #[test]
    #[cfg(windows)]
    fn windows_native_status_is_not_lost_as_an_unsigned_overflow() {
        use std::os::windows::process::ExitStatusExt;
        let diagnostics = Diagnostics::new(None);
        let child = Command::new("cmd")
            .args(["/D", "/C", "exit 0"])
            .spawn()
            .unwrap();
        let mut backend = Backend::new(child, diagnostics.clone());
        backend.child.wait().unwrap();
        backend.record_exit(ExitStatus::from_raw(0xc0000005));
        assert_eq!(
            native_records(&diagnostics)[0]["attributes"]["exit_code"],
            -1073741819i64
        );
    }

    #[test]
    fn diagnostics_frames_do_not_change_ready_identity_or_capture_arbitrary_output() {
        let diagnostics = crate::diagnostics::Diagnostics::new(None);
        let input = concat!(
            "WENYI_DIAGNOSTIC {\"event\":\"engine_bootstrap\"}\n",
            "WENYI_READY {\"protocol\":1,\"port\":1234,\"pid\":9}\n",
            "fake-private-token\n",
            "WENYI_DIAGNOSTIC {\"event\":\"task_failed\",\"attributes\":{\"raw\":\"fake-private-token\"}}\n",
        );
        let (tx, rx) = mpsc::channel();
        let notifications = crate::notifications::Center::silent(diagnostics.clone());
        receive_protocol(
            input.as_bytes(),
            9,
            "owner-token",
            &diagnostics,
            &notifications,
            tx,
        );
        let connection: serde_json::Value =
            serde_json::from_str(&rx.recv().unwrap().unwrap()).unwrap();
        assert_eq!(connection["apiBase"], "http://127.0.0.1:1234");
        assert_eq!(connection["token"], "owner-token");
        assert!(rx.try_recv().is_err());
        diagnostics.drain();
        assert!(diagnostics.status().last_error_id.is_some());
    }

    struct PythonFixture(std::path::PathBuf);

    impl PythonFixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!("wenyi-python-{}", token().unwrap()));
            std::fs::create_dir_all(root.join("venv/Scripts")).unwrap();
            std::fs::create_dir_all(root.join("base runtime")).unwrap();
            std::fs::write(root.join("venv/Scripts/python.exe"), []).unwrap();
            std::fs::write(root.join("base runtime/python.exe"), []).unwrap();
            Self(root)
        }

        fn python(&self) -> std::path::PathBuf {
            self.0.join("venv/Scripts/python.exe")
        }

        fn configure(&self, contents: &str) {
            std::fs::write(self.0.join("venv/pyvenv.cfg"), contents).unwrap();
        }
    }

    impl Drop for PythonFixture {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.0).unwrap();
        }
    }

    #[test]
    fn windows_venv_launch_owns_base_python_but_preserves_venv_identity() {
        let fixture = PythonFixture::new();
        fixture.configure(&format!(
            "include-system-site-packages = false\r\nhome = {}\r\n",
            fixture.0.join("base runtime").display()
        ));
        let command = windows_python_command(&fixture.python()).unwrap();
        assert_eq!(
            command.get_program(),
            fixture.0.join("base runtime/python.exe")
        );
        let launcher = std::path::absolute(fixture.python()).unwrap();
        assert!(command.get_envs().any(|(key, value)| {
            key == "__PYVENV_LAUNCHER__" && value == Some(launcher.as_os_str())
        }));

        // CPython checks an adjacent configuration before the parent directory.
        std::fs::write(fixture.0.join("venv/Scripts/pyvenv.cfg"), "home = invalid").unwrap();
        assert!(windows_python_command(&fixture.python()).is_err());
    }

    #[test]
    fn windows_venv_configuration_fails_closed_instead_of_starting_a_redirector() {
        let fixture = PythonFixture::new();
        for config in [
            "version = 3.12",
            "home = ",
            "home = relative",
            &format!("home = {}", fixture.0.join("missing").display()),
            &format!("home = {}", fixture.0.join("venv/Scripts").display()),
        ] {
            fixture.configure(config);
            assert!(windows_python_command(&fixture.python()).is_err());
        }
    }

    #[test]
    fn standalone_python_is_not_redirected_to_an_inherited_venv() {
        let fixture = PythonFixture::new();
        let command = windows_python_command(&fixture.python()).unwrap();
        assert_eq!(command.get_program(), fixture.python());
        assert!(command
            .get_envs()
            .any(|(key, value)| key == "__PYVENV_LAUNCHER__" && value.is_none()));
    }

    #[test]
    #[cfg(windows)]
    fn windows_real_venv_identity_and_graceful_or_forced_cleanup() {
        // CI installs this venv before cargo test. Do not skip a missing runtime:
        // this is the real-platform acceptance test for CPython's launcher hint.
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let python = root.join(".venv/Scripts/python.exe");
        for force in [false, true] {
            let mut command = windows_python_command(&python).unwrap();
            command.args([
                "-c",
                concat!(
                    "import json, os, sys, time; ",
                    "import wenyi_desktop; ",
                    "print('WENYI_READY ' + json.dumps(dict(protocol=1, port=1234, ",
                    "pid=os.getpid())), flush=True); ",
                    "print(json.dumps([sys.executable, sys.prefix]), flush=True); ",
                    "time.sleep(60) if sys.argv[1] == 'force' else ",
                    "sys.exit(0 if sys.stdin.readline() == ",
                    "'{\\\"command\\\":\\\"shutdown\\\"}\\n' else 1)"
                ),
                if force { "force" } else { "graceful" },
            ]);
            let child = command
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .spawn()
                .unwrap();
            let mut backend = Backend::new(child, Diagnostics::new(None));
            let output = backend.child.stdout.take().unwrap();
            let (tx, rx) = mpsc::channel();
            std::thread::spawn(move || {
                use std::io::BufRead;
                let mut reader = BufReader::new(output);
                let mut lines = [String::new(), String::new()];
                for line in &mut lines {
                    reader.read_line(line).unwrap();
                }
                let _ = tx.send(lines);
            });
            let lines = rx.recv_timeout(Duration::from_secs(10)).unwrap();
            assert_eq!(ready(&lines[0], backend.child.id()), Ok(1234));
            let paths: Vec<std::path::PathBuf> = serde_json::from_str(&lines[1]).unwrap();
            assert_eq!(
                paths[0].canonicalize().unwrap(),
                python.canonicalize().unwrap()
            );
            assert_eq!(
                paths[1].canonicalize().unwrap(),
                root.join(".venv").canonicalize().unwrap()
            );
            backend.shutdown_with_timeout(if force {
                Duration::ZERO
            } else {
                Duration::from_secs(2)
            });
            let status = backend.child.try_wait().unwrap().unwrap();
            assert_eq!(status.success(), !force);
        }
    }

    #[test]
    fn strict_protocol_does_not_echo_sensitive_input() {
        assert_eq!(
            ready(
                "WENYI_READY {\"protocol\":1,\"port\":1234,\"pid\":42}\n",
                42
            ),
            Ok(1234)
        );
        for line in [
            "secret",
            "WENYI_READY {\"protocol\":2,\"port\":1234,\"pid\":42}",
            "WENYI_READY {\"protocol\":1,\"port\":0,\"pid\":42}",
            "WENYI_READY {\"protocol\":1,\"port\":1234,\"pid\":43}",
            "WENYI_READY {\"protocol\":1,\"port\":1234,\"pid\":42,\"token\":\"secret\"}",
        ] {
            assert!(!ready(line, 42).unwrap_err().contains("secret"));
        }
    }
    #[test]
    fn credentials_are_fresh_url_safe_256_bit_values() {
        let a = token().unwrap();
        assert_eq!(a.len(), 64);
        assert!(a.bytes().all(|b| b.is_ascii_hexdigit()));
        assert_ne!(a, token().unwrap());
    }

    #[test]
    #[cfg(unix)]
    fn graceful_shutdown_sends_command_and_reaps_child() {
        let child = Command::new("sh")
            .args([
                "-c",
                "read line; test \"$line\" = '{\"command\":\"shutdown\"}'",
            ])
            .stdin(Stdio::piped())
            .spawn()
            .unwrap();
        let diagnostics = Diagnostics::new(None);
        let mut backend = Backend::new(child, diagnostics.clone());
        backend.mark_ready();
        backend.shutdown_with_timeout(Duration::from_secs(2));
        assert!(backend.child.try_wait().unwrap().unwrap().success());
        let records = native_records(&diagnostics);
        assert_eq!(
            records[0]["attributes"],
            json!({
                "stage": "running", "reason": "shutdown_requested", "exit_code": 0,
            })
        );
    }

    #[test]
    #[cfg(unix)]
    fn unresponsive_child_is_killed_and_reaped_after_deadline() {
        let child = Command::new("sh")
            .args(["-c", "while :; do :; done"])
            .stdin(Stdio::piped())
            .spawn()
            .unwrap();
        let diagnostics = Diagnostics::new(None);
        let mut backend = Backend::new(child, diagnostics.clone());
        let start = Instant::now();
        backend.shutdown_with_timeout(Duration::from_millis(50));
        assert!(start.elapsed() < Duration::from_secs(2));
        assert!(backend.exited());
        assert!(!backend.child.try_wait().unwrap().unwrap().success());
        let records = native_records(&diagnostics);
        assert_eq!(records.len(), 1);
        assert_eq!(
            records[0]["attributes"],
            json!({
                "stage": "bootstrap", "reason": "forced", "signal": 9,
            })
        );
    }
}
