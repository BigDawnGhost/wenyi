//! The single place where Wenyi's directory roots are named and resolved.
//!
//! The isolation design defines three layers, and every one of them is resolved
//! here instead of at its call site:
//!
//! | layer | root | lifetime |
//! |---|---|---|
//! | one | ~/.wenyi/desktop | rebuilt with the application |
//! | two | ~/Wenyi | backed up, shared across projects |
//! | three | a sibling of the installation directory | user documents |
//!
//! Resolution is pure: nothing here creates, canonicalizes, or checks a
//! directory on disk. Diagnostics deliberately derives its roots from the
//! admitted workspace and the platform base only (never from layers one to
//! three), so a startup that later fails its layout assertions can still write
//! the log that explains why.
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

/// Product name shared by the desktop workspace and its fallback log directory.
const PRODUCT: &str = "Wenyi Desktop";
/// Fallback log directory. The spelling deliberately differs from PRODUCT:
/// logs have to stay available when the workspace itself is not writable.
const LOGS: &str = "Wenyi Desktop Logs";
/// Directory inside a workspace that owns the diagnostic log files.
const DIAGNOSTICS: &str = "diagnostics";
/// Layer one root under the user profile.
const RUNTIME: &str = ".wenyi";
/// Layer one entry for this application inside RUNTIME.
const ENTRY: &str = "desktop";
/// Layer two root under the user profile.
const USER: &str = "Wenyi";
/// Layer three directory created next to the installation directory.
const LIBRARY: &str = "Wenyi Desktop Data";
/// Environment override for layer one. Layers two and three are deliberately
/// not overridable: layer two is persisted configuration and layer three is
/// recorded per project in the workspace registry.
const RUNTIME_OVERRIDE: &str = "WENYI_RUNTIME_DIR";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// The platform does not expose the base directory these roots derive from.
    Base,
    /// The installation directory is a volume root, so it has no sibling.
    InstallDirAtVolumeRoot,
    /// Two roots are equal or one contains the other.
    Overlap,
    /// A chosen library location is one the layout cannot keep data in.
    LibraryRootInvalid,
}

impl Error {
    pub fn message(self) -> &'static str {
        match self {
            Self::Base => "Wenyi could not determine where to keep its files on this computer. Check the user profile environment and reopen Wenyi.",
            Self::InstallDirAtVolumeRoot => "Wenyi is installed at a drive root. Reinstall it into a folder so its data can live beside it.",
            Self::Overlap => "Wenyi's data folders overlap. Move one of them and reopen Wenyi.",
            Self::LibraryRootInvalid => "Wenyi cannot keep project data in that folder. Choose a folder that is not Wenyi's own program folder, a drive or user folder, or a system folder.",
        }
    }
}

/// The workspace a native parent admits and hands to the engine.
///
/// An explicit --data-dir always wins; otherwise the platform default below
/// applies. This is the value the engine receives, so it must stay identical to
/// the Python standalone fallback in wenyi_desktop.desktop.
pub fn workspace(explicit: Option<PathBuf>) -> Result<PathBuf, Error> {
    match explicit {
        Some(path) => Ok(path),
        None => default_workspace(),
    }
}

/// %LOCALAPPDATA%\\Wenyi Desktop and its per-platform equivalents.
pub fn default_workspace() -> Result<PathBuf, Error> {
    Ok(data_base()?.join(PRODUCT))
}

/// Ordered diagnostic roots: the workspace copy first, then the user-level copy.
///
/// Both candidates are kept on purpose. The fallback serves the case that needs
/// logs most: a workspace that cannot be written to. The second entry uses the
/// workspace identity hash so that several workspaces never share one log file.
pub fn log_roots(workspace: &Path) -> Vec<PathBuf> {
    let mut candidates = vec![workspace.join(DIAGNOSTICS)];
    if let Some(base) = log_base().filter(|path| path.is_absolute()) {
        let reference = format!(
            "{:x}",
            Sha256::digest(workspace.to_string_lossy().as_bytes())
        );
        candidates.push(base.join(LOGS).join(&reference[..16]));
    }
    candidates
}

/// Layer one: application runtime that may be discarded and rebuilt.
pub fn runtime_root() -> Result<PathBuf, Error> {
    if let Some(explicit) = override_path(RUNTIME_OVERRIDE) {
        return Ok(explicit);
    }
    Ok(home_dir()?.join(RUNTIME).join(ENTRY))
}

/// Layer two: user-level data that is backed up and shared across projects.
pub fn user_root() -> Result<PathBuf, Error> {
    Ok(home_dir()?.join(USER))
}

/// Layer three: the default directory that holds one folder per project.
///
/// The parent is a *sibling* of the installation, never a child of it: upgrading
/// or uninstalling touches $INSTDIR, and a directory beside it is one the
/// installer can never reach by any policy change upstream might make.
///
/// A drive root has no parent of its own, and a bare drive spelling reports an
/// empty parent, so both are refused rather than silently resolved to the volume
/// the installation sits in. No caller may build this path itself: the defence
/// only holds while there is one place that names it.
pub fn library_root(install_dir: &Path) -> Result<PathBuf, Error> {
    let parent = install_dir.parent().ok_or(Error::InstallDirAtVolumeRoot)?;
    if parent == install_dir || parent.as_os_str().is_empty() {
        return Err(Error::InstallDirAtVolumeRoot);
    }
    Ok(parent.join(LIBRARY))
}

