import { invoke } from "@tauri-apps/api/core";
import { EngineClient, type EngineEvent } from "./engine";
import { demoFromQuery, themeFromQuery } from "./dev";
import { initThemePref } from "./theme-pref";

themeFromQuery();
// Follows the theme chosen in settings, live, via the shared storage event.
initThemePref();
const DEMO = demoFromQuery();

const SEGMENTS = 16;
// Speech sits roughly between -34 and -14 dBFS. This window puts normal talking
// mid-meter, with headroom left for loud syllables.
const FLOOR_DB = -48;
const CEILING_DB = -6;
const RELEASE_PER_FRAME = 0.035;
const PEAK_HOLD_MS = 700;
const PEAK_FALL_MS = 60;

const $ = (id: string) => document.getElementById(id) as HTMLElement;

const body = document.body;
const mode = $("mode");
const counter = $("counter");
const readout = $("readout");
const vu = $("vu");
const text = $("text");
const line = $("line");
const stableEl = $("stable");
const tentativeEl = $("tentative");
const message = $("message");

const segments = Array.from({ length: SEGMENTS }, () => {
  const seg = document.createElement("span");
  seg.className = "seg";
  vu.appendChild(seg);
  return seg;
});

// -- VU meter ------------------------------------------------------------

let target = 0;
let level = 0;
let peak = 0;
let peakMovedAt = 0;
let latestDb = -Infinity;
let readoutAt = 0;

function unitFromRms(rms: number): number {
  latestDb = 20 * Math.log10(Math.max(rms, 1e-6));
  return Math.min(1, Math.max(0, (latestDb - FLOOR_DB) / (CEILING_DB - FLOOR_DB)));
}

function formatDb(db: number): string {
  return db < -60 ? "-∞ DB" : `${Math.round(db)} DB`;
}

function renderMeter(now: number): void {
  // Instant attack, steady release: the meter snaps to a syllable and falls
  // back like a needle instead of flickering between frames.
  level = target >= level ? target : Math.max(target, level - RELEASE_PER_FRAME);
  const lit = Math.round(level * SEGMENTS);

  if (lit >= peak) {
    peak = lit;
    peakMovedAt = now;
  } else if (now - peakMovedAt > PEAK_HOLD_MS) {
    peak -= 1;
    peakMovedAt = now - PEAK_HOLD_MS + PEAK_FALL_MS;
  }

  for (let i = 0; i < SEGMENTS; i += 1) {
    const cls = i < lit ? "seg on" : i === peak - 1 ? "seg peak" : "seg";
    if (segments[i].className !== cls) segments[i].className = cls;
  }
}

// -- tape counter ----------------------------------------------------------

let recordingSince = 0;
const two = (n: number) => String(n).padStart(2, "0");

function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `${two(Math.floor(seconds / 60))}:${two(seconds % 60)}`;
}

function renderCounter(now: number): void {
  counter.textContent = formatElapsed(now - recordingSince);
}

// -- draw loop -------------------------------------------------------------

let looping = false;

function ensureLoop(): void {
  if (looping) return;
  looping = true;
  requestAnimationFrame(frame);
}

function frame(now: number): void {
  const recording = body.classList.contains("state-recording");
  if (!recording) target = 0;
  renderMeter(now);

  if (recording) {
    renderCounter(now);
    // Throttled so the digits are readable rather than a blur.
    if (now - readoutAt > 250) {
      readoutAt = now;
      readout.textContent = formatDb(latestDb);
    }
  }

  // Stop once hidden and at rest. The overlay window never closes, so an
  // unconditional loop would redraw sixty times a second all day for nothing.
  if (body.classList.contains("state-idle") && level === 0 && peak === 0) {
    looping = false;
    return;
  }
  requestAnimationFrame(frame);
}

// -- text ----------------------------------------------------------------

function showWords(stable: string, tentative: string): void {
  message.hidden = true;
  line.hidden = false;
  stableEl.textContent = stable;
  tentativeEl.textContent = tentative;
  // Pin the view to the newest words, like an LCD scrolling left. Hard clip,
  // no fade: a real display cuts characters, it does not feather them.
  requestAnimationFrame(() => {
    text.scrollLeft = text.scrollWidth;
  });
}

function showMessage(msg: string): void {
  line.hidden = true;
  message.hidden = false;
  message.textContent = msg;
  text.scrollLeft = 0;
}

// Re-pin whenever the line's rendered width changes, not only when its text
// does. The dot-matrix font can finish loading after the first words arrive;
// that widened the line after it had been pinned and left the newest words
// clipped off the right edge.
new ResizeObserver(() => {
  if (!line.hidden) text.scrollLeft = text.scrollWidth;
}).observe(line);

// -- state -----------------------------------------------------------------

type Look = "idle" | "loading" | "recording" | "transcribing" | "done" | "notice" | "error";

let hideTimer: number | undefined;
/** True while a result or notice is on screen. The engine sends a plain
 *  "idle" straight after a transcript; without this the result vanished the
 *  instant it arrived. */
let holding = false;

function setLook(look: Look, modeText: string): void {
  window.clearTimeout(hideTimer);
  holding = false;
  body.className = `state-${look}`;
  mode.textContent = modeText;
  ensureLoop();
}

function holdThenHide(ms: number): void {
  holding = true;
  if (DEMO) return;
  hideTimer = window.setTimeout(() => {
    holding = false;
    body.className = "state-idle";
  }, ms);
}

