//! Windows toast delivery for the platform-independent notification center.
//!
//! All WinRT calls happen on the center's dedicated dispatcher thread, which is
//! initialized as a multithreaded apartment. Nothing here decides what should be
//! shown: the notice was validated, deduplicated and localized before it arrived.

use std::path::{Path, PathBuf};

use ::windows::core::HSTRING;
use ::windows::Data::Xml::Dom::XmlDocument;
use ::windows::UI::Notifications::{
    NotificationSetting, ToastNotification, ToastNotificationManager, ToastNotifier,
};
use tauri::Manager;

use super::{Failure, Presentation, Sink, AUMID};

/// A 64 px logo for toast banners. Embedded so development and installed builds
/// write the same asset next to their runtime state and reference it by URI.
const TOAST_ICON: &[u8] = include_bytes!("../../icons/toast.png");

/// Set the process identity before the first window exists.
///
/// Windows groups the process, its taskbar button and its toasts by this value.
/// The installer registers the same identity on its shortcuts; this also writes
/// the per-user registration Windows needs to resolve the display name and icon
/// for toasts from an unpackaged build.
pub fn set_app_user_model_id() {
    let value: Vec<u16> = AUMID.encode_utf16().chain(std::iter::once(0)).collect();
    // SAFETY: the buffer is NUL-terminated and only read during this call.
    unsafe {
        windows_sys::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID(value.as_ptr());
    }
    register_aumid();
}

/// Write the embedded logo into the runtime directory and point the app identity
/// at it. Icon usage is best-effort: a failure leaves the text-only toast.
fn prepare_icon(workspace: &Path) -> Option<PathBuf> {
    let directory = crate::paths::runtime_root(workspace);
    std::fs::create_dir_all(&directory).ok()?;
    let path = directory.join("toast.png");
    if std::fs::read(&path).ok().as_deref() != Some(TOAST_ICON) {
        std::fs::write(&path, TOAST_ICON).ok()?;
    }
    register_icon(&path);
    Some(path)
}

/// Best-effort per-user registration under HKCU, so an unpackaged build has an
/// identity that the notification platform can resolve. Failures are harmless:
/// the explicit process identity still applies.
fn register_aumid() {
    let Some(key) = open_identity_key() else {
        return;
    };
    write_string(key, "DisplayName", "Wenyi Desktop");
    if let Ok(executable) = std::env::current_exe() {
        if let Some(path) = executable.to_str() {
            write_string(key, "IconUri", path);
        }
    }
    // SAFETY: the handle came from open_identity_key and is not used afterwards.
    unsafe {
        windows_sys::Win32::System::Registry::RegCloseKey(key);
    }
}

fn register_icon(path: &Path) {
    let Some(key) = open_identity_key() else {
        return;
    };
    write_string(key, "IconUri", &path.to_string_lossy());
    // SAFETY: the handle came from open_identity_key and is not used afterwards.
    unsafe {
        windows_sys::Win32::System::Registry::RegCloseKey(key);
    }
}

fn open_identity_key() -> Option<windows_sys::Win32::System::Registry::HKEY> {
    use windows_sys::Win32::Foundation::ERROR_SUCCESS;
    use windows_sys::Win32::System::Registry::{RegCreateKeyW, HKEY, HKEY_CURRENT_USER};

    let subkey = wide(&format!("Software\\Classes\\AppUserModelId\\{AUMID}"));
    let mut key: HKEY = std::ptr::null_mut();
    // SAFETY: the subkey is NUL-terminated and outlives the call.
    let status = unsafe { RegCreateKeyW(HKEY_CURRENT_USER, subkey.as_ptr(), &mut key) };
    if status != ERROR_SUCCESS {
        return None;
    }
    Some(key)
}

