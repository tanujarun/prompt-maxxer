//! Prompt Maxxer shell.
//!
//! The shell is deliberately thin. The Python engine owns the dictation hot
//! path - hotkey, capture, recognition, injection - so everything here is
//! presentation: a tray icon, a settings window, and a transparent overlay
//! pill that mirrors engine state over a localhost WebSocket.
//!
//! If this process dies, dictation keeps working.

mod autostart;
mod engine;
mod updater;

use engine::EngineProcess;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, Monitor, PhysicalPosition, RunEvent, State, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder, WindowEvent, Wry,
};

/// Where the engine publishes its event stream.
const ENGINE_WS: &str = "ws://127.0.0.1:8765";

pub(crate) const TRAY_ID: &str = "prompt-maxxer-tray";

/// Sections of the settings window that can be opened directly.
#[derive(Clone, Copy)]
enum View {
    Session,
    Models,
}

impl View {
    fn as_str(self) -> &'static str {
        match self {
            View::Session => "session",
            View::Models => "models",
        }
    }

    /// `--settings` opens the settings window, `--models` opens it on models.
    fn from_args<I, S>(args: I) -> Option<View>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut view = None;
        for arg in args {
            match arg.as_ref() {
                "--settings" => view = view.or(Some(View::Session)),
                "--models" => view = Some(View::Models),
                _ => {}
            }
        }
        view
    }
}

#[tauri::command]
fn engine_endpoint() -> String {
    ENGINE_WS.to_string()
}

/// Whether anything is listening on the engine port.
#[tauri::command]
fn engine_running() -> bool {
    engine::is_running()
}

/// Start the engine on demand, for the button the settings window shows when
/// it cannot reach one.
#[tauri::command]
fn start_engine(state: State<'_, EngineProcess>) -> Result<bool, String> {
    engine::ensure_running(&state)
}

/// Whether Windows will launch this copy of the app at sign-in.
#[tauri::command]
fn autostart_status() -> autostart::Status {
    autostart::status()
}

/// Turn launching at sign-in on or off for this copy of the app.
#[tauri::command]
fn set_autostart(enabled: bool) -> Result<autostart::Status, String> {
    autostart::set(enabled)
}

/// The version of an update that is downloaded and waiting for a restart.
#[tauri::command]
fn update_status(app: AppHandle) -> Option<String> {
    updater::ready_version(&app)
}

/// Install the waiting update now. Closes the app, which the installer reopens.
#[tauri::command]
fn install_update(app: AppHandle) -> Result<(), String> {
    updater::install(&app, true)?;
    Err("No update is waiting to be installed.".into())
}

/// Keep the tray tooltip in step with the push-to-talk key. The overlay calls
/// this whenever the engine announces its binding, including after a rebind.
#[tauri::command]
fn set_hotkey_label(app: tauri::AppHandle, label: String) -> Result<(), String> {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        tray.set_tooltip(Some(format!("Prompt Maxxer — hold {label} to dictate")))
            .map_err(|err| err.to_string())?;
    }
    Ok(())
}

/// Put the pill where the person is working and on top of everything. The
/// overlay calls this each time the pill appears.
#[tauri::command]
fn show_overlay(app: AppHandle) -> Result<(), String> {
    present_overlay(&app).map_err(|err| err.to_string())
}

/// Whether the engine has never run for this user: it writes its config file
/// on first start.
fn is_first_run() -> bool {
    std::env::var_os("APPDATA")
        .map(|appdata| std::path::Path::new(&appdata).join("Prompt Maxxer").join("config.json"))
        .is_some_and(|config| !config.is_file())
}

/// Size of the overlay window in logical pixels, as set in tauri.conf.json.
const PILL_WIDTH: f64 = 700.0;
const PILL_HEIGHT: f64 = 100.0;
/// Space left under the pill, in logical pixels, so it clears the taskbar.
const PILL_BOTTOM_GAP: f64 = 110.0;

/// Top-left corner, in physical pixels, that puts the pill at bottom-centre of
/// a monitor. Only the primary monitor starts at (0, 0), so the monitor's own
/// origin is part of the answer.
fn pill_position(origin: (i32, i32), size: (u32, u32), scale: f64) -> (i32, i32) {
    let width = (PILL_WIDTH * scale).round() as i32;
    let height = (PILL_HEIGHT * scale).round() as i32;
    let gap = (PILL_BOTTOM_GAP * scale).round() as i32;
    (
        origin.0 + (size.0 as i32 - width) / 2,
        origin.1 + size.1 as i32 - height - gap,
    )
}

