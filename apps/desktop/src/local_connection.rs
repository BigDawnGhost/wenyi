//! Native-owned authenticated transport to the private loopback engine.
use std::time::Duration;

use reqwest::blocking::Client;
use serde_json::Value;

#[derive(Clone)]
pub(crate) struct Connection {
    pub(crate) client: Client,
    pub(crate) base: String,
    pub(crate) token: String,
}

impl Connection {
    pub(crate) fn new(raw: &str) -> Result<Self, String> {
        let value: Value = serde_json::from_str(raw).map_err(|_| "Local engine unavailable.")?;
        let base = value["apiBase"]
            .as_str()
            .ok_or("Local engine unavailable.")?;
        let url = reqwest::Url::parse(base).map_err(|_| "Invalid local engine address.")?;
        if url.scheme() != "http"
            || url.host_str() != Some("127.0.0.1")
            || url.port().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.path() != "/"
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err("Invalid local engine address.".into());
        }
        let client = Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| "Cannot start local engine transport.")?;
        Ok(Self {
            client,
            base: base.trim_end_matches('/').into(),
            token: value["token"]
                .as_str()
                .ok_or("Local engine unavailable.")?
                .into(),
        })
    }
}
