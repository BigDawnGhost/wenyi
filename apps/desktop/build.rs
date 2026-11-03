/// The GitHub repository the updater reads releases from.
///
/// A fork that publishes its own releases has to be able to point the updater at
/// itself without editing source, because the value is checked in two places and
/// a rebuild that updated only one of them would trust a URL it never wrote.
/// The default is this project: an ordinary build, a test and a development run
/// are unchanged by the variable being unset.
const DEFAULT_UPDATE_REPO: &str = "BigDawnGhost/wenyi";

fn main() {
    println!("cargo:rerun-if-env-changed=WENYI_UPDATE_REPO");
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
                "scripts/release/release_version.py",
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
    // Validated here rather than at the call site: the value becomes part of a
    // URL prefix that `trusted_artifact` compares, so a typo would not produce a
    // visible failure -- it would produce an updater that silently rejects every
    // release the same build published.
    let repo = std::env::var("WENYI_UPDATE_REPO")
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_UPDATE_REPO.to_owned());
    assert!(
        is_repository_slug(&repo),
        "WENYI_UPDATE_REPO must be \"owner/repository\", not {repo:?}"
    );
    println!("cargo:rustc-env=WENYI_UPDATE_REPO={repo}");
    config["version"] = version.into();
    let config = config.to_string();
    std::env::set_var("TAURI_CONFIG", &config);
    println!("cargo:rustc-env=TAURI_CONFIG={config}");
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .windows_attributes(tauri_build::WindowsAttributes::new().app_manifest(APP_MANIFEST)),
    )
    .expect("failed to run tauri-build");
}

/// Whether a value names one GitHub repository and nothing else.
///
/// The shape is deliberately narrow. The value is spliced into a URL and into a
/// prefix compared against the paths of download links, so an extra segment, a
/// scheme or a trailing slash would change which URLs are trusted rather than
/// merely failing to parse.
fn is_repository_slug(value: &str) -> bool {
    let Some((owner, repository)) = value.split_once('/') else {
        return false;
    };
    !owner.is_empty()
        && !repository.is_empty()
        && !repository.contains('/')
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'/'))
}

/// The Windows application manifest embedded in the executable.
///
/// `longPathAware` is what makes the process use the extended-length path rules
/// instead of the legacy 260-character limit. Without it, a path that Python can
/// open through the extended-length prefix is still refused by every Win32 call the
/// native side makes, so a deep workspace would work in one layer and fail in
/// the other.
///
/// The Common-Controls v6 dependency is kept verbatim from the manifest
/// `tauri-build` ships by default: without it the file dialogs fall back to the
/// legacy control set and lose their modern layout.
const APP_MANIFEST: &str = r#"<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <dependency>
    <dependentAssembly>
      <assemblyIdentity
        type="win32"
        name="Microsoft.Windows.Common-Controls"
        version="6.0.0.0"
        processorArchitecture="*"
        publicKeyToken="6595b64144ccf1df"
        language="*"
      />
    </dependentAssembly>
  </dependency>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
    </windowsSettings>
  </application>
</assembly>
"#;
