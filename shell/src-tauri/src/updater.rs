//! Keeping installed copies up to date.
//!
//! An installed copy checks the public releases repository in the background,
//! downloads a new version quietly, and installs it the next time the app
//! restarts: at the next launch, when someone quits from the tray, or straight
//! away from the settings window or the tray menu. It never interrupts someone
//! mid-sentence.
//!
//! Everything the app runs on ships at exact, tested versions - the engine's
//! Python libraries inside the installer, the GPU library set it downloads, the
//! model revisions it fetches - so nothing changes underneath a working copy.
//! An update replaces the whole set at once.
//!
//! Downloads are checked against the minisign public key in tauri.conf.json
//! before they are kept, and again before they are run.

use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

use base64::Engine as _;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_updater::UpdaterExt;

use crate::engine::{self, EngineProcess};

/// Sent to the windows, with the version, once an update is downloaded.
pub const READY_EVENT: &str = "update-ready";

const FIRST_CHECK_AFTER: Duration = Duration::from_secs(30);
const CHECK_EVERY: Duration = Duration::from_secs(6 * 60 * 60);
const RETRY_AFTER: Duration = Duration::from_secs(30 * 60);

/// A staged update that fails to install this many times is thrown away and
/// downloaded afresh, rather than retried on every launch forever.
const MAX_ATTEMPTS: u32 = 2;

const MANIFEST: &str = "staged.json";

/// An update downloaded, verified and waiting to be installed.
#[derive(Serialize, Deserialize)]
struct Staged {
    version: String,
    signature: String,
    file: String,
    #[serde(default)]
    attempts: u32,
}

/// Updates apply to installed copies only. A build run from a source checkout
/// is updated with git; installing a release over it would be wrong.
pub fn enabled() -> bool {
    engine::is_installed_copy() && std::env::var_os("PROMPT_MAXXER_NO_UPDATES").is_none()
}

fn folder() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA")
        .map(|base| PathBuf::from(base).join("Prompt Maxxer").join("updates"))
}

fn to_string(err: impl std::fmt::Display) -> String {
    err.to_string()
}

/// The staged update, if it is newer than this copy. Anything else in the
/// folder - an update that has since been installed, a partial download - is
/// deleted.
fn staged(app: &AppHandle) -> Option<Staged> {
    let dir = folder()?;
    if !dir.exists() {
        return None;
    }
    let usable = fs::read(dir.join(MANIFEST))
        .ok()
        .and_then(|raw| serde_json::from_slice::<Staged>(&raw).ok())
        .filter(|staged| {
            semver::Version::parse(&staged.version)
                .is_ok_and(|version| version > app.package_info().version)
                && dir.join(&staged.file).is_file()
        });
    if usable.is_none() {
        discard();
    }
    usable
}

fn discard() {
    if let Some(dir) = folder() {
        let _ = fs::remove_dir_all(dir);
    }
}

/// The version waiting to be installed, if there is one.
pub fn ready_version(app: &AppHandle) -> Option<String> {
    if !enabled() {
        return None;
    }
    staged(app).map(|staged| staged.version)
}

/// Check for updates in the background: shortly after launch, then every few
/// hours for an app that stays running all week.
pub fn watch(app: AppHandle) {
    if !enabled() {
        return;
    }
    std::thread::spawn(move || {
        std::thread::sleep(FIRST_CHECK_AFTER);
        loop {
            let wait = match tauri::async_runtime::block_on(check(&app)) {
                Ok(()) => CHECK_EVERY,
                Err(err) => {
                    eprintln!("prompt-maxxer: update check failed: {err}");
                    RETRY_AFTER
                }
            };
            std::thread::sleep(wait);
        }
    });
}

async fn check(app: &AppHandle) -> Result<(), String> {
    let updater = app.updater().map_err(to_string)?;
    let Some(update) = updater.check().await.map_err(to_string)? else {
        return Ok(());
    };
    if staged(app).is_some_and(|staged| staged.version == update.version) {
        return Ok(());
    }
    // Parsing also guarantees the version is safe to put in a file name.
    let version = semver::Version::parse(&update.version).map_err(to_string)?;

    // The plugin verifies the signature before handing the bytes back.
    let bytes = update.download(|_, _| {}, || {}).await.map_err(to_string)?;
    stage(&version, &update.signature, &bytes)?;
    eprintln!("prompt-maxxer: update {version} downloaded; it installs on the next restart");

    let _ = app.emit(READY_EVENT, version.to_string());
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || crate::refresh_tray_menu(&handle));
    Ok(())
}