/// Centre of the window that has keyboard focus, in physical pixels.
#[cfg(windows)]
fn focused_window_centre() -> Option<(f64, f64)> {
    use windows_sys::Win32::Foundation::RECT;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetShellWindow, GetWindowRect, IsIconic,
    };

    unsafe {
        let hwnd = GetForegroundWindow();
        // Nothing focused, or the desktop itself, which spans every monitor
        // and so says nothing about which one is in use.
        if hwnd.is_null() || hwnd == GetShellWindow() || IsIconic(hwnd) != 0 {
            return None;
        }
        let mut rect: RECT = std::mem::zeroed();
        if GetWindowRect(hwnd, &mut rect) == 0 {
            return None;
        }
        Some((
            (rect.left as f64 + rect.right as f64) / 2.0,
            (rect.top as f64 + rect.bottom as f64) / 2.0,
        ))
    }
}

#[cfg(not(windows))]
fn focused_window_centre() -> Option<(f64, f64)> {
    None
}

/// The monitor the person is working on: the one holding the window with
/// keyboard focus, because that is where the words are about to be typed.
/// Failing that, the monitor under the mouse pointer, then the primary one.
fn active_monitor(overlay: &WebviewWindow) -> Option<Monitor> {
    if let Some((x, y)) = focused_window_centre() {
        if let Ok(Some(monitor)) = overlay.monitor_from_point(x, y) {
            return Some(monitor);
        }
    }
    if let Ok(cursor) = overlay.cursor_position() {
        if let Ok(Some(monitor)) = overlay.monitor_from_point(cursor.x, cursor.y) {
            return Some(monitor);
        }
    }
    overlay.primary_monitor().ok().flatten()
}

/// Put the overlay back above every other window, without taking focus.
///
/// Asking for always-on-top once, at startup, is not enough. The window is
/// never hidden - an idle pill is only transparent - so nothing ever re-raises
/// it, and Windows has been seen to drop it out of the always-on-top band
/// altogether while leaving its always-on-top flag set: after a sign-in the pill
/// sat underneath ordinary application windows and dictation appeared to have
/// no pill at all. Tauri's own `set_always_on_top(true)` does nothing in that
/// state, because as far as it knows the window is already on top.
fn raise_to_top(overlay: &WebviewWindow) -> tauri::Result<()> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::WindowsAndMessaging::{
            SetWindowPos, HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE,
        };

        let hwnd = overlay.hwnd()?;
        // SWP_NOACTIVATE: raising must never take focus from the window the
        // words are being typed into.
        let raised = unsafe {
            SetWindowPos(
                hwnd.0,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE,
            )
        };
        if raised == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }

    #[cfg(not(windows))]
    overlay.set_always_on_top(true)?;

    Ok(())
}

/// Move the pill to bottom-centre of the monitor in use, and above every other
/// window. Cheap enough to do every time the pill appears, which is the only
/// moment either property matters.
fn present_overlay(app: &AppHandle) -> tauri::Result<()> {
    let Some(overlay) = app.get_webview_window("overlay") else {
        return Ok(());
    };

    if let Some(monitor) = active_monitor(&overlay) {
        let origin = monitor.position();
        let size = monitor.size();
        let (x, y) = pill_position(
            (origin.x, origin.y),
            (size.width, size.height),
            monitor.scale_factor(),
        );
        let already_there = overlay
            .outer_position()
            .is_ok_and(|at| at.x == x && at.y == y);
        if !already_there {
            overlay.set_position(PhysicalPosition::new(x, y))?;
        }
    }

    raise_to_top(&overlay)
}

/// Set the overlay up at launch: clicks fall through to whatever is underneath,
/// and it starts on the monitor in use.
fn place_overlay(app: &tauri::AppHandle) -> tauri::Result<()> {
    let Some(overlay) = app.get_webview_window("overlay") else {
        return Ok(());
    };

    // The pill must never intercept input; the user is typing into the window
    // behind it.
    overlay.set_ignore_cursor_events(true)?;

    present_overlay(app)
}

/// Show the settings window on a given section, creating it on first use.
///
/// Every window costs its own WebView2 renderer. Creating this one eagerly and
/// hiding it measured at roughly 150 MB of resident memory for a window most
/// sessions never open, which is a poor trade for a tray app that runs all day.
fn show_settings(app: &tauri::AppHandle, view: View) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.eval(format!("window.__pmShowView?.({:?})", view.as_str()));
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
        return;
    }

    match WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Prompt Maxxer")
        .inner_size(720.0, 800.0)
        .min_inner_size(560.0, 640.0)
        .center()
        // Runs before the page's own scripts, so the window opens on the
        // requested section instead of flashing the default one first.
        .initialization_script(format!("window.__PM_INITIAL_VIEW = {:?};", view.as_str()))
        .build()
    {
        Ok(win) => {
            let _ = win.set_focus();
        }
        Err(err) => eprintln!("could not open settings window: {err}"),
    }
}

