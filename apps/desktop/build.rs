fn main() {
    println!("cargo:rerun-if-env-changed=WENYI_DESKTOP_VERSION");
    println!("cargo:rerun-if-env-changed=WENYI_BUILD_TAG");
    println!("cargo:rerun-if-env-changed=WENYI_UPDATER_PUBLIC_KEY");
    println!("cargo:rerun-if-env-changed=TAURI_CONFIG");
    // Direct cargo run/test must use the same identity as the packaging launcher.
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    // Observe tracked sources, not ignored workspaces, book data or build caches.
    let tracked = std::process::Command::new("git")
        .args(["ls-files", "-z"])
        .current_dir(&root)
        .output()
        .expect("Git is required for Desktop builds");
    assert!(tracked.status.success(), "Cannot enumerate tracked sources");
    for file in String::from_utf8_lossy(&tracked.stdout).split('\0') {
        if !file.is_empty() {
            println!("cargo:rerun-if-changed={}", root.join(file).display());
        }
    }
    for name in ["HEAD", "index", "refs", "packed-refs"] {
        let location = std::process::Command::new("git")
            .args(["rev-parse", "--path-format=absolute", "--git-path", name])
            .current_dir(&root)
            .output()
            .expect("Cannot locate Git metadata");
        assert!(location.status.success(), "Cannot locate Git metadata");
        println!(
            "cargo:rerun-if-changed={}",
            String::from_utf8_lossy(&location.stdout).trim()
        );
    }
    let version = std::env::var("WENYI_DESKTOP_VERSION").unwrap_or_else(|_| {
        let output = std::process::Command::new("uv")
            .args([
                "run",
                "--no-project",
                "--with",
                "hatch-vcs",
                "python",
                "scripts/release_version.py",
            ])
            .current_dir(&root)
            .output()
            .expect("uv is required to resolve the Git application version");
        assert!(
            output.status.success(),
            "Cannot resolve application version: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let identity: serde_json::Value =
            serde_json::from_slice(&output.stdout).expect("Invalid version helper response");
        identity["version"]
            .as_str()
            .expect("Missing version")
            .to_owned()
    });
    let mut config: serde_json::Value =
        serde_json::from_str(&std::env::var("TAURI_CONFIG").unwrap_or_else(|_| "{}".into()))
            .expect("Invalid TAURI_CONFIG");
    let public_key = std::env::var("WENYI_UPDATER_PUBLIC_KEY").unwrap_or_default();
    let public_key = public_key.trim();
    assert!(
        !public_key.contains(['\r', '\n']),
        "WENYI_UPDATER_PUBLIC_KEY must be the single-line Tauri public key content"
    );
    println!("cargo:rustc-env=WENYI_UPDATER_PUBLIC_KEY={public_key}");
    println!("cargo:rustc-env=WENYI_DESKTOP_VERSION={version}");
    config["version"] = version.into();
    let config = config.to_string();
    std::env::set_var("TAURI_CONFIG", &config);
    println!("cargo:rustc-env=TAURI_CONFIG={config}");
    tauri_build::build();
}
