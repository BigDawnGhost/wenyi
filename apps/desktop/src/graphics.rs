//! Process-local Linux WebKit compatibility, applied before GTK starts any threads.

#[cfg(any(target_os = "linux", test))]
fn defaults(
    wayland_available: bool,
    display_available: bool,
    gdk_backend: Option<&str>,
    renderer_is_configured: bool,
    nvidia: bool,
    hyprland: bool,
) -> Vec<(&'static str, &'static str)> {
    let mut values = Vec::new();
    let gdk_backend = gdk_backend.filter(|value| !value.trim().is_empty());
    // Native Wayland is preferred; X11 is only a connection-time fallback when available.
    if gdk_backend.is_none() {
        match (wayland_available, display_available) {
            (true, true) => values.push(("GDK_BACKEND", "wayland,x11")),
            (true, false) => values.push(("GDK_BACKEND", "wayland")),
            (false, true) => values.push(("GDK_BACKEND", "x11")),
            (false, false) => {}
        }
    }
    let backend = gdk_backend
        .or(if wayland_available {
            Some("wayland")
        } else if display_available {
            Some("x11")
        } else {
            None
        })
        .and_then(|value| {
            value
                .split(',')
                .map(str::trim)
                .find(|value| matches!(*value, "wayland" | "x11"))
        });
    if nvidia && !renderer_is_configured {
        match backend {
            // Keep DMA-BUF rather than forcing slow shared-memory readback on Wayland.
            // NVIDIA egl-wayland supports this process-local explicit-sync workaround.
            Some("wayland") if !hyprland => values.push(("__NV_DISABLE_EXPLICIT_SYNC", "1")),
            Some("wayland" | "x11") => values.push(("WEBKIT_DISABLE_DMABUF_RENDERER", "1")),
            _ => {}
        }
    }
    values
}

pub fn configure() {
    #[cfg(target_os = "linux")]
    {
        let configured = |name| std::env::var_os(name).is_some_and(|value| !value.is_empty());
        let gdk_backend = std::env::var("GDK_BACKEND").ok();
        let renderer_is_configured = [
            "WEBKIT_DISABLE_DMABUF_RENDERER",
            "__NV_DISABLE_EXPLICIT_SYNC",
            "WEBKIT_DISABLE_COMPOSITING_MODE",
            "WEBKIT_DMABUF_RENDERER_FORCE_SHM",
            "LIBGL_ALWAYS_SOFTWARE",
        ]
        .into_iter()
        .any(configured);
        let hyprland = std::env::var("XDG_CURRENT_DESKTOP")
            .unwrap_or_default()
            .split(':')
            .any(|desktop| desktop.eq_ignore_ascii_case("hyprland"));
        for (name, value) in defaults(
            configured("WAYLAND_DISPLAY"),
            configured("DISPLAY"),
            gdk_backend.as_deref(),
            renderer_is_configured,
            std::path::Path::new("/sys/module/nvidia").exists(),
            hyprland,
        ) {
            // main calls this before constructing Tauri or starting the sidecar.
            std::env::set_var(name, value);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_wayland_is_preferred_when_xwayland_is_also_available() {
        assert_eq!(
            defaults(true, true, None, false, true, false),
            vec![
                ("GDK_BACKEND", "wayland,x11"),
                ("__NV_DISABLE_EXPLICIT_SYNC", "1")
            ]
        );
    }

    #[test]
    fn wayland_only_sessions_do_not_require_xwayland() {
        assert_eq!(
            defaults(true, false, None, false, false, false),
            vec![("GDK_BACKEND", "wayland")]
        );
    }

    #[test]
    fn x11_and_headless_sessions_keep_their_available_backend() {
        assert_eq!(
            defaults(false, true, None, true, false, false),
            vec![("GDK_BACKEND", "x11")]
        );
        assert!(defaults(false, false, None, false, true, false).is_empty());
    }

    #[test]
    fn explicit_graphics_settings_remain_authoritative() {
        assert!(defaults(true, true, Some("wayland"), true, true, false).is_empty());
        assert_eq!(
            defaults(true, true, Some("x11,wayland"), false, true, false),
            vec![("WEBKIT_DISABLE_DMABUF_RENDERER", "1")]
        );
        assert_eq!(
            defaults(true, true, None, true, true, false),
            vec![("GDK_BACKEND", "wayland,x11")]
        );
    }

    #[test]
    fn other_drivers_keep_the_normal_accelerated_renderer() {
        assert!(defaults(true, true, Some("wayland"), false, false, false).is_empty());
        assert!(defaults(false, true, Some("x11"), false, false, false).is_empty());
        assert!(defaults(true, true, Some("broadway"), false, true, false).is_empty());
    }

    #[test]
    fn nvidia_hyprland_keeps_its_separate_gbm_compatibility_path() {
        assert_eq!(
            defaults(true, true, Some("wayland"), false, true, true),
            vec![("WEBKIT_DISABLE_DMABUF_RENDERER", "1")]
        );
    }
}