fn write_string(key: windows_sys::Win32::System::Registry::HKEY, name: &str, value: &str) {
    use windows_sys::Win32::System::Registry::{RegSetValueExW, REG_SZ};

    let name = wide(name);
    let value = wide(value);
    // SAFETY: the data buffer is NUL-terminated; the byte length includes it.
    unsafe {
        RegSetValueExW(
            key,
            name.as_ptr(),
            0,
            REG_SZ,
            value.as_ptr() as *const u8,
            (value.len() * 2) as u32,
        );
    }
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Build a file URI for the toast XML. Spaces and URI delimiters must be
/// percent-encoded; Windows paths use backslashes.
fn file_uri(path: &Path) -> String {
    let mut encoded = String::new();
    for character in path.to_string_lossy().replace('\\', "/").chars() {
        match character {
            '%' => encoded.push_str("%25"),
            ' ' => encoded.push_str("%20"),
            '#' => encoded.push_str("%23"),
            '?' => encoded.push_str("%3F"),
            other => encoded.push(other),
        }
    }
    format!("file:///{}", encoded.trim_start_matches('/'))
}

/// The language Windows would use for this user, or an empty string.
pub fn user_locale() -> String {
    use windows_sys::Win32::Globalization::GetUserDefaultLocaleName;

    let mut buffer = [0u16; 85];
    // SAFETY: the buffer is valid for its stated length and the call writes at
    // most that many UTF-16 units, including the terminator.
    let length = unsafe { GetUserDefaultLocaleName(buffer.as_mut_ptr(), buffer.len() as i32) };
    let count = usize::try_from(length).unwrap_or(0);
    if count <= 1 {
        return String::new();
    }
    String::from_utf16_lossy(&buffer[..count - 1])
}

pub struct WindowsSink {
    app: tauri::AppHandle,
    initialized: bool,
    icon: Option<PathBuf>,
}

impl WindowsSink {
    pub fn new(app: tauri::AppHandle, workspace: &Path) -> Self {
        Self {
            app,
            initialized: false,
            icon: prepare_icon(workspace),
        }
    }

    fn initialize(&mut self) {
        if self.initialized {
            return;
        }
        self.initialized = true;
        // SAFETY: initializing this dedicated thread's apartment has no other
        // preconditions. A failure leaves WinRT unavailable, which the callers
        // report as a bounded delivery classification.
        unsafe {
            windows_sys::Win32::System::Com::CoInitializeEx(
                std::ptr::null(),
                windows_sys::Win32::System::Com::COINIT_MULTITHREADED as u32,
            );
        }
    }

    fn notifier() -> ::windows::core::Result<ToastNotifier> {
        ToastNotificationManager::CreateToastNotifierWithId(&HSTRING::from(AUMID))
    }
}

impl Sink for WindowsSink {
    fn available(&mut self) -> Result<(), Failure> {
        self.initialize();
        let notifier = Self::notifier().map_err(|_| Failure("notifier_failed"))?;
        match notifier.Setting() {
            Ok(NotificationSetting::Enabled) => Ok(()),
            Ok(_) => Err(Failure("os_disabled")),
            Err(_) => Err(Failure("setting_failed")),
        }
    }

    fn show(&mut self, presentation: &Presentation) -> Result<(), Failure> {
        self.initialize();
        let notifier = Self::notifier().map_err(|_| Failure("notifier_failed"))?;
        match notifier.Setting() {
            Ok(NotificationSetting::Enabled) => {}
            Ok(_) => return Err(Failure("os_disabled")),
            Err(_) => return Err(Failure("setting_failed")),
        }
        let icon = self.icon.as_ref().map_or_else(String::new, |path| {
            format!(
                "<image placement=\"appLogoOverride\" hint-crop=\"circle\" src=\"{}\"/>",
                escape(&file_uri(path))
            )
        });
        let xml = format!(
            concat!(
                "<toast activationType=\"foreground\"><visual>",
                "<binding template=\"ToastGeneric\"><text>{}</text><text>{}</text>{}</binding>",
                "</visual>",
                // Desktop (unpackaged) toasts do not reliably fall back to the
                // implicit sound, so request the system default explicitly.
                "<audio src=\"ms-winsoundevent:Notification.Default\"/>",
                "</toast>"
            ),
            escape(&presentation.title),
            escape(&presentation.body),
            icon
        );
        let document = XmlDocument::new().map_err(|_| Failure("winrt_failed"))?;
        document
            .LoadXml(&HSTRING::from(xml))
            .map_err(|_| Failure("winrt_failed"))?;
        let toast = ToastNotification::CreateToastNotification(&document)
            .map_err(|_| Failure("winrt_failed"))?;
        toast
            .SetTag(&HSTRING::from(presentation.tag.as_str()))
            .map_err(|_| Failure("winrt_failed"))?;
        toast
            .SetGroup(&HSTRING::from(presentation.group.as_str()))
            .map_err(|_| Failure("winrt_failed"))?;
        notifier.Show(&toast).map_err(|_| Failure("winrt_failed"))
    }

    fn fallback(&mut self, _presentation: &Presentation) {
        if let Some(window) = self.app.get_webview_window("main") {
            if let Ok(hwnd) = window.hwnd() {
                let info = windows_sys::Win32::UI::WindowsAndMessaging::FLASHWINFO {
                    cbSize: std::mem::size_of::<
                        windows_sys::Win32::UI::WindowsAndMessaging::FLASHWINFO,
                    >() as u32,
                    hwnd: hwnd.0,
                    dwFlags: windows_sys::Win32::UI::WindowsAndMessaging::FLASHW_ALL
                        | windows_sys::Win32::UI::WindowsAndMessaging::FLASHW_TIMERNOFG,
                    uCount: 3,
                    dwTimeout: 0,
                };
                // SAFETY: the structure is fully initialized and only read here.
                unsafe {
                    windows_sys::Win32::UI::WindowsAndMessaging::FlashWindowEx(&info);
                }
            }
        }
    }

    fn shutdown(&mut self) {}
}

/// Escape text for the toast XML. Tag and group go through WinRT setters, so
/// only the two text nodes need this.
fn escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

#[cfg(test)]
mod tests {
    use super::file_uri;
    use std::path::Path;

    #[test]
    fn file_uri_encodes_backslashes_and_spaces() {
        assert_eq!(
            file_uri(Path::new(r"C:\Users\A B\Wenyi\toast.png")),
            "file:///C:/Users/A%20B/Wenyi/toast.png"
        );
    }
}
