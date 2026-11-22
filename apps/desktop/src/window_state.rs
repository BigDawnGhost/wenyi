//! Window geometry scoped to the selected Desktop data root.

use std::{
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        Condvar, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt};
use cap_std::fs::{Dir, OpenOptions};
use serde::{Deserialize, Serialize};
use tauri::WebviewWindow;

const FILE: &str = "window-state.json";
const TARGET_WIDTH: f64 = 1280.0;
const TARGET_HEIGHT: f64 = 720.0;
const MIN_WIDTH: f64 = 1024.0;
const MIN_HEIGHT: f64 = 600.0;
// Reserve room for native borders and a title bar before the window exists.
const FRAME_WIDTH: f64 = 16.0;
const FRAME_HEIGHT: f64 = 48.0;
const SAVE_DEBOUNCE: Duration = Duration::from_millis(350);

#[derive(Clone, Copy, Debug)]
struct Rect {
    x: f64,
    y: f64,
    width: f64,
    height: f64,
}

#[derive(Clone, Debug)]
pub(super) struct Screen {
    name: Option<String>,
    position: [i32; 2],
    work_area: Rect,
    scale_factor: f64,
    primary: bool,
}

impl Screen {
    pub fn from_monitor(monitor: &tauri::Monitor, primary: bool) -> Self {
        let work_area = monitor.work_area();
        let position = monitor.position();
        Self {
            name: monitor.name().cloned(),
            position: [position.x, position.y],
            work_area: Rect {
                x: f64::from(work_area.position.x),
                y: f64::from(work_area.position.y),
                width: f64::from(work_area.size.width),
                height: f64::from(work_area.size.height),
            },
            scale_factor: monitor.scale_factor(),
            primary,
        }
    }

    fn valid(&self) -> bool {
        self.work_area.width > 0.0
            && self.work_area.height > 0.0
            && self.scale_factor.is_finite()
            && (0.25..=8.0).contains(&self.scale_factor)
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct Document {
    version: u8,
    monitor: Option<String>,
    /// Physical monitor origin used to distinguish displays with the same name.
    #[serde(default)]
    monitor_position: Option<[i32; 2]>,
    /// Logical offset from the monitor work area's top-left corner.
    x: f64,
    y: f64,
    /// Logical client size, excluding native window decorations.
    width: f64,
    height: f64,
    /// Scale factor at capture time; geometry remains stored in logical pixels.
    scale_factor: f64,
}

impl Document {
    fn valid(&self) -> bool {
        self.version == 1
            && self.monitor.as_ref().is_none_or(|name| name.len() <= 256)
            && self.x.is_finite()
            && self.y.is_finite()
            && (-16_384.0..=16_384.0).contains(&self.x)
            && (-16_384.0..=16_384.0).contains(&self.y)
            && self.width.is_finite()
            && self.height.is_finite()
            && (320.0..=16_384.0).contains(&self.width)
            && (200.0..=16_384.0).contains(&self.height)
            && self.scale_factor.is_finite()
            && (0.25..=8.0).contains(&self.scale_factor)
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) struct Placement {
    pub(super) x: f64,
    pub(super) y: f64,
    pub(super) width: f64,
    pub(super) height: f64,
    pub(super) min_width: f64,
    pub(super) min_height: f64,
}

#[derive(Clone)]
pub(super) struct Store {
    root: PathBuf,
    writing: std::sync::Arc<Mutex<()>>,
    pending: std::sync::Arc<(Mutex<Option<Pending>>, Condvar)>,
    generation: std::sync::Arc<AtomicU64>,
}

struct Pending {
    window: WebviewWindow,
    due: Instant,
    generation: u64,
}

impl Store {
    pub fn new(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
            writing: Default::default(),
            pending: Default::default(),
            generation: Default::default(),
        }
    }

    fn load_document(&self) -> io::Result<Option<Document>> {
        let _guard = self.writing.lock().unwrap();
        match read_document(&self.root) {
            Ok(document) => Ok(Some(document)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error),
        }
    }

    pub fn initial_placement(&self, screens: &[Screen]) -> Option<Placement> {
        let saved = self.load_document().ok().flatten();
        placement(saved.as_ref(), screens)
    }

    pub fn start_worker(&self) {
        let store = self.clone();
        thread::spawn(move || store.run_worker());
    }

    pub fn schedule(&self, window: &WebviewWindow) {
        let generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        let (pending, changed) = &*self.pending;
        *pending.lock().unwrap() = Some(Pending {
            window: window.clone(),
            due: Instant::now() + SAVE_DEBOUNCE,
            generation,
        });
        changed.notify_one();
    }

    pub fn save_now(&self, window: &WebviewWindow) {
        self.generation.fetch_add(1, Ordering::SeqCst);
        let (pending, changed) = &*self.pending;
        let _ = pending.lock().unwrap().take();
        changed.notify_one();
        let _ = self.save_normal_window(window);
    }

    fn run_worker(&self) {
        let (pending, changed) = &*self.pending;
        let mut queued = pending.lock().unwrap();
        loop {
            while queued.is_none() {
                queued = changed.wait(queued).unwrap();
            }
            let due = queued.as_ref().unwrap().due;
            let wait = due.saturating_duration_since(Instant::now());
            if !wait.is_zero() {
                let (next, _) = changed.wait_timeout(queued, wait).unwrap();
                queued = next;
                continue;
            }
            let item = queued.take().unwrap();
            drop(queued);
            if self.generation.load(Ordering::SeqCst) == item.generation {
                let _ = self.save_normal_window(&item.window);
            }
            queued = pending.lock().unwrap();
        }
    }

    fn save_normal_window(&self, window: &WebviewWindow) -> io::Result<()> {
        if window.is_minimized().unwrap_or(true) || window.is_maximized().unwrap_or(true) {
            return Ok(());
        }
        let document = capture(window)?;
        self.write_document(&document)
    }

    fn write_document(&self, document: &Document) -> io::Result<()> {
        if !document.valid() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Invalid window state",
            ));
        }
        let _guard = self.writing.lock().unwrap();
        let root = Dir::open_ambient_dir(&self.root, cap_std::ambient_authority())?;
        root.create_dir_all("runtime")?;
        let directory = root.open_dir_nofollow("runtime")?;
        let mut random = [0; 16];
        getrandom::fill(&mut random)
            .map_err(|_| io::Error::other("Secure randomness unavailable."))?;
        let suffix: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let temporary = format!(".window-state-{suffix}.tmp");
        let mut options = OpenOptions::new();
        options
            .write(true)
            .create_new(true)
            .follow(FollowSymlinks::No);
        let mut file = directory.open_with(&temporary, &options)?;
        let result = (|| {
            file.write_all(&serde_json::to_vec(document)?)?;
            file.sync_all()?;
            drop(file);
            directory.rename(&temporary, &directory, FILE)
        })();
        if let Err(error) = result {
            let _ = directory.remove_file(&temporary);
            return Err(error);
        }
        Ok(())
    }

    #[cfg(test)]
    fn write_for_test(&self, document: &Document) -> io::Result<()> {
        self.write_document(document)
    }
}

