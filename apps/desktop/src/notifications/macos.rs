//! UserNotifications for an installed .app bundle. Framework callbacks are
//! acknowledged with deadlines off the main thread; authorization is explicit.

use std::{cell::RefCell, ptr::NonNull, sync::mpsc, time::Duration};

use block2::RcBlock;
use objc2::{
    define_class, msg_send,
    rc::{autoreleasepool, Retained},
    runtime::{Bool, ProtocolObject},
    AnyThread,
};
use objc2_foundation::{NSBundle, NSError, NSLocale, NSObject, NSObjectProtocol, NSString};
use objc2_user_notifications::{
    UNAuthorizationOptions, UNAuthorizationStatus, UNMutableNotificationContent, UNNotification,
    UNNotificationPresentationOptions, UNNotificationRequest, UNNotificationSettings,
    UNNotificationSound, UNUserNotificationCenter, UNUserNotificationCenterDelegate,
};

use super::{request_attention, Failure, Permission, Presentation, Sink, APP_ID};

const DEADLINE: Duration = Duration::from_secs(2);
const AUTHORIZATION_DEADLINE: Duration = Duration::from_secs(90);

define_class!(
    // SAFETY: NSObject has no subclassing requirements. The delegate is stateless,
    // never touches UI, and may be called on the framework's callback queue.
    #[unsafe(super = NSObject)]
    struct ForegroundDelegate;

    unsafe impl NSObjectProtocol for ForegroundDelegate {}

    unsafe impl UNUserNotificationCenterDelegate for ForegroundDelegate {
        #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
        fn will_present(
            &self,
            _center: &UNUserNotificationCenter,
            _notification: &UNNotification,
            completion: &block2::DynBlock<dyn Fn(UNNotificationPresentationOptions)>,
        ) {
            // Alert also works before macOS 11's separate Banner/List options.
            #[allow(deprecated)]
            let options =
                UNNotificationPresentationOptions::Alert | UNNotificationPresentationOptions::Sound;
            completion.call((options,));
        }
    }
);

impl ForegroundDelegate {
    fn new() -> Retained<Self> {
        // SAFETY: this stateless NSObject subclass inherits NSObject's init.
        unsafe { msg_send![Self::alloc(), init] }
    }
}

thread_local! {
    // UNUserNotificationCenter holds its delegate weakly. Keep the strong owner
    // on the dispatcher, not in a Send wrapper around an Objective-C pointer.
    static DELEGATE: RefCell<Option<Retained<ForegroundDelegate>>> = const { RefCell::new(None) };
}

fn center() -> Result<Retained<UNUserNotificationCenter>, Failure> {
    // currentNotificationCenter can raise an exception for an unbundled binary.
    // Do not impersonate another app or mutate bundle identity for cargo run.
    if NSBundle::mainBundle()
        .bundleIdentifier()
        .as_deref()
        .map(|id| id.to_string())
        .as_deref()
        != Some(APP_ID)
    {
        return Err(Failure::Unavailable);
    }
    Ok(UNUserNotificationCenter::currentNotificationCenter())
}

fn authorization(status: UNAuthorizationStatus) -> Permission {
    match status {
        UNAuthorizationStatus::NotDetermined => Permission::NotDetermined,
        UNAuthorizationStatus::Denied => Permission::Denied,
        UNAuthorizationStatus::Authorized
        | UNAuthorizationStatus::Provisional
        | UNAuthorizationStatus::Ephemeral => Permission::Granted,
        _ => Permission::Unavailable,
    }
}

fn read_permission(center: &UNUserNotificationCenter) -> Permission {
    let (sender, receiver) = mpsc::sync_channel(1);
    let completion = RcBlock::new(move |settings: NonNull<UNNotificationSettings>| {
        // SAFETY: the framework provides a non-null settings object valid for
        // the duration of this callback. Only its bounded enum leaves the block.
        let status = unsafe { settings.as_ref() }.authorizationStatus();
        let _ = sender.try_send(authorization(status));
    });
    center.getNotificationSettingsWithCompletionHandler(&completion);
    receiver
        .recv_timeout(DEADLINE)
        .unwrap_or(Permission::Unavailable)
}

