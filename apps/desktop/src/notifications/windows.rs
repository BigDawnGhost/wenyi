//! Windows toast delivery for the platform-independent notification center.
//!
//! All WinRT calls happen on the center's dedicated dispatcher thread, which is
//! initialized as a multithreaded apartment. Nothing here decides what should be
//! shown: the notice was validated, deduplicated and localized before it arrived.

use ::windows::core::HSTRING;
use ::windows::Data::Xml::Dom::XmlDocument;
use ::windows::UI::Notifications::{
    NotificationSetting, ToastNotification, ToastNotificationManager, ToastNotifier,
};
use tauri::Manager;

use super::{Failure, Presentation, Sink, AUMID};

/// Set the process identity before the first window exists.
///
/// Windows groups the process, its taskbar button and its toasts by this value.
/// The installer registers the same identity, so an installed build resolves its
/// name and icon rather than falling back to the executable.
pub fn set_app_user_model_id() {
    let value: Vec<u16> = AUMID.encode_utf16().chain(std::iter::once(0)).collect();
    // SAFETY: the buffer is NUL-terminated and only read during this call.
    unsafe {
        windows_sys::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID(value.as_ptr());
    }
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
}

impl WindowsSink {
    pub fn new(app: tauri::AppHandle) -> Self {
        Self {
            app,
            initialized: false,
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
    fn available(&mut self) -> bool {
        self.initialize();
        Self::notifier()
            .and_then(|notifier| notifier.Setting())
            .is_ok_and(|setting| setting == NotificationSetting::Enabled)
    }

    fn show(&mut self, presentation: &Presentation) -> Result<(), Failure> {
        self.initialize();
        let notifier = Self::notifier().map_err(|_| Failure("unavailable"))?;
        if !matches!(notifier.Setting(), Ok(setting) if setting == NotificationSetting::Enabled) {
            return Err(Failure("os_disabled"));
        }
        let xml = format!(
            concat!(
                "<toast activationType=\"foreground\"><visual>",
                "<binding template=\"ToastGeneric\"><text>{}</text><text>{}</text></binding>",
                "</visual></toast>"
            ),
            escape(&presentation.title),
            escape(&presentation.body)
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