fn stage(version: &semver::Version, signature: &str, bytes: &[u8]) -> Result<(), String> {
    if !bytes.starts_with(b"MZ") {
        return Err("the update is not a Windows installer".into());
    }
    let dir = folder().ok_or("LOCALAPPDATA is not set")?;
    // Whatever was staged before is older than this.
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).map_err(to_string)?;

    let file = format!("Prompt-Maxxer_{version}_x64-setup.exe");
    let part = dir.join(format!("{file}.part"));
    fs::write(&part, bytes).map_err(to_string)?;
    fs::rename(&part, dir.join(&file)).map_err(to_string)?;

    // Written last: a manifest only ever describes a complete download.
    let staged = Staged {
        version: version.to_string(),
        signature: signature.to_string(),
        file,
        attempts: 0,
    };
    fs::write(dir.join(MANIFEST), serde_json::to_vec_pretty(&staged).map_err(to_string)?)
        .map_err(to_string)
}

/// Install the staged update: stop the engine, start the installer and exit.
///
/// Returns only when there is nothing to install or it could not be started.
/// With `reopen`, the installer starts the app again once it is done.
pub fn install(app: &AppHandle, reopen: bool) -> Result<(), String> {
    if !enabled() {
        return Ok(());
    }
    let Some(mut staged) = staged(app) else {
        return Ok(());
    };
    let dir = folder().ok_or("LOCALAPPDATA is not set")?;
    if staged.attempts >= MAX_ATTEMPTS {
        discard();
        return Err(format!(
            "update {} did not install after {MAX_ATTEMPTS} attempts; it will be downloaded again",
            staged.version
        ));
    }

    let installer = dir.join(&staged.file);
    let bytes = fs::read(&installer).map_err(to_string)?;
    if let Err(err) = verify(app, &bytes, &staged.signature) {
        discard();
        return Err(format!("the downloaded update failed its signature check and was deleted: {err}"));
    }
    drop(bytes);

    staged.attempts += 1;
    if let Ok(raw) = serde_json::to_vec_pretty(&staged) {
        let _ = fs::write(dir.join(MANIFEST), raw);
    }

    // The engine runs from the install folder, so it must be gone before the
    // installer can replace its files.
    engine::shutdown(&app.state::<EngineProcess>());

    // The same arguments the updater plugin gives Tauri's NSIS installer:
    // a progress bar only, update mode (keeps shortcuts, the Start with
    // Windows entry and settings), and optionally reopen the app afterwards.
    let mut cmd = Command::new(&installer);
    cmd.args(["/P", "/UPDATE"]);
    if reopen {
        cmd.args(["/R", "/ARGS"]);
        cmd.args(std::env::args().skip(1).filter(|arg| arg == "--settings" || arg == "--models"));
    }
    cmd.spawn()
        .map_err(|err| format!("could not start the update installer: {err}"))?;

    eprintln!("prompt-maxxer: installing update {}", staged.version);
    if let Some(tray) = app.tray_by_id(crate::TRAY_ID) {
        let _ = tray.set_visible(false);
    }
    std::process::exit(0);
}

fn verify(app: &AppHandle, bytes: &[u8], signature: &str) -> Result<(), String> {
    let pubkey = app
        .config()
        .plugins
        .0
        .get("updater")
        .and_then(|updater| updater.get("pubkey"))
        .and_then(|key| key.as_str())
        .ok_or("the app has no update signing key configured")?;
    verify_with(pubkey, bytes, signature)
}

/// Both the key and the signature are base64 over minisign's text format,
/// exactly as `tauri signer` writes them.
fn verify_with(pubkey: &str, bytes: &[u8], signature: &str) -> Result<(), String> {
    let decode = |text: &str| -> Result<String, String> {
        let raw = base64::engine::general_purpose::STANDARD
            .decode(text.trim())
            .map_err(to_string)?;
        String::from_utf8(raw).map_err(to_string)
    };
    let key = minisign_verify::PublicKey::decode(&decode(pubkey)?).map_err(to_string)?;
    let signature = minisign_verify::Signature::decode(&decode(signature)?).map_err(to_string)?;
    key.verify(bytes, &signature, true).map_err(to_string)
}

#[cfg(test)]
mod tests {
    use super::verify_with;

    const CONFIG: &str = include_str!("../tauri.conf.json");
    // Signed with the release key by `tauri signer sign`.
    const SIGNED: &[u8] = include_bytes!("testdata/signed.txt");
    const SIGNATURE: &str = include_str!("testdata/signed.txt.sig");

    fn pubkey() -> String {
        let config: serde_json::Value = serde_json::from_str(CONFIG).unwrap();
        config["plugins"]["updater"]["pubkey"].as_str().unwrap().to_string()
    }

    #[test]
    fn accepts_a_file_signed_with_the_release_key() {
        verify_with(&pubkey(), SIGNED, SIGNATURE).unwrap();
    }

    #[test]
    fn rejects_a_modified_file() {
        let mut tampered = SIGNED.to_vec();
        tampered[0] ^= 1;
        assert!(verify_with(&pubkey(), &tampered, SIGNATURE).is_err());
    }

    #[test]
    fn rejects_a_malformed_signature() {
        assert!(verify_with(&pubkey(), SIGNED, "not a signature").is_err());
    }
}