function notice(msg: string): void {
  setLook("notice", "STBY");
  readout.textContent = "";
  showMessage(msg);
  holdThenHide(1400);
}

let hotkeyLabel = "RIGHT CTRL";

function handle(event: EngineEvent): void {
  switch (event.event) {
    case "ready":
      hotkeyLabel = event.hotkey.toUpperCase();
      if (!holding) setLook("idle", "STBY");
      // The overlay lives as long as the app does, so it is the window that
      // keeps the tray tooltip naming the current key.
      if ("__TAURI_INTERNALS__" in window) {
        invoke("set_hotkey_label", { label: event.hotkey }).catch(() => {});
      }
      break;

    case "state":
      if (event.state === "recording") {
        setLook("recording", "REC");
        recordingSince = performance.now();
        counter.textContent = "00:00";
        readout.textContent = formatDb(-Infinity);
        showWords("", "");
      } else if (event.state === "transcribing") {
        // Keep the words on screen; blanking them mid-sentence reads as a glitch.
        setLook("transcribing", "PROC");
        readout.textContent = "";
        // Freeze on the engine's own measurement of the take rather than on
        // whatever the last drawn frame happened to show.
        if (event.audio_s !== undefined) counter.textContent = formatElapsed(event.audio_s * 1000);
        if (!stableEl.textContent && !tentativeEl.textContent) {
          showMessage("[TRANSCRIBING]");
        }
      } else if (event.state === "loading") {
        setLook("loading", "STBY");
        counter.textContent = "";
        readout.textContent = "";
        showMessage("[LOADING MODEL]");
      } else if (event.reason === "too_short") {
        notice(`[HOLD ${hotkeyLabel} TO RECORD]`);
      } else if (event.reason === "silence" || event.reason === "empty") {
        notice("[NO SPEECH DETECTED]");
      } else if (event.reason !== "error" && !holding) {
        setLook("idle", "STBY");
      }
      break;

    case "level":
      if (body.classList.contains("state-recording")) target = unitFromRms(event.rms);
      break;

    case "gpu_runtime":
      // First launch on an NVIDIA machine: the GPU libraries come first.
      if (body.classList.contains("state-loading")) {
        if (event.status === "downloading") {
          const pct = event.total_bytes
            ? Math.floor(((event.done_bytes ?? 0) / event.total_bytes) * 100)
            : null;
          showMessage(pct === null ? "[DOWNLOADING GPU LIBRARIES]" : `[DOWNLOADING GPU LIBRARIES ${pct}%]`);
        } else if (event.status === "error") {
          showMessage("[GPU LIBRARY DOWNLOAD FAILED - RETRYING]");
        }
      }
      break;

    case "model_download":
      // First launch: the model downloads before the engine can listen. Show
      // progress rather than an unexplained loading pill.
      if (body.classList.contains("state-loading")) {
        if (event.status === "downloading") {
          const pct = event.total_bytes
            ? Math.floor(((event.done_bytes ?? 0) / event.total_bytes) * 100)
            : null;
          showMessage(pct === null ? "[DOWNLOADING MODEL]" : `[DOWNLOADING MODEL ${pct}%]`);
        } else if (event.status === "error") {
          showMessage("[DOWNLOAD FAILED - RETRYING]");
        }
      }
      break;

    case "partial":
      if (body.classList.contains("state-recording")) {
        showWords(event.stable, event.tentative);
      }
      break;

    case "transcript":
      setLook("done", "DONE");
      readout.textContent = `${event.total_ms} MS`;
      showWords(event.text, "");
      holdThenHide(1600);
      break;

    case "error":
      setLook("error", "ERROR");
      readout.textContent = "";
      showMessage(`[ERROR: ${event.message.toUpperCase().slice(0, 44)}]`);
      holdThenHide(2800);
      break;
  }
}

// -- QA demo states ----------------------------------------------------------

function runDemo(kind: string): void {
  // Mid grey stands in for whatever application the overlay floats above.
  document.documentElement.style.background = "#808080";
  handle({
    event: "ready",
    hotkey: "Right Ctrl",
    engine: "faster-whisper",
    model: "large-v3-turbo",
    device: "cuda",
    streaming: true,
  });

  if (kind === "loading") return handle({ event: "state", state: "loading" });
  if (kind === "nospeech") return handle({ event: "state", state: "idle", reason: "silence" });
  if (kind === "error") return handle({ event: "error", message: "Microphone unavailable" });

  handle({ event: "state", state: "recording" });
  recordingSince = performance.now() - 7400;
  let t = 0;
  window.setInterval(() => {
    t += 1;
    if (body.classList.contains("state-recording")) {
      handle({ event: "level", rms: 0.05 + 0.04 * Math.abs(Math.sin(t * 0.3)) });
    }
  }, 33);
  if (kind === "rec-empty") return;

  handle({
    event: "partial",
    stable: "can you move the design review to thursday and send the",
    tentative: "updated agenda",
    audio_s: 7.4,
    latency_ms: 112,
  });
  if (kind === "rec") return;
  if (kind === "proc") return handle({ event: "state", state: "transcribing", audio_s: 7.4 });

  handle({
    event: "transcript",
    text: "Can you move the design review to Thursday and send the updated agenda?",
    language: "en",
    audio_s: 7.4,
    asr_ms: 233,
    total_ms: 241,
    strategy: "keystrokes",
  });
}

if (DEMO) {
  runDemo(DEMO);
} else {
  const client = new EngineClient("ws://127.0.0.1:8765");
  client.on(handle);
  client.connect();
}