/// What this start found when it compared the install location on record with
/// the directory it is actually running from.
///
/// The installer writes the chosen directory into the registry and reads it back
/// on upgrade, so a mismatch means the data the previous version wrote is not
/// where this version will look. It is reported, never acted on here: deciding
/// what to do about it (scan, migrate, ask) belongs to the batch that changes
/// where a project directory lives, and guessing at it would move a user's
/// projects on a start that was not asked to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum InstallCheck {
    /// No record of an installation, so there is nothing to compare. A
    /// development run and a portable copy both land here, and neither is a
    /// fault: the check exists to catch a *changed* install location, and an
    /// installation that was never recorded has not changed.
    NotInstalled,
    /// The recorded directory and the running directory are the same.
    Unchanged,
    /// The registry records a different directory than the running one.
    Changed { recorded: PathBuf, running: PathBuf },
    /// The registry holds a record that could not be read as a path.
    Unreadable,
}

impl InstallCheck {
    /// Whether this start should be reported as running from a moved install.
    pub fn is_changed(&self) -> bool {
        matches!(self, Self::Changed { .. })
    }
}

/// The directory an installation of this application is running from.
///
/// `current_exe()` is the only answer that needs no configuration and no guess:
/// it is the directory the user double-clicked into. A path that cannot be
/// resolved is reported rather than replaced with a default, because substituting
/// one would make every later comparison agree about the wrong directory.
pub fn running_install_dir() -> Result<PathBuf, Error> {
    let executable = std::env::current_exe().map_err(|_| Error::Base)?;
    executable
        .parent()
        .map(Path::to_path_buf)
        .ok_or(Error::Base)
}

/// Compare the installation record with the directory this build runs from.
///
/// `recorded` is whatever the registry held. `None` means the key was absent,
/// which is the ordinary state of a development run, an unpacked build, and any
/// installation made before the installer started writing it -- so it is
/// `NotInstalled` and never an error.
///
/// Comparison is case-insensitive on Windows because the filesystem is, and it
/// normalizes separators so that a record written with a trailing slash does not
/// report a move that did not happen.
pub fn check_install(recorded: Option<&Path>, running: &Path) -> InstallCheck {
    let Some(recorded) = recorded else {
        return InstallCheck::NotInstalled;
    };
    let recorded = recorded.to_path_buf();
    if recorded.as_os_str().is_empty() {
        return InstallCheck::Unreadable;
    }
    if same_directory(&recorded, running) {
        return InstallCheck::Unchanged;
    }
    InstallCheck::Changed {
        recorded,
        running: running.to_path_buf(),
    }
}

/// Whether two spellings name the same directory on this platform.
///
/// Neither side is resolved: `canonicalize()` fails for a recorded directory that
/// has since been removed, and that case must still be reported as unchanged
/// rather than as a second, unrelated failure. Only spelling is compared, which
/// is enough because both sides name a directory this application installed.
fn same_directory(left: &Path, right: &Path) -> bool {
    let left = normalized(left);
    let right = normalized(right);
    if cfg!(windows) {
        left.eq_ignore_ascii_case(&right)
    } else {
        left == right
    }
}

/// One directory spelling: forward slashes, no trailing separator.
fn normalized(path: &Path) -> String {
    let text = path.to_string_lossy().replace('\\', "/");
    if text.len() > 1 {
        text.trim_end_matches('/').to_string()
    } else {
        text
    }
}

/// Registry key the installer writes the chosen directory into.
///
/// The bundler derives both components from `tauri.conf.json`: with no
/// `publisher` set, the manufacturer falls back to the second component of the
/// bundle identifier (`org.wenyi.desktop` -> `wenyi`), and the product name is
/// `productName`. Spelling the result here rather than reading it keeps the
/// resolver independent of build-time configuration, and a test pins both
/// components against that file so they cannot drift apart in silence.
pub const INSTALL_KEY: &str = "Software\\wenyi\\Wenyi Desktop";

/// The run of installations this build can compare itself against.
///
/// `None` is the ordinary answer for a development run and for an installation
/// that predates the record, and it is deliberately not an error: the check exists
/// to notice a location that changed, and nothing changed if nothing was recorded.
pub fn recorded_install_dir() -> Option<PathBuf> {
    read_install_dir()
}