pub fn permission(request: bool) -> Permission {
    autoreleasepool(|_| {
        let Ok(center) = center() else {
            return Permission::Unavailable;
        };
        let current = read_permission(&center);
        if !request || current != Permission::NotDetermined {
            return current;
        }
        let (sender, receiver) = mpsc::sync_channel(1);
        let completion = RcBlock::new(move |granted: Bool, error: *mut NSError| {
            let status = if !error.is_null() {
                Permission::Unavailable
            } else if granted.as_bool() {
                Permission::Granted
            } else {
                Permission::Denied
            };
            let _ = sender.try_send(status);
        });
        center.requestAuthorizationWithOptions_completionHandler(
            UNAuthorizationOptions::Alert | UNAuthorizationOptions::Sound,
            &completion,
        );
        receiver
            .recv_timeout(AUTHORIZATION_DEADLINE)
            .unwrap_or(Permission::Unavailable)
    })
}

pub fn user_locale() -> String {
    autoreleasepool(|_| {
        NSLocale::preferredLanguages()
            .firstObject()
            .filter(|locale| locale.to_string().to_ascii_lowercase().starts_with("zh"))
            .map_or_else(|| "en".into(), |_| "zh-CN".into())
    })
}

pub struct MacosSink {
    app: tauri::AppHandle,
}

impl MacosSink {
    pub fn new(app: tauri::AppHandle) -> Self {
        Self { app }
    }
}

impl Sink for MacosSink {
    fn available(&mut self) -> Result<(), Failure> {
        match permission(false) {
            Permission::Granted => Ok(()),
            Permission::NotDetermined => Err(Failure::PermissionRequired),
            Permission::Denied => Err(Failure::PermissionDenied),
            _ => Err(Failure::Unavailable),
        }
    }

    fn show(&mut self, presentation: &Presentation) -> Result<(), Failure> {
        autoreleasepool(|_| {
            let center = center()?;
            DELEGATE.with(|owner| {
                let mut owner = owner.borrow_mut();
                let delegate = owner.get_or_insert_with(ForegroundDelegate::new);
                center.setDelegate(Some(ProtocolObject::from_ref(&**delegate)));
            });
            let content = UNMutableNotificationContent::new();
            content.setTitle(&NSString::from_str(&presentation.title));
            content.setBody(&NSString::from_str(&presentation.body));
            content.setThreadIdentifier(&NSString::from_str(&presentation.group));
            content.setSound(Some(&UNNotificationSound::defaultSound()));
            let identifier =
                NSString::from_str(&format!("{}:{}", presentation.group, presentation.tag));
            let request = UNNotificationRequest::requestWithIdentifier_content_trigger(
                &identifier,
                &content,
                None,
            );
            let (sender, receiver) = mpsc::sync_channel(1);
            let completion = RcBlock::new(move |error: *mut NSError| {
                // Do not read/format NSError: it can contain paths or payload text.
                let _ = sender.try_send(if error.is_null() {
                    Ok(())
                } else {
                    Err(Failure::DeliveryFailed)
                });
            });
            center.addNotificationRequest_withCompletionHandler(&request, Some(&completion));
            receiver
                .recv_timeout(DEADLINE)
                .unwrap_or(Err(Failure::TimedOut))
        })
    }

    fn fallback(&mut self, _presentation: &Presentation) {
        request_attention(&self.app);
    }

    fn shutdown(&mut self) {
        autoreleasepool(|_| {
            if let Ok(center) = center() {
                center.setDelegate(None);
            }
            DELEGATE.with(|owner| {
                owner.borrow_mut().take();
            });
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn authorization_status_is_bounded_and_never_prompts() {
        assert_eq!(
            authorization(UNAuthorizationStatus::NotDetermined),
            Permission::NotDetermined
        );
        assert_eq!(
            authorization(UNAuthorizationStatus::Denied),
            Permission::Denied
        );
        assert_eq!(
            authorization(UNAuthorizationStatus::Authorized),
            Permission::Granted
        );
        assert_eq!(
            authorization(UNAuthorizationStatus(99)),
            Permission::Unavailable
        );
    }
}
