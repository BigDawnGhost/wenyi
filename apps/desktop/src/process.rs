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
        let mut command = if cfg!(debug_assertions) {
            if let Some(python) = std::env::var_os("WENYI_DESKTOP_PYTHON") {
                let mut command = Command::new(python);
                command.args(["-m", "wenyi_desktop"]);
                command
            } else {
                Command::new(resources.join("sidecar/wenyi-engine").join(engine_name()))
            }
        } else {
            Command::new(resources.join("sidecar/wenyi-engine").join(engine_name()))
        };
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