#[cfg(windows)]
fn read_install_dir() -> Option<PathBuf> {
    use windows_sys::Win32::System::Registry::{
        RegCloseKey, RegOpenKeyExW, RegQueryValueExW, HKEY_CURRENT_USER, KEY_READ, REG_SZ,
    };

    // The value is the key's default value, which NSIS writes with no name.
    let wide = |text: &str| -> Vec<u16> {
        let mut units: Vec<u16> = text.encode_utf16().collect();
        units.push(0);
        units
    };
    let key = wide(INSTALL_KEY);
    let value = wide("");
    let mut handle: windows_sys::Win32::System::Registry::HKEY = std::ptr::null_mut();
    // SAFETY: both strings are null-terminated and outlive the call, and the
    // handle is closed below on every path that opened it.
    let opened =
        unsafe { RegOpenKeyExW(HKEY_CURRENT_USER, key.as_ptr(), 0, KEY_READ, &mut handle) };
    if opened != 0 {
        return None;
    }
    let mut kind = 0u32;
    let mut bytes = 0u32;
    // SAFETY: the handle is open, and a null buffer with a zero length is the
    // documented way to ask how much space the value needs.
    let sized = unsafe {
        RegQueryValueExW(
            handle,
            value.as_ptr(),
            std::ptr::null_mut(),
            &mut kind,
            std::ptr::null_mut(),
            &mut bytes,
        )
    };
    if sized != 0 || kind != REG_SZ || bytes < 2 {
        // SAFETY: the handle was opened above and is closed exactly once.
        unsafe { RegCloseKey(handle) };
        return None;
    }
    let mut buffer = vec![0u16; (bytes as usize).div_ceil(2)];
    let mut written = bytes;
    // SAFETY: the buffer holds the size that was just queried, and `written` is
    // that same length in bytes.
    let read = unsafe {
        RegQueryValueExW(
            handle,
            value.as_ptr(),
            std::ptr::null_mut(),
            &mut kind,
            buffer.as_mut_ptr().cast(),
            &mut written,
        )
    };
    // SAFETY: the handle was opened above and is closed exactly once.
    unsafe { RegCloseKey(handle) };
    if read != 0 {
        return None;
    }
    let end = buffer
        .iter()
        .position(|unit| *unit == 0)
        .unwrap_or(buffer.len());
    let text = String::from_utf16_lossy(&buffer[..end]);
    let text = text.trim();
    // A record that is not a path is reported as no record: comparing this build
    // against a value the installer could not have written would only produce a
    // warning nobody can act on.
    let path = PathBuf::from(text);
    path.is_absolute().then_some(path)
}

/// Every other platform has no installer record, so no comparison is made.
#[cfg(not(windows))]
fn read_install_dir() -> Option<PathBuf> {
    None
}

/// Compare this build against the installation the registry records.
pub fn current_install() -> InstallCheck {
    let recorded = recorded_install_dir();
    match running_install_dir() {
        Ok(running) => check_install(recorded.as_deref(), &running),
        // A running directory that cannot be named is not a moved installation;
        // reporting one would send the user looking for a directory problem that
        // this start never observed.
        Err(_) => InstallCheck::NotInstalled,
    }
}

