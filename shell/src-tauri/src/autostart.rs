//! Start with Windows: a value under the per-user Run key.
//!
//! Written straight to the registry rather than through a plugin, because the
//! switch in settings has to tell the truth about two things the Run key alone
//! does not show. Windows can turn an entry off from Settings > Apps > Startup
//! (or Task Manager) without removing it, and the entry may launch a different
//! copy of the app than the one that is running.

use serde::Serialize;
use std::io;
use winreg::{
    enums::{HKEY_CURRENT_USER, KEY_SET_VALUE},
    RegKey,
};

const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
const APPROVED_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run";
const ENTRY: &str = "Prompt Maxxer";

/// Added to the command Windows runs at sign-in, so that launch can be told
/// apart from one the user started.
pub const LAUNCH_ARG: &str = "--autostart";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Status {
    /// Windows will launch this copy of the app when the user signs in.
    pub enabled: bool,
    /// An entry exists, but it was switched off in Windows' Startup apps.
    pub disabled_by_windows: bool,
    /// An entry exists, but it launches a different copy of the app.
    pub other_copy: bool,
}

pub fn status() -> Status {
    status_of(ENTRY, &this_command())
}

pub fn set(enabled: bool) -> Result<Status, String> {
    let command = this_command();
    set_for(ENTRY, &command, enabled)
        .map_err(|err| format!("Could not update Windows startup: {err}"))?;
    Ok(status_of(ENTRY, &command))
}

fn this_command() -> String {
    let exe = std::env::current_exe().unwrap_or_default();
    format!("\"{}\" {}", exe.display(), LAUNCH_ARG)
}

fn status_of(entry: &str, command: &str) -> Status {
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let registered: Option<String> = hkcu
        .open_subkey(RUN_KEY)
        .ok()
        .and_then(|key| key.get_value(entry).ok());

    let Some(registered) = registered else {
        return Status { enabled: false, disabled_by_windows: false, other_copy: false };
    };

    // Windows paths are case-insensitive, so a difference in case is still
    // this copy of the app.
    let ours = registered.eq_ignore_ascii_case(command);
    let disabled_by_windows = switched_off_by_windows(&hkcu, entry);
    Status {
        enabled: ours && !disabled_by_windows,
        disabled_by_windows,
        other_copy: !ours,
    }
}

fn switched_off_by_windows(hkcu: &RegKey, entry: &str) -> bool {
    // Settings > Apps > Startup and Task Manager record their switch here, as
    // a binary value whose first byte is even when on and odd when off.
    hkcu.open_subkey(APPROVED_KEY)
        .ok()
        .and_then(|key| key.get_raw_value(entry).ok())
        .and_then(|value| value.bytes.first().copied())
        .is_some_and(|flag| flag & 1 == 1)
}

fn set_for(entry: &str, command: &str, enabled: bool) -> io::Result<()> {
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let (run, _) = hkcu.create_subkey(RUN_KEY)?;
    if enabled {
        run.set_value(entry, &command.to_string())?;
    } else {
        ignore_missing(run.delete_value(entry))?;
    }

    // Clear Windows' own on/off record as well. Turning the option on here has
    // to take effect even if the entry was once switched off in Startup apps,
    // and a missing record is what Windows treats as on.
    if let Ok(approved) = hkcu.open_subkey_with_flags(APPROVED_KEY, KEY_SET_VALUE) {
        ignore_missing(approved.delete_value(entry))?;
    }
    Ok(())
}

fn ignore_missing(result: io::Result<()>) -> io::Result<()> {
    match result {
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use winreg::{enums::REG_BINARY, RegValue};

    // An entry name of its own, so tests never touch the real one. One test
    // function, because the steps share that entry and tests run in parallel.
    const TEST_ENTRY: &str = "Prompt Maxxer (test)";
    const COMMAND: &str = r#""C:\Apps\Prompt Maxxer\prompt-maxxer.exe" --autostart"#;
    const OFF: Status = Status { enabled: false, disabled_by_windows: false, other_copy: false };

    #[test]
    fn switches_on_and_off_and_reports_what_windows_will_do() {
        let _ = set_for(TEST_ENTRY, COMMAND, false);
        assert_eq!(status_of(TEST_ENTRY, COMMAND), OFF);

        set_for(TEST_ENTRY, COMMAND, true).unwrap();
        assert!(status_of(TEST_ENTRY, COMMAND).enabled, "enabled after turning on");
        assert!(
            status_of(TEST_ENTRY, &COMMAND.to_uppercase()).enabled,
            "path case does not matter on Windows"
        );

        let elsewhere = status_of(TEST_ENTRY, r#""D:\Other\prompt-maxxer.exe" --autostart"#);
        assert!(!elsewhere.enabled && elsewhere.other_copy, "detects another copy");

        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        let (approved, _) = hkcu.create_subkey(APPROVED_KEY).unwrap();
        approved
            .set_raw_value(
                TEST_ENTRY,
                &RegValue { vtype: REG_BINARY, bytes: vec![3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0].into() },
            )
            .unwrap();
        let blocked = status_of(TEST_ENTRY, COMMAND);
        assert!(!blocked.enabled && blocked.disabled_by_windows, "detects Windows switching it off");

        set_for(TEST_ENTRY, COMMAND, true).unwrap();
        assert!(status_of(TEST_ENTRY, COMMAND).enabled, "turning it on clears Windows' switch");

        set_for(TEST_ENTRY, COMMAND, false).unwrap();
        assert_eq!(status_of(TEST_ENTRY, COMMAND), OFF, "off again, with nothing left behind");
        set_for(TEST_ENTRY, COMMAND, false).unwrap(); // turning off twice is fine
    }
}
