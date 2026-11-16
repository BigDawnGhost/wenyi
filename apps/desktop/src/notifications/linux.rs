//! Freedesktop notifications over the user's session bus, never a shell command.
//! Connections and replacement IDs belong to the notification dispatcher.

use std::{collections::HashMap, time::Duration};

use dbus::{
    arg::{RefArg, Variant},
    blocking::Connection,
};

use super::{request_attention, Failure, Presentation, Sink};

const SERVICE: &str = "org.freedesktop.Notifications";
const PATH: &str = "/org/freedesktop/Notifications";
const DEADLINE: Duration = Duration::from_secs(1);
// Tauri installs icons under the main binary's name, not the bundle identifier.
const ICON_NAME: &str = env!("CARGO_BIN_NAME");

#[derive(Default)]
struct Freedesktop {
    desktop_entry: String,
    connection: Option<Connection>,
    replacements: HashMap<String, u32>,
    markup: bool,
}

impl Freedesktop {
    fn available(&mut self) -> Result<(), Failure> {
        if self.connection.is_none() {
            self.connection = Some(Connection::new_session().map_err(|_| Failure::Unavailable)?);
        }
        let result: Result<(Vec<String>,), _> = self
            .connection
            .as_ref()
            .unwrap()
            .with_proxy(SERVICE, PATH, DEADLINE)
            .method_call(SERVICE, "GetCapabilities", ());
        match result {
            Ok((capabilities,)) => {
                self.markup = capabilities.iter().any(|value| value == "body-markup");
                Ok(())
            }
            Err(error) => {
                self.connection = None;
                self.replacements.clear();
                Err(classify(&error, Failure::Unavailable))
            }
        }
    }

    fn show(&mut self, presentation: &Presentation) -> Result<(), Failure> {
        let connection = self.connection.as_ref().ok_or(Failure::Unavailable)?;
        let key = format!("{}:{}", presentation.group, presentation.tag);
        let mut hints: HashMap<&str, Variant<Box<dyn RefArg>>> = HashMap::new();
        hints.insert(
            "desktop-entry",
            Variant(Box::new(self.desktop_entry.clone())),
        );
        hints.insert("sound-name", Variant(Box::new("complete".to_string())));
        hints.insert("urgency", Variant(Box::new(1u8)));
        let body = body(&presentation.body, self.markup);
        let result: Result<(u32,), dbus::Error> =
            connection.with_proxy(SERVICE, PATH, DEADLINE).method_call(
                SERVICE,
                "Notify",
                (
                    "Wenyi Desktop",
                    self.replacements.get(&key).copied().unwrap_or(0),
                    ICON_NAME,
                    presentation.title.as_str(),
                    body,
                    Vec::<String>::new(),
                    hints,
                    -1i32,
                ),
            );
        match result {
            Ok((id,)) => {
                if self.replacements.len() >= 512 {
                    self.replacements.clear();
                }
                self.replacements.insert(key, id);
                Ok(())
            }
            Err(error) => {
                self.connection = None;
                self.replacements.clear();
                Err(classify(&error, Failure::DeliveryFailed))
            }
        }
    }
}

fn classify(error: &dbus::Error, otherwise: Failure) -> Failure {
    match error.name() {
        Some("org.freedesktop.DBus.Error.AccessDenied") => Failure::PermissionDenied,
        Some("org.freedesktop.DBus.Error.NoReply") | Some("org.freedesktop.DBus.Error.Timeout") => {
            Failure::TimedOut
        }
        Some("org.freedesktop.DBus.Error.ServiceUnknown")
        | Some("org.freedesktop.DBus.Error.NameHasNoOwner") => Failure::Unavailable,
        _ => otherwise,
    }
}

fn body(value: &str, markup: bool) -> String {
    if markup {
        value
            .replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;")
    } else {
        value.to_string()
    }
}

pub struct LinuxSink {
    app: tauri::AppHandle,
    transport: Freedesktop,
}

impl LinuxSink {
    pub fn new(app: tauri::AppHandle) -> Self {
        // Tauri's .desktop filename is the product name, without any slugging.
        let transport = Freedesktop {
            desktop_entry: app
                .config()
                .product_name
                .clone()
                .unwrap_or_else(|| app.package_info().name.clone()),
            ..Freedesktop::default()
        };
        Self { app, transport }
    }
}