fn read_document(root: &Path) -> io::Result<Document> {
    let root = Dir::open_ambient_dir(root, cap_std::ambient_authority())?;
    let directory = root.open_dir_nofollow("runtime")?;
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    let file = directory.open_with(FILE, &options)?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::other("Invalid window state file"));
    }
    let mut bytes = Vec::new();
    file.take(1025).read_to_end(&mut bytes)?;
    if bytes.len() > 1024 {
        return Err(io::Error::other("Invalid window state size"));
    }
    let document: Document = serde_json::from_slice(&bytes)?;
    if !document.valid() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Invalid window state",
        ));
    }
    Ok(document)
}

fn capture(window: &WebviewWindow) -> io::Result<Document> {
    let monitor = window
        .current_monitor()
        .map_err(|error| io::Error::other(error.to_string()))?
        .ok_or_else(|| io::Error::other("Window monitor is unavailable"))?;
    let position = window
        .outer_position()
        .map_err(|error| io::Error::other(error.to_string()))?;
    let size = window
        .inner_size()
        .map_err(|error| io::Error::other(error.to_string()))?;
    let scale_factor = monitor.scale_factor();
    let area = monitor.work_area();
    let monitor_position = monitor.position();
    let document = Document {
        version: 1,
        monitor: monitor.name().cloned(),
        monitor_position: Some([monitor_position.x, monitor_position.y]),
        x: (f64::from(position.x) - f64::from(area.position.x)) / scale_factor,
        y: (f64::from(position.y) - f64::from(area.position.y)) / scale_factor,
        width: f64::from(size.width) / scale_factor,
        height: f64::from(size.height) / scale_factor,
        scale_factor,
    };
    if !document.valid() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Window geometry is outside the supported range",
        ));
    }
    Ok(document)
}

