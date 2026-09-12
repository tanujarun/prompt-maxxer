//! Starting and supervising the Python engine.
//!
//! The shell is the thing a user launches, so it is the shell's job to make
//! sure the engine is running. Telling someone to open a terminal and run a
//! package-manager script is a developer instruction, not a product.
//!
//! The engine is still a separate process — dictation must survive the UI
//! crashing — but it is a child of the shell, started on launch and stopped
//! when the user quits from the tray.

use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// Keeps the engine from flashing a console window on start.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const ENGINE_ADDR: &str = "127.0.0.1:8765";

/// Name of the frozen engine the installer ships beside the app.
const BUNDLED_ENGINE: &str = "engine/prompt-maxxer-engine.exe";

/// Handle to an engine we started ourselves. An engine that was already
/// running when we launched is left alone, and stays running when we quit.
#[derive(Default)]
pub struct EngineProcess {
    child: Mutex<Option<Child>>,
    #[cfg(windows)]
    job: Mutex<Option<job::Job>>,
}

/// How to start the engine.
struct Launch {
    program: PathBuf,
    args: &'static [&'static str],
    dir: PathBuf,
}

/// A reachable port is the only honest test that the engine is up: it may have
/// been started by hand, by a previous shell, or by the launcher script.
pub fn is_running() -> bool {
    let Ok(addr) = ENGINE_ADDR.parse::<SocketAddr>() else {
        return false;
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok()
}

fn canonical(path: PathBuf) -> PathBuf {
    path.canonicalize().unwrap_or(path)
}

/// Find the engine: a source checkout's virtualenv first, then the frozen
/// engine an installed copy carries.
fn locate() -> Option<Launch> {
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(Path::to_path_buf));

    // Source checkouts first. Running from the repository should use the live
    // source in the virtualenv, not a frozen copy that may be out of date.
    let mut roots: Vec<PathBuf> = Vec::new();
    if let Ok(custom) = std::env::var("PROMPT_MAXXER_ENGINE_DIR") {
        roots.push(PathBuf::from(custom));
    }
    if let Some(dir) = &exe_dir {
        // shell/src-tauri/target/release/prompt-maxxer.exe
        roots.push(dir.join("../../../../engine"));
        roots.push(dir.join("../../../engine"));
    }
    for root in roots {
        let python = root.join(".venv/Scripts/python.exe");
        if python.is_file() {
            return Some(Launch {
                program: canonical(python),
                args: &["-m", "prompt_maxxer"],
                dir: canonical(root),
            });
        }
    }

    // Installed copies: the standalone engine shipped inside the installer,
    // which needs no Python on the machine.
    let bundled = exe_dir?.join(BUNDLED_ENGINE);
    if bundled.is_file() {
        let dir = bundled.parent().map(Path::to_path_buf).unwrap_or_default();
        return Some(Launch { program: bundled, args: &[], dir });
    }
    None
}

/// Whether this is an installed copy - the app beside the engine its installer
/// shipped - rather than a build run from a source checkout.
pub fn is_installed_copy() -> bool {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join(BUNDLED_ENGINE)))
        .is_some_and(|engine| engine.is_file())
}

fn describe_missing() -> String {
    concat!(
        "Could not find the Prompt Maxxer engine. Reinstalling the app restores it. ",
        "In a source checkout, create engine/.venv as described in the README, ",
        "or set PROMPT_MAXXER_ENGINE_DIR."
    )
    .to_string()
}

/// Start the engine unless something is already listening.
pub fn ensure_running(state: &EngineProcess) -> Result<bool, String> {
    if is_running() {
        return Ok(false);
    }

    // Do not stack children if a previous spawn is still warming up: loading
    // the model takes several seconds, during which the port is not yet open.
    if let Ok(mut guard) = state.child.lock() {
        if let Some(child) = guard.as_mut() {
            match child.try_wait() {
                Ok(None) => return Ok(false), // still starting
                _ => *guard = None,           // exited; fall through and retry
            }
        }
    }

    let launch = locate().ok_or_else(describe_missing)?;
    let child = spawn(&launch)?;

    #[cfg(windows)]
    if let Some(handle) = job::adopt(&child) {
        if let Ok(mut guard) = state.job.lock() {
            *guard = Some(handle);
        }
    }

    if let Ok(mut guard) = state.child.lock() {
        *guard = Some(child);
    }
    Ok(true)
}

/// Ties the engine's lifetime to the shell's.
///
/// Killing the child by handle is not enough on Windows: the venv launcher
/// re-execs the real interpreter, so the process that actually holds the
/// keyboard hook is a *grandchild*. It currently dies along with its parent,
/// but only incidentally. A job object with KILL_ON_JOB_CLOSE makes it
/// guaranteed, and covers the case that matters most - the shell being force
/// killed or crashing, which would otherwise strand an engine that swallows
/// the push-to-talk key system-wide with no interface left to stop it.
#[cfg(windows)]
mod job {
    use std::process::Child;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
        JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    pub struct Job(HANDLE);

    // The handle is owned solely by this struct and only closed on drop.
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}

    impl Drop for Job {
        fn drop(&mut self) {
            if !self.0.is_null() {
                // Closing the last handle terminates everything in the job.
                unsafe { CloseHandle(self.0) };
            }
        }
    }

    /// Put `child` in a job that kills its whole tree when this process exits.
    pub fn adopt(child: &Child) -> Option<Job> {
        use std::os::windows::io::AsRawHandle;

        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return None;
            }

            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

            let ok = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const std::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if ok == 0 {
                CloseHandle(handle);
                return None;
            }

            if AssignProcessToJobObject(handle, child.as_raw_handle() as HANDLE) == 0 {
                CloseHandle(handle);
                return None;
            }

            Some(Job(handle))
        }
    }
}

fn spawn(launch: &Launch) -> Result<Child, String> {
    let mut cmd = Command::new(&launch.program);
    cmd.args(launch.args).current_dir(&launch.dir);
    // The warning is noise the user cannot act on, and it is the first thing
    // written to stderr on every start.
    cmd.env("HF_HUB_DISABLE_SYMLINKS_WARNING", "1");

    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    cmd.spawn()
        .map_err(|err| format!("Could not start the engine: {err}"))
}

/// Stop an engine we started. One we merely found is left running.
pub fn shutdown(state: &EngineProcess) {
    if let Ok(mut guard) = state.child.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
    // Dropping the job terminates anything still left in it, including the
    // real interpreter behind the venv launcher.
    #[cfg(windows)]
    if let Ok(mut guard) = state.job.lock() {
        guard.take();
    }
}