fn tray_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let settings = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
    let models = MenuItem::with_id(app, "models", "Models…", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Prompt Maxxer", true, None::<&str>)?;
    match updater::ready_version(app) {
        Some(version) => {
            let update = MenuItem::with_id(
                app,
                "update",
                format!("Restart to update to {version}"),
                true,
                None::<&str>,
            )?;
            Menu::with_items(app, &[&settings, &models, &separator, &update, &quit])
        }
        None => Menu::with_items(app, &[&settings, &models, &separator, &quit]),
    }
}

/// Rebuild the tray menu, to offer an update that has just finished downloading.
pub(crate) fn refresh_tray_menu(app: &AppHandle) {
    if let (Some(tray), Ok(menu)) = (app.tray_by_id(TRAY_ID), tray_menu(app)) {
        let _ = tray.set_menu(Some(menu));
    }
}

fn build_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let menu = tray_menu(app)?;

    let icon = app
        .default_window_icon()
        .cloned()
        .expect("bundle always provides a default window icon");

    TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon)
        .tooltip("Prompt Maxxer — hold Right Ctrl to dictate")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            // Left click opens settings; the menu stays on right click. A tray
            // icon that only responds to right click is easy to miss entirely.
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_settings(tray.app_handle(), View::Session);
            }
        })
        .on_menu_event(|app, event| match event.id.as_ref() {
            "settings" => show_settings(app, View::Session),
            "models" => show_settings(app, View::Models),
            "update" => {
                if let Err(err) = updater::install(app, true) {
                    eprintln!("prompt-maxxer: {err}");
                }
            }
            "quit" => {
                // Quitting is a restart of sorts: a waiting update installs
                // now, without reopening the app. Does not return if it does.
                if let Err(err) = updater::install(app, false) {
                    eprintln!("prompt-maxxer: {err}");
                }
                engine::shutdown(&app.state::<EngineProcess>());
                app.exit(0);
            }
            _ => {}
        })
        .build(app)?;

    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // Registered first on purpose: a second launch is intercepted here and
        // exits before it can create a tray icon, start another engine or open
        // windows of its own. The copy already running shows its settings.
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            show_settings(app, View::from_args(&args).unwrap_or(View::Session));
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(EngineProcess::default())
        .invoke_handler(tauri::generate_handler![
            engine_endpoint,
            engine_running,
            start_engine,
            set_hotkey_label,
            show_overlay,
            autostart_status,
            set_autostart,
            update_status,
            install_update
        ])
        .setup(|app| {
            // An update downloaded during an earlier session installs now,
            // before the engine starts and locks its files. The installer
            // reopens the app, so this does not return if there is one.
            if let Err(err) = updater::install(app.handle(), true) {
                eprintln!("prompt-maxxer: {err}");
            }

            build_tray(app.handle())?;
            place_overlay(app.handle())?;

            // Checked before the engine starts, because the engine creates the
            // config file as soon as it runs.
            let first_run = is_first_run();

            // Launching the shell should be enough to make dictation work.
            if let Err(err) = engine::ensure_running(&app.state::<EngineProcess>()) {
                eprintln!("prompt-maxxer: {err}");
            }

            if let Some(view) = View::from_args(std::env::args()) {
                show_settings(app.handle(), view);
            } else if first_run {
                // First launch on this machine: put the model download and the
                // push-to-talk key in front of the person, rather than having
                // it all happen silently in the tray.
                show_settings(app.handle(), View::Session);
            }

            updater::watch(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the settings window destroys it, freeing its renderer;
            // the tray rebuilds it on demand. Either way the close button must
            // not end the session - Prompt Maxxer is a tray app.
            if let WindowEvent::CloseRequested { .. } = event {
                if window.label() == "overlay" {
                    // The overlay is click-through and has no close affordance,
                    // but guard against anything else closing it.
                    return;
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Prompt Maxxer")
        .run(|app, event| {
            // Covers the paths that do not go through the tray Quit item.
            if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
                engine::shutdown(&app.state::<EngineProcess>());
            }
        });
}

#[cfg(test)]
mod tests {
    use super::pill_position;

    #[test]
    fn sits_bottom_centre_of_the_primary_monitor() {
        // 2560x1440 at 100%: a 700x100 pill, 110 px clear of the bottom edge.
        assert_eq!(pill_position((0, 0), (2560, 1440), 1.0), (930, 1230));
    }

    #[test]
    fn follows_a_monitor_that_does_not_start_at_the_origin() {
        // A second monitor to the left of, and lower than, the primary.
        assert_eq!(pill_position((-2560, 337), (2560, 1440), 1.0), (-1630, 1567));
    }

    #[test]
    fn scales_with_the_monitor() {
        // 3840x2160 at 150%: the pill is 1050x150 with a 165 px gap.
        assert_eq!(pill_position((2560, 0), (3840, 2160), 1.5), (3955, 1845));
    }
}