fn placement(saved: Option<&Document>, screens: &[Screen]) -> Option<Placement> {
    let saved_screen = saved.and_then(|document| {
        let name = document.monitor.as_deref()?;
        let mut matching = screens
            .iter()
            .filter(|screen| screen.name.as_deref() == Some(name));
        let first = matching.next()?;
        if let Some(position) = document.monitor_position {
            if let Some(exact) = screens.iter().find(|screen| {
                screen.name.as_deref() == Some(name) && screen.position == position
            }) {
                return Some(exact);
            }
        }
        matching.next().is_none().then_some(first)
    });
    let screen = saved_screen
        .or_else(|| screens.iter().find(|screen| screen.primary))
        .or_else(|| screens.first())?;
    if !screen.valid() {
        return None;
    }

    let available_width = (screen.work_area.width / screen.scale_factor - FRAME_WIDTH).max(1.0);
    let available_height = (screen.work_area.height / screen.scale_factor - FRAME_HEIGHT).max(1.0);
    let min_width = MIN_WIDTH.min(available_width);
    let min_height = MIN_HEIGHT.min(available_height);
    let width = saved
        .map(|document| document.width)
        .unwrap_or(TARGET_WIDTH)
        .clamp(min_width, available_width);
    let height = saved
        .map(|document| document.height)
        .unwrap_or(TARGET_HEIGHT)
        .clamp(min_height, available_height);
    let max_x = (screen.work_area.width / screen.scale_factor - width - FRAME_WIDTH).max(0.0);
    let max_y = (screen.work_area.height / screen.scale_factor - height - FRAME_HEIGHT).max(0.0);
    let (offset_x, offset_y) = if saved_screen.is_some() {
        let saved = saved.unwrap();
        (saved.x.clamp(0.0, max_x), saved.y.clamp(0.0, max_y))
    } else {
        (max_x / 2.0, max_y / 2.0)
    };

    Some(Placement {
        x: (screen.work_area.x / screen.scale_factor) + offset_x,
        y: (screen.work_area.y / screen.scale_factor) + offset_y,
        width,
        height,
        min_width,
        min_height,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn screen(name: &str, x: i32, y: i32, width: u32, height: u32, scale_factor: f64) -> Screen {
        Screen {
            name: Some(name.into()),
            position: [x, y],
            work_area: Rect {
                x: f64::from(x),
                y: f64::from(y),
                width: f64::from(width),
                height: f64::from(height),
            },
            scale_factor,
            primary: name == "primary",
        }
    }

    fn saved(monitor: &str, x: f64, y: f64, width: f64, height: f64) -> Document {
        Document {
            version: 1,
            monitor: Some(monitor.into()),
            monitor_position: None,
            x,
            y,
            width,
            height,
            scale_factor: 1.0,
        }
    }

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            let mut random = [0; 16];
            getrandom::fill(&mut random).unwrap();
            let suffix: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
            let path = std::env::temp_dir().join(format!("wenyi-window-state-{suffix}"));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn first_launch_uses_target_size_centered_inside_primary_work_area() {
        let screen = screen("primary", 0, 0, 1920, 1080, 1.0);
        let placement = placement(None, &[screen]).unwrap();
        assert_eq!(placement.width, 1280.0);
        assert_eq!(placement.height, 720.0);
        assert_eq!(placement.x, 312.0);
        assert_eq!(placement.y, 156.0);
    }

    #[test]
    fn first_launch_shrinks_to_fit_small_high_dpi_work_area_and_keeps_minimum_adaptive() {
        let screen = screen("primary", 0, 0, 1366, 768, 1.25);
        let placement = placement(None, &[screen]).unwrap();
        assert!(placement.width <= 1366.0 / 1.25 - FRAME_WIDTH);
        assert!(placement.height <= 768.0 / 1.25 - FRAME_HEIGHT);
        assert_eq!(placement.min_width, MIN_WIDTH.min(1366.0 / 1.25 - FRAME_WIDTH));
        assert!(placement.min_height < MIN_HEIGHT);
        assert!(placement.x >= 0.0 && placement.y >= 0.0);
        assert!(placement.x + placement.width + FRAME_WIDTH <= 1366.0 / 1.25);
        assert!(placement.y + placement.height + FRAME_HEIGHT <= 768.0 / 1.25);
    }

    #[test]
    fn restores_last_normal_size_and_relative_position() {
        let screen = screen("primary", 0, 0, 1920, 1080, 1.0);
        let placement = placement(Some(&saved("primary", 80.0, 60.0, 1100.0, 680.0)), &[screen])
            .unwrap();
        assert_eq!(placement.width, 1100.0);
        assert_eq!(placement.height, 680.0);
        assert_eq!(placement.x, 80.0);
        assert_eq!(placement.y, 60.0);
    }

    #[test]
    fn restores_legacy_state_without_a_monitor_position_on_a_unique_display() {
        let document: Document = serde_json::from_str(
            r#"{"version":1,"monitor":"primary","x":80,"y":60,"width":1100,"height":680,"scale_factor":1}"#,
        )
        .unwrap();
        assert_eq!(document.monitor_position, None);

        let screen = screen("primary", 0, 0, 1920, 1080, 1.0);
        let placement = placement(Some(&document), &[screen]).unwrap();

        assert_eq!(placement.x, 80.0);
        assert_eq!(placement.y, 60.0);
    }

    #[test]
    fn uses_saved_monitor_position_to_disambiguate_duplicate_monitor_names() {
        let secondary = screen("same model", 0, 0, 1920, 1080, 1.0);
        let mut primary = screen("same model", 1920, 0, 1920, 1080, 1.0);
        primary.primary = true;
        let screens = [secondary, primary];
        let mut document = saved("same model", 80.0, 60.0, 1100.0, 680.0);
        document.monitor_position = Some([1920, 0]);

        let placement = placement(Some(&document), &screens).unwrap();

        assert_eq!(placement.x, 2000.0);
        assert_eq!(placement.y, 60.0);
    }

    #[test]
    fn centers_on_primary_when_duplicate_monitor_names_cannot_be_disambiguated() {
        let secondary = screen("same model", 0, 0, 1920, 1080, 1.0);
        let mut primary = screen("same model", 1920, 0, 1920, 1080, 1.0);
        primary.primary = true;
        let screens = [secondary, primary];
        let mut document = saved("same model", 80.0, 60.0, 1100.0, 680.0);
        document.monitor_position = Some([3840, 0]);

        let placement = placement(Some(&document), &screens).unwrap();

        assert_eq!(placement.x, 2322.0);
        assert_eq!(placement.y, 176.0);
    }

    #[test]
    fn clamps_offscreen_saved_position_to_the_work_area() {
        let screen = screen("primary", 0, 0, 1280, 800, 1.0);
        let placement = placement(Some(&saved("primary", 800.0, 700.0, 1200.0, 700.0)), &[screen])
            .unwrap();
        assert_eq!(placement.width, 1200.0);
        assert_eq!(placement.height, 700.0);
        assert_eq!(placement.x, 64.0);
        assert_eq!(placement.y, 52.0);
    }

    #[test]
    fn falls_back_to_primary_center_when_the_saved_display_is_disconnected() {
        let screen = screen("primary", 200, 100, 1600, 900, 1.0);
        let disconnected = saved("disconnected", 200.0, 200.0, 1100.0, 680.0);
        let placement = placement(Some(&disconnected), &[screen]).unwrap();
        assert_eq!(placement.width, 1100.0);
        assert_eq!(placement.height, 680.0);
        assert_eq!(placement.x, 442.0);
        assert_eq!(placement.y, 186.0);
    }

    #[test]
    fn preserves_logical_geometry_when_monitor_scale_changes() {
        let screen = screen("primary", 1920, 0, 1920, 1200, 1.5);
        let placement = placement(Some(&saved("primary", 80.0, 40.0, 1100.0, 680.0)), &[screen])
            .unwrap();
        assert_eq!(placement.width, 1100.0);
        assert_eq!(placement.height, 680.0);
        assert_eq!(placement.x, 1360.0);
        assert_eq!(placement.y, 40.0);
    }

    #[test]
    fn damaged_state_falls_back_to_default_and_unwritable_state_is_ignored_by_startup() {
        let fixture = Fixture::new();
        fs::create_dir(fixture.0.join("runtime")).unwrap();
        fs::write(fixture.0.join("runtime").join(FILE), b"not json").unwrap();
        let store = Store::new(&fixture.0);
        assert!(store.load_document().is_err());
        let primary = screen("primary", 0, 0, 1920, 1080, 1.0);
        assert_eq!(
            store
                .initial_placement(std::slice::from_ref(&primary))
                .unwrap()
                .width,
            TARGET_WIDTH
        );

        fs::remove_file(fixture.0.join("runtime").join(FILE)).unwrap();
        fs::create_dir(fixture.0.join("runtime").join(FILE)).unwrap();
        assert_eq!(
            store
                .initial_placement(std::slice::from_ref(&primary))
                .unwrap()
                .width,
            TARGET_WIDTH
        );
        let document = saved("primary", 20.0, 30.0, 1100.0, 680.0);
        assert!(store.write_for_test(&document).is_err());
    }

    #[test]
    fn state_round_trips_atomically_and_is_scoped_to_its_data_root() {
        let first = Fixture::new();
        let second = Fixture::new();
        let store = Store::new(&first.0);
        let document = saved("primary", 20.0, 30.0, 1100.0, 680.0);
        store.write_for_test(&document).unwrap();
        assert_eq!(store.load_document().unwrap(), Some(document));
        assert_eq!(Store::new(&second.0).load_document().unwrap(), None);
        assert_eq!(fs::read_dir(first.0.join("runtime")).unwrap().count(), 1);
    }
}