/// The directory a person may choose for layer three, or the reason they may not.
///
/// This is the creation-time half of the same rule the startup assertion applies:
/// a library root the application accepts here must be one the rest of the layout
/// can live beside. Six categories are refused, and they are all instances of one
/// question -- is this directory the installation, the profile, or a root that a
/// recursive removal would take whole?
///
/// No category is spelled as a list of paths to compare against. Every refusal is
/// derived from the two roots the application already resolves, so a machine
/// configured differently is judged by the same rule rather than by a copy of one
/// machine's idea of a dangerous path.
pub fn admit_library_root(
    candidate: &Path,
    install_dir: &Path,
    workspace: &Path,
) -> Result<(), Error> {
    // A relative or empty spelling names nothing, and would resolve against
    // whatever directory the process happens to be in.
    if !candidate.is_absolute() || candidate.as_os_str().is_empty() {
        return Err(Error::LibraryRootInvalid);
    }
    // A drive root, a UNC share root, and the user profile are all "roots" in the
    // sense that matters: everything below them belongs to something else, and a
    // recursive removal in one takes unrelated work with it.
    let at_volume_root = candidate
        .parent()
        .is_none_or(|parent| parent == candidate || parent.as_os_str().is_empty());
    if at_volume_root {
        return Err(Error::LibraryRootInvalid);
    }
    let user = home_dir().map_err(|_| Error::Base)?;
    if same_directory(candidate, &user) {
        return Err(Error::LibraryRootInvalid);
    }
    let system = system_directories();
    let roots: [(&'static str, PathBuf); 4] = [
        ("install", install_dir.to_path_buf()),
        ("layer-one", runtime_root().map_err(|_| Error::Base)?),
        ("layer-two", user.clone().join(USER)),
        // The whole profile is refused for the same reason a volume root is: its
        // children are other applications' directories.
        ("profile", user),
    ];
    // Only the candidate is compared against each root. Asking the pairwise check
    // to judge the whole set would fail on the roots themselves: layer one and
    // layer two are both inside the profile, which is exactly why the profile is
    // one of the four.
    if roots.iter().any(|(_, root)| overlaps(candidate, root)) {
        return Err(Error::LibraryRootInvalid);
    }
    // The workspace is the directory the engine was actually given, and it is not
    // necessarily the platform default: it can be named on the command line, so it
    // is compared as its own participant rather than assumed to be under anything.
    if overlaps(candidate, workspace) {
        return Err(Error::LibraryRootInvalid);
    }
    if system.iter().any(|path| overlaps(candidate, path)) {
        return Err(Error::LibraryRootInvalid);
    }
    Ok(())
}

/// Directories the operating system owns, in the spelling the platform uses.
///
/// These are read from the environment the operating system sets for its own
/// directories rather than typed out, so a non-English installation is judged by
/// where its system directories actually are.
fn system_directories() -> Vec<PathBuf> {
    let names: &[&str] = if cfg!(windows) {
        &[
            "SystemRoot",
            "windir",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "ProgramData",
        ]
    } else {
        &["SystemRoot"]
    };
    let mut found: Vec<PathBuf> = names
        .iter()
        .filter_map(|name| env_path(name))
        .filter(|path| path.is_absolute())
        .collect();
    if cfg!(windows) {
        // /Windows/System32 is what an application can actually name, and it is a
        // descendant of the root the variables above describe.
        found.extend(env_path("SystemRoot").map(|root| root.join("System32")));
    } else {
        found.extend(
            ["/usr", "/bin", "/sbin", "/etc", "/boot", "/var"]
                .iter()
                .map(PathBuf::from),
        );
    }
    found
}

/// Whether two roots are equal or nested either way, comparing whole components.
///
/// Resolving is deliberately avoided: the candidate is a directory that may not
/// exist yet, and a path that cannot be resolved must be refused on its spelling
/// rather than admitted because the check could not run.
fn overlaps(left: &Path, right: &Path) -> bool {
    let left = normalized(left);
    let right = normalized(right);
    let (left, right) = if cfg!(windows) {
        (left.to_lowercase(), right.to_lowercase())
    } else {
        (left, right)
    };
    let boundary =
        |prefix: &str, whole: &str| whole == prefix || whole.starts_with(&format!("{prefix}/"));
    boundary(&left, &right) || boundary(&right, &left)
}

/// Roots must be pairwise disjoint: neither equal nor nested either way, because
/// cleaning, backing up, or uninstalling one must never reach another.
///
/// Two callers depend on this shape. The pre-upgrade snapshot refuses a backup
/// directory that sits inside the data directory it is about to copy, and the
/// library-root admission refuses a location that is the installation, the
/// workspace, or either user-level layer.
///
/// Comparison is by whole path component and case-insensitive on Windows, through
/// the same rules the admission uses. A spelling mismatch would make the opposite
/// mistake: an installation inside a user's data directory would pass in silence,
/// because "D:/Apps/Wenyi" and "d:/apps/wenyi" are one directory to the filesystem
/// and two strings to a byte comparison. It would also refuse layer one beside
/// layer two on a profile whose two halves differ by case, which on Windows they
/// cannot, and on any other platform they must not.
pub fn check_disjoint(roots: &[(&'static str, &Path)]) -> Result<(), Error> {
    for (index, (_, path)) in roots.iter().enumerate() {
        for (_, other) in roots[index + 1..].iter() {
            if overlaps(path, other) {
                return Err(Error::Overlap);
            }
        }
    }
    Ok(())
}

/// The two user-level layers, in the order the layout names them.
///
/// Layers one and two are derivable on every platform from the profile alone, so
/// the installer applies the same pair to `$INSTDIR` before a single file is
/// copied. This function is the single spelling of that pair on the native side;
/// the NSIS hook has to repeat it, because the two programs share no code, and a
/// test pins the repeated spelling against these literals.
pub fn user_layers() -> Result<[(&'static str, PathBuf); 2], Error> {
    Ok([("layer-one", runtime_root()?), ("layer-two", user_root()?)])
}

/// The layer-three root this start will actually use, when it can name one.
///
/// `None` is the ordinary answer for a development run, a test, and an unpacked
/// build: none of them has an installer record, so none of them has a directory
/// beside an installation. It is also the answer for a refused choice — an
/// installation at a volume root has no sibling, and `main` then runs without a
/// layer-three root rather than inventing one. The startup assertion reports the
/// roots it is given and never derives a fourth.
pub fn layer_three(install: &InstallCheck, workspace: &Path) -> Option<PathBuf> {
    if matches!(
        install,
        InstallCheck::NotInstalled | InstallCheck::Unreadable
    ) {
        return None;
    }
    let install_dir = running_install_dir().ok()?;
    let library = library_root(&install_dir).ok()?;
    admit_library_root(&library, &install_dir, workspace)
        .ok()
        .map(|()| library)
}

/// What this start found when it compared the four parties of the layout.
///
/// The four are layers one and two, layer three, and the installation directory.
/// `check_disjoint` is called with all of them at once, so the layout is admitted
/// only when no two of the six pairs share a directory or contain one another.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LayoutCheck {
    /// Every pair answered, and the layout is live.
    Consistent,
    /// Two of the four parties share a directory or contain one another.
    Overlap,
    /// One of the user-level roots cannot be resolved on this computer.
    Unresolved,
}

/// Whether the layers and the installation directory are pairwise disjoint.
///
/// This is the startup half of the layout rule; the installer applies its own
/// half to `$INSTDIR` before copying anything, and admitting a new layer-three
/// folder applies a third to the folder a person picked. All three exist because
/// each one runs when the others cannot: the installer never runs again, and the
/// admission only ever sees a folder someone chose.
///
/// The participants are supplied rather than resolved here, so that the caller
/// decides what a development run contributes. `library` is `None` when no
/// installation was recorded, which is the state of every `cargo run`, every test,
/// and every unpacked build: with three parties the three pairs that remain are
/// still checked, and a start that would fail because a developer has no
/// installer record never happens.
pub fn check_layout(install_dir: &Path, library: Option<&Path>) -> LayoutCheck {
    let Some(layers) = user_layers().ok() else {
        return LayoutCheck::Unresolved;
    };
    let mut roots: Vec<(&'static str, &Path)> = Vec::with_capacity(4);
    roots.extend(layers.iter().map(|(name, path)| (*name, path.as_path())));
    roots.push(("install", install_dir));
    if let Some(library) = library {
        roots.push(("layer-three", library));
    }
    match check_disjoint(&roots) {
        Ok(()) => LayoutCheck::Consistent,
        Err(_) => LayoutCheck::Overlap,
    }
}

impl LayoutCheck {
    /// The message shown when a start is refused, or `None` when it proceeds.
    ///
    /// A refusal has to say what to do, because the user cannot see a directory
    /// layout: the file that explains the arrangement is the one below, and it is
    /// written before this runs.
    pub fn message(&self) -> Option<&'static str> {
        match self {
            Self::Consistent => None,
            Self::Overlap => Some(
                "Wenyi's data folders overlap. One of them is inside another, which would let an upgrade or an uninstall remove more than it should. Move the installation or the project folder so the two are separate, then reopen Wenyi. The diagnostic log in the workspace records which folders were compared.",
            ),
            Self::Unresolved => Some(
                "Wenyi could not determine where its user folders are. Check that the user profile is available, then reopen Wenyi. The diagnostic log in the workspace records which folders were compared.",
            ),
        }
    }
}

/// Platform base the workspace and its log fallback derive from.
///
/// The chain is kept byte-for-byte as it was before this module existed, so the
/// paths an installed application uses do not move. The environment variable
/// still comes first for that reason: an existing installation, and every
/// workspace a person has already created beside it, is named by it.
fn data_base() -> Result<PathBuf, Error> {
    #[cfg(windows)]
    {
        env_path("LOCALAPPDATA")
            .or_else(local_app_data)
            .ok_or(Error::Base)
    }
    #[cfg(target_os = "macos")]
    {
        let home = env_path("HOME").ok_or(Error::Base)?;
        Ok(home.join("Library/Application Support"))
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        match env_path("XDG_DATA_HOME") {
            Some(path) => Ok(path),
            None => {
                let home = env_path("HOME").ok_or(Error::Base)?;
                Ok(home.join(".local/share"))
            }
        }
    }
}

/// Platform base for the log fallback, matching the previous resolution order.
fn log_base() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        env_path("LOCALAPPDATA").or_else(local_app_data)
    }
    #[cfg(target_os = "macos")]
    {
        env_path("HOME").map(|path| path.join("Library/Logs"))
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        env_path("XDG_STATE_HOME")
            .or_else(|| env_path("HOME").map(|path| path.join(".local/state")))
    }
}

/// The user profile that owns layers one and two.
fn home_dir() -> Result<PathBuf, Error> {
    #[cfg(windows)]
    {
        env_path("USERPROFILE").ok_or(Error::Base)
    }
    #[cfg(not(windows))]
    {
        env_path("HOME").ok_or(Error::Base)
    }
}

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name).map(PathBuf::from)
}

