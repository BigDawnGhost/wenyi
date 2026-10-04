//! A single owner for the private server and its anonymous protocol pipes.
use std::{
    io::{BufReader, Write},
    path::Path,
    process::{Child, Command, Stdio},
    sync::mpsc::{self, Receiver},
    time::{Duration, Instant},
};

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
fn windows_python_command(python: &Path) -> Result<Command, &'static str> {
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
}

pub type ReadySignal = Receiver<Result<String, &'static str>>;

impl Backend {
    pub fn spawn(
        resources: &Path,
        workspace: Option<&Path>,
    ) -> Result<(Self, ReadySignal), &'static str> {
        let token = token()?;
        let mut command = Command::new(resources.join("sidecar/wenyi-engine").join(engine_name()));
        command.env_remove("__PYVENV_LAUNCHER__");
        if cfg!(debug_assertions) {
            if let Some(python) = std::env::var_os("WENYI_DESKTOP_PYTHON") {
                #[cfg(windows)]
                {
                    command = windows_python_command(Path::new(&python))?;
                }
                #[cfg(not(windows))]
                {
                    command = Command::new(python);
                    command.env_remove("__PYVENV_LAUNCHER__");
                }
                command.args(["-m", "wenyi_desktop"]);
            }
        }
        if let Some(workspace) = workspace {
            command.arg("--data-dir").arg(workspace);
        }
        command
            .env("WENYI_API_TOKEN", &token)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // Never forward arbitrary backend output (which might contain credentials).
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let mut child = command
            .spawn()
            .map_err(|_| "Could not start local engine")?;
        let output = child.stdout.take().ok_or("Missing protocol pipe")?;
        let pid = child.id();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(output);
            let mut line = String::new();
            // Bounded read: a broken child cannot allocate arbitrary memory.
            let result =
                std::io::BufRead::read_line(&mut std::io::Read::take(&mut reader, 4096), &mut line)
                    .map_err(|_| "Could not read ready protocol")
                    .and_then(|_| ready(&line, pid))
                    .map(|port| {
                        serde_json::json!({
                            "apiBase": format!("http://127.0.0.1:{port}"),
                            "token": token
                        })
                        .to_string()
                    });
            let _ = tx.send(result);
            let _ = std::io::copy(&mut reader, &mut std::io::sink());
        });
        Ok((Self { child }, rx))
    }

    pub fn exited(&mut self) -> bool {
        !matches!(self.child.try_wait(), Ok(None))
    }

    pub fn shutdown(&mut self) {
        self.shutdown_with_timeout(Duration::from_secs(30));
    }

    fn shutdown_with_timeout(&mut self, grace: Duration) {
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
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.shutdown();
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
        assert!(command.get_envs().any(|(key, value)| {
            key == "__PYVENV_LAUNCHER__" && value == Some(fixture.python().as_os_str())
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
            let mut backend = Backend { child };
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
        let mut backend = Backend { child };
        backend.shutdown_with_timeout(Duration::from_secs(2));
        assert!(backend.child.try_wait().unwrap().unwrap().success());
    }

    #[test]
    #[cfg(unix)]
    fn unresponsive_child_is_killed_and_reaped_after_deadline() {
        let child = Command::new("sh")
            .args(["-c", "while :; do :; done"])
            .stdin(Stdio::piped())
            .spawn()
            .unwrap();
        let mut backend = Backend { child };
        let start = Instant::now();
        backend.shutdown_with_timeout(Duration::from_millis(50));
        assert!(start.elapsed() < Duration::from_secs(2));
        assert!(backend.exited());
        assert!(!backend.child.try_wait().unwrap().unwrap().success());
    }
}