impl Sink for LinuxSink {
    fn available(&mut self) -> Result<(), Failure> {
        self.transport.available()
    }

    fn show(&mut self, presentation: &Presentation) -> Result<(), Failure> {
        self.transport.show(presentation)
    }

    fn fallback(&mut self, _presentation: &Presentation) {
        request_attention(&self.app);
    }

    fn shutdown(&mut self) {
        self.transport.connection = None;
        self.transport.replacements.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markup_is_escaped_only_when_the_server_supports_it() {
        assert_eq!(body("A < B & C", true), "A &lt; B &amp; C");
        assert_eq!(body("A < B & C", false), "A < B & C");
    }

    #[test]
    fn dbus_errors_are_classified_without_their_messages() {
        for (name, expected) in [
            ("AccessDenied", Failure::PermissionDenied),
            ("NoReply", Failure::TimedOut),
            ("Timeout", Failure::TimedOut),
            ("ServiceUnknown", Failure::Unavailable),
            ("NameHasNoOwner", Failure::Unavailable),
            ("Failed", Failure::DeliveryFailed),
        ] {
            let error = dbus::Error::new_custom(
                &format!("org.freedesktop.DBus.Error.{name}"),
                "private native exception",
            );
            assert_eq!(classify(&error, Failure::DeliveryFailed), expected);
        }
    }

    #[test]
    #[ignore = "requires an isolated dbus-run-session; never use the desktop session bus"]
    fn private_bus_delivery_replaces_by_workspace_and_run() {
        use dbus::{channel::MatchingReceiver, message::MatchRule};
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc, Mutex,
        };

        assert_eq!(
            std::env::var("WENYI_PRIVATE_NOTIFICATION_BUS").as_deref(),
            Ok("1")
        );
        let server = Connection::new_session().unwrap();
        server.request_name(SERVICE, false, true, false).unwrap();
        let observed = Arc::new(Mutex::new(Vec::new()));
        let received = observed.clone();
        server.start_receive(
            MatchRule::new_method_call(),
            Box::new(move |message, connection| {
                let reply = match message.member().as_ref().map(|member| member.as_ref()) {
                    Some("GetCapabilities") => message.method_return().append1(vec!["body-markup"]),
                    Some("Notify") => {
                        let (_, replacement, icon, title, text, actions, hints, _): (
                            String,
                            u32,
                            String,
                            String,
                            String,
                            Vec<String>,
                            HashMap<String, Variant<Box<dyn RefArg>>>,
                            i32,
                        ) = message.read_all().unwrap();
                        let entry = hints["desktop-entry"].0.as_str().unwrap().to_string();
                        received.lock().unwrap().push((
                            replacement,
                            title,
                            text,
                            actions,
                            icon,
                            entry,
                        ));
                        message.method_return().append1(42u32)
                    }
                    _ => return true,
                };
                connection.channel().send(reply).unwrap();
                true
            }),
        );
        let stopped = Arc::new(AtomicBool::new(false));
        let done = stopped.clone();
        let worker = std::thread::spawn(move || {
            while !done.load(Ordering::SeqCst) {
                server.process(Duration::from_millis(10)).unwrap();
            }
        });
        let mut transport = Freedesktop {
            desktop_entry: "Fixture Desktop".into(),
            ..Freedesktop::default()
        };
        transport.available().unwrap();
        let mut value = Presentation {
            title: "Book <one>".into(),
            body: "A < B & C".into(),
            tag: "run:one".into(),
            group: "workspace-one".into(),
        };
        transport.show(&value).unwrap();
        transport.show(&value).unwrap();
        value.group = "workspace-two".into();
        transport.show(&value).unwrap();
        stopped.store(true, Ordering::SeqCst);
        worker.join().unwrap();
        let values = observed.lock().unwrap();
        assert_eq!(
            values.iter().map(|value| value.0).collect::<Vec<_>>(),
            vec![0, 42, 0]
        );
        assert!(values.iter().all(|value| value.1 == "Book <one>"
            && value.2 == "A &lt; B &amp; C"
            && value.3.is_empty()
            && value.4 == ICON_NAME
            && value.5 == "Fixture Desktop"));
    }
}