/// The local application data directory as the shell knows it.
///
/// `%LOCALAPPDATA%` is a convenience variable, not the definition: a process
/// started without the user environment -- a scheduled task, a service, a
/// shortcut that clears its block, an installer run from an elevated shell --
/// inherits no value at all. The known folder is the definition, and it answers
/// in exactly those processes.
///
/// The fallback is deliberately second. Reading the variable first keeps every
/// existing workspace where it is, since a variable a person set on purpose (a
/// roaming-profile test, a redirection to another drive) would otherwise be
/// overruled by the shell's answer and the projects would appear to vanish.
#[cfg(windows)]
fn local_app_data() -> Option<PathBuf> {
    use std::ffi::OsString;
    use std::os::windows::ffi::OsStringExt;
    use windows_sys::Win32::System::Com::CoTaskMemFree;
    use windows_sys::Win32::UI::Shell::{FOLDERID_LocalAppData, SHGetKnownFolderPath};

    let mut raw: *mut u16 = std::ptr::null_mut();
    // SAFETY: `raw` is a valid out-pointer for a `PWSTR`. The call either
    // leaves it null or fills it with an allocation this function owns and
    // frees below, which is the contract `SHGetKnownFolderPath` documents.
    let status =
        unsafe { SHGetKnownFolderPath(&FOLDERID_LocalAppData, 0, std::ptr::null_mut(), &mut raw) };
    if status < 0 || raw.is_null() {
        if !raw.is_null() {
            // SAFETY: non-null means the shell allocated the string.
            unsafe { CoTaskMemFree(raw.cast()) };
        }
        return None;
    }
    let length = (0..).take_while(|offset| *offset < 32768).find(|offset| {
        // SAFETY: the allocation is NUL-terminated by contract; the bound above
        // keeps a non-conforming value from reading without end.
        unsafe { *raw.add(*offset) == 0 }
    });
    let Some(length) = length else {
        // SAFETY: non-null means the shell allocated the string.
        unsafe { CoTaskMemFree(raw.cast()) };
        return None;
    };
    // SAFETY: `length` was just measured inside the same allocation.
    let value = unsafe { std::slice::from_raw_parts(raw, length) };
    let path = PathBuf::from(OsString::from_wide(value));
    // SAFETY: non-null means the shell allocated the string, and it is no longer
    // read here: `path` has copied the contents.
    unsafe { CoTaskMemFree(raw.cast()) };
    path.is_absolute().then_some(path)
}

/// An override is honoured only when it is a real, absolute path. A blank value
/// means "not set" so that no configuration can silently select the working
/// directory.
fn override_path(name: &str) -> Option<PathBuf> {
    let value = std::env::var_os(name)?;
    if value.to_string_lossy().trim().is_empty() {
        return None;
    }
    let path = PathBuf::from(value);
    path.is_absolute().then_some(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[cfg(windows)]
    fn the_known_folder_answers_where_the_variable_does() {
        // The fallback only has to be equivalent. If it named a different
        // directory, a process without the environment would open a second,
        // empty workspace and every project would look deleted.
        let known = local_app_data().expect("the shell names a local app data folder");
        assert!(known.is_absolute());
        assert!(known.is_dir());
        if let Some(configured) = env_path("LOCALAPPDATA") {
            // Compared case-insensitively: Windows path identity does not
            // depend on the spelling of its components.
            assert_eq!(
                known.to_string_lossy().to_lowercase(),
                configured.to_string_lossy().to_lowercase()
            );
        }
    }

    #[test]
    fn the_workspace_name_is_spelled_once() {
        assert_eq!(
            Path::new("C:/profile").join(PRODUCT),
            Path::new("C:/profile/Wenyi Desktop")
        );
    }

    #[test]
    fn the_workspace_copy_of_the_logs_comes_first() {
        let workspace = Path::new("C:/profile/Wenyi Desktop");
        let roots = log_roots(workspace);
        assert_eq!(roots[0], workspace.join("diagnostics"));
        // The fallback depends on the platform base, so it may be absent here.
        assert!(roots.len() <= 2);
        let identity = format!(
            "{:x}",
            Sha256::digest(workspace.to_string_lossy().as_bytes())
        );
        for root in &roots[1..] {
            assert!(root.to_string_lossy().contains(LOGS));
            // One directory per workspace identity, never a shared file name.
            assert_eq!(root.file_name().unwrap().to_str().unwrap(), &identity[..16]);
        }
    }

    #[test]
    fn several_workspaces_do_not_share_one_log_directory() {
        let first = log_roots(Path::new("C:/profile/Wenyi Desktop"));
        let second = log_roots(Path::new("D:/work/Wenyi Desktop"));
        assert_eq!(first[0], Path::new("C:/profile/Wenyi Desktop/diagnostics"));
        assert_eq!(second[0], Path::new("D:/work/Wenyi Desktop/diagnostics"));
        assert_ne!(first, second);
    }

    #[test]
    fn an_installed_directory_gets_a_sibling_library_root() {
        assert_eq!(
            library_root(Path::new("D:/Apps/Wenyi")).unwrap(),
            PathBuf::from("D:/Apps/Wenyi Desktop Data")
        );
    }

    #[test]
    #[cfg(windows)]
    fn a_volume_root_is_refused_instead_of_invented() {
        for install in ["D:/", "D:"] {
            assert_eq!(
                library_root(Path::new(install)).err(),
                Some(Error::InstallDirAtVolumeRoot)
            );
        }
    }

    #[test]
    fn the_install_record_key_matches_the_bundler_configuration() {
        // The bundler derives the manufacturer from the second component of the
        // identifier when no publisher is set, and the product name from
        // productName. A change to either would move the key, and the check would
        // then compare against a record that does not exist -- reporting nothing,
        // forever, without a single test failing.
        let root = Path::new(env!("CARGO_MANIFEST_DIR"));
        let config: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(root.join("tauri.conf.json")).unwrap())
                .unwrap();
        let identifier = config["identifier"].as_str().unwrap();
        assert!(
            config["publisher"].is_null(),
            "a publisher would replace the derived manufacturer"
        );
        let manufacturer = identifier.split('.').nth(1).unwrap();
        let product = config["productName"].as_str().unwrap();
        assert_eq!(
            INSTALL_KEY,
            format!("Software\\{manufacturer}\\{product}"),
            "INSTALL_KEY must name the key installer.nsi writes"
        );
    }

    #[test]
    fn an_installation_with_no_record_is_not_a_moved_installation() {
        // This is the state of every development run and every unpacked build. It
        // must be reported as "nothing to compare", never as a fault: a check that
        // fires on a developer's machine is a check that gets disabled.
        assert_eq!(
            check_install(None, Path::new("D:/Apps/Wenyi Desktop")),
            InstallCheck::NotInstalled
        );
        assert!(!check_install(None, Path::new("D:/Apps/Wenyi Desktop")).is_changed());
    }

    #[test]
    fn a_record_that_matches_the_running_directory_is_unchanged() {
        let running = Path::new("D:/Apps/Wenyi Desktop");
        for recorded in [
            "D:/Apps/Wenyi Desktop",
            "D:\\Apps\\Wenyi Desktop",
            "D:/Apps/Wenyi Desktop/",
            "d:/apps/wenyi desktop",
        ] {
            assert_eq!(
                check_install(Some(Path::new(recorded)), running),
                InstallCheck::Unchanged,
                "{recorded} names the directory this build runs from"
            );
        }
    }

    #[test]
    fn a_record_that_names_another_directory_is_reported() {
        let recorded = Path::new("D:/Apps/Wenyi Desktop");
        let running = Path::new("E:/Other/Wenyi Desktop");
        assert_eq!(
            check_install(Some(recorded), running),
            InstallCheck::Changed {
                recorded: recorded.to_path_buf(),
                running: running.to_path_buf(),
            }
        );
        assert!(check_install(Some(recorded), running).is_changed());
        // A nested directory is not the same directory: the installer records one
        // location, and a build running below it is a different installation.
        assert!(check_install(Some(recorded), &recorded.join("bin")).is_changed());
    }

    #[test]
    fn a_record_that_is_not_a_path_is_unreadable() {
        assert_eq!(
            check_install(Some(Path::new("")), Path::new("D:/Apps/Wenyi Desktop")),
            InstallCheck::Unreadable
        );
    }

    #[test]
    fn the_running_install_directory_is_the_one_holding_the_executable() {
        // current_exe() is the only answer that needs no configuration and no
        // guess; substituting a default would make every later comparison agree
        // about the wrong directory.
        let running = running_install_dir().unwrap();
        let executable = std::env::current_exe().unwrap();
        assert_eq!(running, executable.parent().unwrap());
    }

    #[test]
    fn a_chosen_library_root_beside_the_installation_is_admitted() {
        // The default layer-three location: a sibling of the installation, which
        // no uninstall or upgrade policy can reach.
        let install = Path::new("D:/Apps/Wenyi Desktop");
        let library = library_root(install).unwrap();
        assert_eq!(
            admit_library_root(&library, install, Path::new("C:/profile/Wenyi Desktop")),
            Ok(())
        );
    }

    #[test]
    fn the_installation_and_its_ancestors_are_refused() {
        let install = Path::new("D:/Apps/Wenyi Desktop");
        for candidate in [
            Path::new("D:/Apps/Wenyi Desktop"),
            Path::new("D:/Apps/Wenyi Desktop/data"),
            // An ancestor is the more dangerous case: a recursive removal of the
            // chosen folder would take the installation with it.
            Path::new("D:/Apps"),
            Path::new("D:/"),
        ] {
            assert_eq!(
                admit_library_root(candidate, install, Path::new("C:/profile/Wenyi Desktop")).err(),
                Some(Error::LibraryRootInvalid),
                "{candidate:?} must not be admitted"
            );
        }
    }

    #[test]
    fn the_volume_root_and_relative_spellings_are_refused() {
        let install = Path::new("D:/Apps/Wenyi Desktop");
        let workspace = Path::new("C:/profile/Wenyi Desktop");
        for candidate in [Path::new(""), Path::new("relative/folder"), Path::new(".")] {
            assert_eq!(
                admit_library_root(candidate, install, workspace).err(),
                Some(Error::LibraryRootInvalid),
                "{candidate:?} is not an absolute location"
            );
        }
    }

    #[test]
    fn sibling_names_that_merely_share_a_prefix_do_not_overlap() {
        // D:/Apps and D:/Apps2 share a prefix and neither contains the other.
        // Comparing whole components is what keeps that from being a silent pass.
        assert!(!overlaps(Path::new("D:/Apps"), Path::new("D:/Apps2")));
        assert!(overlaps(Path::new("D:/Apps"), Path::new("D:/Apps/data")));
        assert!(!overlaps(Path::new("D:/Apps"), Path::new("D:/Apps2/data")));
    }

    #[test]
    fn the_four_parties_are_admitted_only_when_pairwise_disjoint() {
        // The layout of an installation whose layer three is the sibling directory
        // the default names. This is the ordinary case, and it must pass.
        let home = Path::new("C:/Users/Reader");
        let runtime = home.join(".wenyi/desktop");
        let user = home.join("Wenyi");
        let install = Path::new("D:/Apps/Wenyi Desktop");
        for (first, second) in [
            (runtime.clone(), user.clone()),
            (runtime.clone(), install.to_path_buf()),
            (user.clone(), install.to_path_buf()),
        ] {
            assert_eq!(
                check_disjoint(&[("first", &first), ("second", &second)]),
                Ok(()),
                "{first:?} and {second:?} are separate"
            );
        }
        // The installation sits beside layer three, not inside it, so the fourth
        // pair is separate as well.
        let library = library_root(install).unwrap();
        assert_eq!(
            check_disjoint(&[
                ("layer-one", &runtime),
                ("layer-two", &user),
                ("install", install),
                ("layer-three", &library),
            ]),
            Ok(())
        );
    }

    #[test]
    fn an_installation_inside_a_layer_is_rejected_whatever_the_spelling() {
        // "相等的" and "互为祖先" are the same failure for this check, and the
        // spelling must not decide it: the filesystem treats these as one directory.
        let install = Path::new("C:/Users/Reader/Wenyi/Install");
        for layer in [
            Path::new("C:/Users/Reader/Wenyi"),
            Path::new("c:/users/reader/wenyi"),
            Path::new("C:\\Users\\Reader\\Wenyi"),
            install,
        ] {
            assert_eq!(
                check_disjoint(&[("layer-two", layer), ("install", install)]).err(),
                Some(Error::Overlap),
                "{layer:?} and {install:?} must not be admitted"
            );
        }
    }

    #[test]
    fn sibling_directories_that_share_a_name_prefix_are_admitted() {
        // A byte-prefix test would reject "Wenyi Desktop Data" beside "Wenyi
        // Desktop". Whole components are what make the fourth pair work at all.
        let install = Path::new("D:/Apps/Wenyi Desktop");
        let library = Path::new("D:/Apps/Wenyi Desktop Data");
        assert_eq!(
            check_disjoint(&[("install", install), ("layer-three", library)]),
            Ok(())
        );
    }

    #[test]
    fn a_development_run_contributes_three_parties_and_still_passes() {
        // With no installation record there is no layer three. The remaining
        // pairs are still checked; what must never happen is a start that fails
        // because a developer has no installer record.
        let home = env_path(if cfg!(windows) { "USERPROFILE" } else { "HOME" });
        let Some(home) = home else {
            return;
        };
        let running = match running_install_dir() {
            Ok(running) => running,
            Err(_) => return,
        };
        assert_eq!(
            check_layout(&running, None),
            LayoutCheck::Consistent,
            "a run outside any installation must not be refused"
        );
        assert!(LayoutCheck::Consistent.message().is_none());
        // And the same three parties are not where the engine was told to work.
        assert_eq!(
            check_disjoint(&[("layer-two", &home.join(USER)), ("install", &running)]),
            Ok(())
        );
    }

    #[test]
    fn a_refusal_always_explains_what_to_do() {
        // A refusal the user cannot act on is a refusal that gets reported as a
        // broken application. Both failure kinds carry a message, and the two
        // messages differ so that the log says which rule was broken.
        let overlap = LayoutCheck::Overlap.message().unwrap();
        let unresolved = LayoutCheck::Unresolved.message().unwrap();
        assert_ne!(overlap, unresolved);
        assert!(overlap.contains("overlap"));
        assert!(overlap.contains("reopen Wenyi"));
        assert!(unresolved.contains("user profile"));
    }

    #[test]
    fn the_user_layers_are_the_two_documented_directories() {
        let Ok(layers) = user_layers() else {
            return;
        };
        assert_eq!(layers[0].0, "layer-one");
        assert_eq!(layers[1].0, "layer-two");
        assert_eq!(layers[0].1.file_name().unwrap(), ENTRY);
        assert_eq!(layers[1].1.file_name().unwrap(), USER);
        let roots: Vec<(&'static str, &Path)> = layers
            .iter()
            .map(|(name, path)| (*name, path.as_path()))
            .collect();
        assert_eq!(check_disjoint(&roots), Ok(()));
    }

    #[test]
    fn an_unrecorded_installation_has_no_layer_three() {
        // The development door of the assertion. Nothing is derived for a build
        // that was never installed, so the assertion cannot refuse a start for a
        // directory nobody chose.
        for install in [InstallCheck::NotInstalled, InstallCheck::Unreadable] {
            assert_eq!(
                layer_three(&install, Path::new("C:/profile/Wenyi Desktop")),
                None
            );
        }
    }

    #[test]
    fn overlapping_roots_are_rejected() {
        for (first, second) in [
            ("D:/Apps", "D:/Apps"),
            ("D:/Apps", "D:/Apps/Wenyi"),
            ("D:/Apps/Wenyi", "D:/Apps"),
        ] {
            assert_eq!(
                check_disjoint(&[("first", Path::new(first)), ("second", Path::new(second))]).err(),
                Some(Error::Overlap)
            );
        }
        assert_eq!(
            check_disjoint(&[
                ("one", Path::new("C:/profile/.wenyi/desktop")),
                ("two", Path::new("C:/profile/Wenyi")),
                ("three", Path::new("D:/Apps/Wenyi Desktop Data")),
            ]),
            Ok(())
        );
    }
}
