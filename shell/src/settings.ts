import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
  EngineClient,
  type EngineCommand,
  type EngineEvent,
  type ModelFit,
  type ModelInfo,
  type ModelsEvent,
  type Take,
} from "./engine";
import { demoFromQuery, themeFromQuery } from "./dev";
import { formatSize, ModelsPanel } from "./models";
import { Scope } from "./scope";
import { initThemePref, readThemePref, setThemePref, type ThemePref } from "./theme-pref";

themeFromQuery();
const DEMO = demoFromQuery();

/** The latency the app is designed around: key release to text on screen. */
const LATENCY_BUDGET_MS = 600;
const LATENCY_SEGMENTS = 20;
const OFFLINE_AFTER_MS = 12000;

const $ = <T extends HTMLElement = HTMLElement>(id: string) =>
  document.getElementById(id) as T;
const pad3 = (n: number) => String(n).padStart(3, "0");

const client = new EngineClient("ws://127.0.0.1:8765");
const send = (command: EngineCommand): boolean => client.send(command);

// -- sections ------------------------------------------------------------------

type View = "session" | "models";

const viewSession = $("view-session");
const viewModels = $("view-models");
const tabs = Array.from(document.querySelectorAll<HTMLButtonElement>(".tab"));

function showView(view: View): void {
  document.body.dataset.view = view;
  viewSession.hidden = view !== "session";
  viewModels.hidden = view !== "models";
  for (const tab of tabs) {
    if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  if (view === "models") send({ cmd: "list_models" });
}

for (const tab of tabs) {
  tab.addEventListener("click", () => showView(tab.dataset.view === "models" ? "models" : "session"));
}

declare global {
  interface Window {
    __pmShowView?: (view: string) => void;
    __PM_INITIAL_VIEW?: string;
  }
}

// The shell opens this window on a particular section - the tray's Models item,
// or a second launch with --models - through these two hooks.
window.__pmShowView = (view) => showView(view === "models" ? "models" : "session");
showView(window.__PM_INITIAL_VIEW === "models" ? "models" : "session");

// -- theme ---------------------------------------------------------------------

const themeButtons = Array.from(document.querySelectorAll<HTMLButtonElement>("[data-theme-pref]"));

function renderThemeControl(pref: ThemePref): void {
  for (const button of themeButtons) {
    button.setAttribute("aria-checked", String(button.dataset.themePref === pref));
  }
}

for (const button of themeButtons) {
  button.addEventListener("click", () => {
    const pref = button.dataset.themePref as ThemePref;
    setThemePref(pref);
    renderThemeControl(pref);
  });
}

initThemePref(renderThemeControl);
renderThemeControl(readThemePref());

// -- hero ------------------------------------------------------------------------

type Hero = "offline" | "starting" | "ready" | "rec" | "working" | "error";

const stateEl = $("state");
const stateSub = $("state-sub");
const startButton = $<HTMLButtonElement>("start-engine");
let hotkeyLabel = "Right Ctrl";

const HERO_COPY: Record<Hero, () => [string, string]> = {
  offline: () => ["OFFLINE", "Dictation is unavailable until the engine starts."],
  starting: () => ["LOADING", "Loading the speech model. This takes a few seconds."],
  ready: () => ["READY", `Hold ${hotkeyLabel}, speak, release.`],
  rec: () => ["REC", "Listening. Release to transcribe."],
  working: () => ["WORKING", "Transcribing on your GPU."],
  error: () => ["ERROR", "Something went wrong."],
};

function setHero(hero: Hero, sub?: string): void {
  const [word, fallback] = HERO_COPY[hero]();
  document.body.dataset.hero = hero;
  stateEl.textContent = word;
  stateSub.textContent = sub ?? fallback;
  startButton.hidden = hero !== "offline";
}

/** The engine takes several seconds to load the model, so "starting" is a
 *  state the user needs to see rather than a silent gap. */
let starting = false;

async function startEngine(): Promise<void> {
  if (starting) return;
  starting = true;
  startButton.disabled = true;
  setHero("starting");
  try {
    await invoke("start_engine");
  } catch (err) {
    starting = false;
    startButton.disabled = false;
    setHero("offline", `[ERROR: ${String(err)}]`);
    return;
  }
  // The client reconnects on its own; clear the flag once it lands or after
  // long enough that something has clearly gone wrong.
  window.setTimeout(() => {
    starting = false;
    startButton.disabled = false;
  }, 30000);
}

startButton.addEventListener("click", () => void startEngine());

const scope = new Scope($<HTMLCanvasElement>("scope"), $("scope-readout"));

// -- push-to-talk key -----------------------------------------------------------

const hotkeyButton = $<HTMLButtonElement>("hotkey");
const hotkeyNote = $("hotkey-note");
let capturing = false;
let noteTimer: number | undefined;

function setNote(text: string, warn = false, clearAfterMs = 0): void {
  window.clearTimeout(noteTimer);
  hotkeyNote.textContent = text;
  hotkeyNote.classList.toggle("warn", warn);
  if (clearAfterMs) {
    noteTimer = window.setTimeout(() => {
      hotkeyNote.textContent = "";
      hotkeyNote.classList.remove("warn");
    }, clearAfterMs);
  }
}

function setCapturing(on: boolean): void {
  capturing = on;
  hotkeyButton.classList.toggle("listening", on);
  hotkeyButton.setAttribute("aria-pressed", String(on));
  hotkeyButton.textContent = on ? "Press a key" : hotkeyLabel;
}

hotkeyButton.addEventListener("click", () => {
  if (capturing) {
    send({ cmd: "cancel_capture" });
    return;
  }
  // The engine's keyboard hook takes the next key press itself: it is the
  // only thing that tells left from right modifiers and sees media keys, and
  // it swallows the press so choosing a key types nothing.
  if (!send({ cmd: "capture_hotkey" })) setNote("[Engine offline]", true, 2500);
});

// -- start with Windows --------------------------------------------------------------

const IN_TAURI = "__TAURI_INTERNALS__" in window;
const autostartSwitch = $<HTMLButtonElement>("autostart");
const autostartNote = $("autostart-note");

interface AutostartStatus {
  enabled: boolean;
  disabled_by_windows: boolean;
  other_copy: boolean;
}

function renderAutostart(status: AutostartStatus): void {
  autostartSwitch.disabled = false;
  autostartSwitch.setAttribute("aria-checked", String(status.enabled));
  // Say why the switch is off when an entry exists anyway, rather than let it
  // look as though nothing is registered.
  const note = status.enabled
    ? ""
    : status.disabled_by_windows
      ? "[Switched off in Windows Startup apps]"
      : status.other_copy
        ? "[Set for another copy of the app]"
        : "";
  autostartNote.textContent = note;
  autostartNote.classList.toggle("warn", note !== "");
}

function showAutostartError(err: unknown): void {
  autostartNote.textContent = `[${String(err)}]`;
  autostartNote.classList.add("warn");
}

async function refreshAutostart(): Promise<void> {
  if (!IN_TAURI) return;
  try {
    renderAutostart(await invoke<AutostartStatus>("autostart_status"));
  } catch (err) {
    showAutostartError(err);
  }
}

autostartSwitch.addEventListener("click", async () => {
  const enable = autostartSwitch.getAttribute("aria-checked") !== "true";
  autostartSwitch.disabled = true;
  try {
    renderAutostart(await invoke<AutostartStatus>("set_autostart", { enabled: enable }));
  } catch (err) {
    autostartSwitch.disabled = false;
    showAutostartError(err);
  }
});

// Windows' own Startup apps page can change this while the app runs, so check
// again whenever the window comes back to the front.
window.addEventListener("focus", () => void refreshAutostart());
void refreshAutostart();

// -- updates -------------------------------------------------------------------------

const tagline = $("tagline");
const updateButton = $<HTMLButtonElement>("update-button");

/** The shell downloads new versions in the background and installs them on the
 *  next restart; this offers that restart now instead of waiting for one. */
function showUpdate(version: string | null): void {
  tagline.hidden = version !== null;
  updateButton.hidden = version === null;
  if (version !== null) {
    updateButton.disabled = false;
    updateButton.textContent = `Update ${version} ready · Restart`;
  }
}

updateButton.addEventListener("click", async () => {
  updateButton.disabled = true;
  updateButton.textContent = "Installing update…";
  try {
    // Does not return on success: the app closes while the installer runs,
    // and the installer reopens it.
    await invoke("install_update");
  } catch (err) {
    updateButton.disabled = false;
    updateButton.textContent = `[Update failed: ${String(err)}]`;
  }
});

if (IN_TAURI) {
  void listen<string>("update-ready", (event) => showUpdate(event.payload));
  invoke<string | null>("update_status").then(showUpdate, () => showUpdate(null));
}

// -- readings and tape log ---------------------------------------------------------

const latencyBar = $("latency-bar");
const statLatency = $("stat-latency");
const statRtf = $("stat-rtf");
const statCount = $("stat-count");
const historyEl = $("history");
const logCount = $("log-count");
const clearButton = $<HTMLButtonElement>("clear-log");

const latencySegments = Array.from({ length: LATENCY_SEGMENTS }, () => {
  const seg = document.createElement("span");
  seg.className = "seg";
  latencyBar.appendChild(seg);
  return seg;
});

function renderReadings(last: Take | undefined): void {
  if (!last) {
    for (const seg of latencySegments) seg.className = "seg";
    statLatency.textContent = "—";
    statLatency.classList.remove("warn");
    statRtf.textContent = "—";
    return;
  }
  const over = last.total_ms > LATENCY_BUDGET_MS;
  const lit = Math.min(LATENCY_SEGMENTS, Math.ceil((last.total_ms / LATENCY_BUDGET_MS) * LATENCY_SEGMENTS));
  latencySegments.forEach((seg, i) => {
    seg.className = i < lit ? (over ? "seg warn" : "seg on") : "seg";
  });
  statLatency.textContent = String(last.total_ms);
  statLatency.classList.toggle("warn", over);
  statRtf.textContent = last.asr_ms > 0 ? ((last.audio_s * 1000) / last.asr_ms).toFixed(0) : "—";
}

let takes: Take[] = [];

function takeItem(take: Take): HTMLLIElement {
  const item = document.createElement("li");
  const idx = document.createElement("span");
  idx.className = "idx";
  idx.textContent = pad3(take.n);
  const said = document.createElement("span");
  said.className = "said";
  said.textContent = take.text;
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = `${take.audio_s.toFixed(1)}s · ${take.asr_ms}ms`;
  item.append(idx, said, meta);
  return item;
}

function renderLog(): void {
  historyEl.replaceChildren();
  if (takes.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = `Hold ${hotkeyLabel} and say something.`;
    historyEl.append(empty);
  } else {
    for (let i = takes.length - 1; i >= 0; i -= 1) historyEl.append(takeItem(takes[i]));
  }
  statCount.textContent = pad3(takes.length);
  logCount.textContent =
    takes.length === 0 ? "Empty" : `${pad3(takes.length)} ${takes.length === 1 ? "take" : "takes"}`;
  clearButton.hidden = takes.length === 0;
  resetClearConfirm();
  renderReadings(takes[takes.length - 1]);
}

let confirmTimer: number | undefined;

function resetClearConfirm(): void {
  window.clearTimeout(confirmTimer);
  clearButton.classList.remove("confirm");
  clearButton.textContent = "Clear";
}

clearButton.addEventListener("click", () => {
  if (!clearButton.classList.contains("confirm")) {
    // Two presses rather than a dialog: modals are off the table, and one
    // stray click should not wipe the session.
    clearButton.classList.add("confirm");
    clearButton.textContent = "Clear all takes?";
    confirmTimer = window.setTimeout(resetClearConfirm, 3000);
    return;
  }
  resetClearConfirm();
  if (!send({ cmd: "clear_log" })) {
    takes = [];
    renderLog();
  }
});

// -- model switcher in the footer ------------------------------------------------

const modelButton = $<HTMLButtonElement>("model-button");
const modelMenu = $("model-menu");
const liveText = $("live-text");

const panel = new ModelsPanel(send, () => {
  if (!modelMenu.hidden) renderMenu();
});

function renderMenu(): void {
  const active = panel.activeModel;
  const items: HTMLElement[] = [];

  for (const model of panel.downloaded()) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "menu-item";
    item.setAttribute("role", "menuitemradio");
    item.setAttribute("aria-checked", String(model.id === active));
    item.disabled = panel.busy;
    const name = document.createElement("span");
    name.textContent = model.id;
    const meta = document.createElement("span");
    meta.className = "menu-meta";
    meta.textContent = panel.switching === model.id ? "Loading" : model.id === active ? "Active" : formatSize(model);
    item.append(name, meta);
    item.addEventListener("click", () => {
      closeMenu();
      if (model.id !== active) send({ cmd: "switch_model", model: model.id });
    });
    items.push(item);
  }

  if (items.length) {
    const rule = document.createElement("div");
    rule.className = "menu-rule";
    rule.setAttribute("role", "separator");
    items.push(rule);
  }

  const all = document.createElement("button");
  all.type = "button";
  all.className = "menu-item";
  all.setAttribute("role", "menuitem");
  all.textContent = "All models";
  all.addEventListener("click", () => {
    closeMenu();
    showView("models");
  });
  items.push(all);

  modelMenu.replaceChildren(...items);
}

function openMenu(): void {
  renderMenu();
  modelMenu.hidden = false;
  modelButton.setAttribute("aria-expanded", "true");
  modelMenu.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
}

function closeMenu(): void {
  if (modelMenu.hidden) return;
  modelMenu.hidden = true;
  modelButton.setAttribute("aria-expanded", "false");
}

modelButton.addEventListener("click", () => (modelMenu.hidden ? openMenu() : closeMenu()));

document.addEventListener("click", (event) => {
  const target = event.target as Node;
  if (!modelMenu.hidden && !modelMenu.contains(target) && !modelButton.contains(target)) closeMenu();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !modelMenu.hidden) {
    closeMenu();
    modelButton.focus();
  }
});

// -- events ------------------------------------------------------------------------

function handle(event: EngineEvent): void {
  switch (event.event) {
    case "ready":
      starting = false;
      startButton.disabled = false;
      hotkeyLabel = event.hotkey;
      if (!capturing) hotkeyButton.textContent = event.hotkey;
      modelButton.textContent = event.model;
      liveText.textContent = event.streaming ? "on" : "off";
      if (takes.length === 0) renderLog(); // the empty-state hint names the key
      setHero("ready");
      break;

    case "state":
      scope.setRecording(event.state === "recording");
      if (event.state === "loading") {
        setHero("starting", panel.switching ? `Loading ${panel.switching}. This takes a few seconds.` : undefined);
      } else if (event.state === "recording") setHero("rec");
      else if (event.state === "transcribing") setHero("working");
      else setHero("ready");
      break;

    case "level":
      scope.push(event.peak ?? event.rms, event.bands);
      break;

    case "transcript":
      if (event.take !== undefined) {
        takes.push({
          n: event.take,
          text: event.text,
          audio_s: event.audio_s,
          asr_ms: event.asr_ms,
          total_ms: event.total_ms,
          strategy: event.strategy,
        });
        renderLog();
      }
      break;

    case "log":
      takes = event.takes;
      renderLog();
      break;

    case "hotkey_capture":
      if (event.status === "listening") {
        setCapturing(true);
        setNote("Esc to cancel");
      } else if (event.status === "saved") {
        setCapturing(false);
        setNote("[Saved]", false, 2000);
      } else if (event.status === "refused") {
        setCapturing(false);
        setNote(`[Can't use ${event.label}: ${event.reason}]`, true, 4500);
      } else if (event.status === "busy") {
        setNote("[Finish the current take first]", true, 2500);
      } else {
        setCapturing(false);
        setNote("");
      }
      break;

    case "gpu_runtime":
      // First launch on an NVIDIA machine downloads the GPU libraries before
      // the model; say so, with progress, instead of sitting on LOADING.
      if (document.body.dataset.hero === "starting") {
        if (event.status === "downloading") {
          const pct = event.total_bytes
            ? Math.floor(((event.done_bytes ?? 0) / event.total_bytes) * 100)
            : null;
          const size = event.total_bytes ? ` of ${(event.total_bytes / 1e9).toFixed(1)} GB` : "";
          setHero(
            "starting",
            pct === null
              ? "Downloading GPU libraries. This only happens once."
              : `Downloading GPU libraries: ${pct}%${size}. This only happens once.`,
          );
        } else if (event.status === "error") {
          setHero("starting", `[GPU libraries: ${event.message}] Retrying…`);
        }
      }
      break;

    case "models":
    case "model_download":
    case "model_switch":
      panel.handle(event);
      // On first launch the model downloads before anything else can happen,
      // so the hero explains the wait instead of sitting on LOADING.
      if (event.event === "model_download" && document.body.dataset.hero === "starting") {
        if (event.status === "downloading") {
          const pct = event.total_bytes
            ? Math.floor(((event.done_bytes ?? 0) / event.total_bytes) * 100)
            : null;
          const size = event.total_bytes ? ` of ${(event.total_bytes / 1e9).toFixed(1)} GB` : "";
          setHero(
            "starting",
            pct === null
              ? `Downloading ${event.model}. This only happens once.`
              : `Downloading ${event.model}: ${pct}%${size}. This only happens once.`,
          );
        } else if (event.status === "error") {
          setHero("starting", `[Download failed: ${event.message}] Retrying…`);
        }
      }
      break;

    case "error":
      setHero("error", `[ERROR: ${event.message}]`);
      break;
  }
}

client.on(handle);
client.onOpen(() => {
  scope.setConnected(true);
  if (!document.hidden) send({ cmd: "monitor", on: true });
});

// Stop the idle level stream while the window is minimised or hidden.
document.addEventListener("visibilitychange", () => send({ cmd: "monitor", on: !document.hidden }));

// -- QA demo states --------------------------------------------------------------------

function demoModels(): ModelsEvent {
  const row = (
    id: string, params_m: number, size_gb: number, size_exact: boolean, english_only: boolean,
    accuracy: number, speed: number, vram_gb: number, downloaded: boolean, fit: ModelFit, note: string,
  ): ModelInfo => ({ id, params_m, size_gb, size_exact, english_only, accuracy, speed, vram_gb, downloaded, fit, note, blurb: "" });
  return {
    event: "models",
    system: {
      cpu: "AMD Ryzen 9 7900X 12-Core Processor", threads: 24, ram_gb: 15.1,
      gpu: "NVIDIA GeForce RTX 4080 SUPER", vram_gb: 16, cuda: true,
    },
    active: "large-v3-turbo",
    recommended: "large-v3-turbo",
    reason: "Your NVIDIA GeForce RTX 4080 SUPER has 16 GB of VRAM. Turbo gives large-v3 accuracy at dictation speed and needs about 1.3 GB of it.",
    downloading: null,
    models: [
      row("large-v3-turbo", 809, 1.62, true, false, 5, 4, 1.3, true, "good", "≈1.3 GB VRAM"),
      row("large-v3", 1550, 3.09, false, false, 5, 2, 2.1, false, "good", "≈2.1 GB VRAM · slower"),
      row("distil-large-v3.5", 756, 1.51, false, true, 4, 4, 1.2, false, "good", "≈1.2 GB VRAM"),
      row("distil-large-v3", 756, 1.51, false, true, 4, 4, 1.2, false, "good", "≈1.2 GB VRAM"),
      row("medium.en", 769, 1.53, true, true, 4, 3, 1.2, true, "good", "≈1.2 GB VRAM"),
      row("medium", 769, 1.53, false, false, 4, 3, 1.2, false, "good", "≈1.2 GB VRAM"),
      row("distil-medium.en", 394, 0.79, false, true, 3, 4, 0.8, false, "good", "≈0.8 GB VRAM"),
      row("small.en", 244, 0.48, true, true, 3, 5, 0.7, true, "good", "≈0.7 GB VRAM"),
      row("small", 244, 0.48, false, false, 3, 5, 0.7, false, "good", "≈0.7 GB VRAM"),
      row("tiny.en", 39, 0.08, false, true, 1, 5, 0.4, false, "good", "≈0.4 GB VRAM"),
    ],
  };
}

function runDemo(kind: string): void {
  handle({ event: "ready", hotkey: "Right Ctrl", engine: "faster-whisper", model: "large-v3-turbo", device: "cuda", streaming: true });
  handle({
    event: "log",
    takes: [
      { n: 1, text: "Draft a reply saying Thursday works and I will bring the revised numbers.", audio_s: 5.8, asr_ms: 196, total_ms: 212, strategy: "keystrokes" },
      { n: 2, text: "Refactor this function so it streams the response instead of buffering it.", audio_s: 4.9, asr_ms: 171, total_ms: 188, strategy: "keystrokes" },
      { n: 3, text: "Can you move the design review to Thursday and send the updated agenda?", audio_s: 9.1, asr_ms: 233, total_ms: 241, strategy: "keystrokes" },
    ],
  });
  handle(demoModels());

  // A voice-like spectrum that moves, so the visualizer has something to draw:
  // two formant humps drifting over time, strongest in the low-mid bands.
  scope.setConnected(true);
  let t = 0;
  window.setInterval(() => {
    t += 1;
    const loudness = 0.45 + 0.55 * Math.abs(Math.sin(t * 0.09));
    const bands = Array.from({ length: 16 }, (_, i) => {
      const first = Math.exp(-((i - 4 - 2 * Math.sin(t * 0.07)) ** 2) / 10);
      const second = 0.55 * Math.exp(-((i - 10 - Math.sin(t * 0.11)) ** 2) / 6);
      return Math.min(1, (first + second) * loudness);
    });
    scope.push(0.3 * loudness, bands);
  }, 33);

  renderAutostart({ enabled: kind !== "startup-blocked", disabled_by_windows: kind === "startup-blocked", other_copy: false });

  if (kind === "rec") handle({ event: "state", state: "recording" });
  if (kind === "working") handle({ event: "state", state: "transcribing" });
  if (kind === "offline") setHero("offline");
  if (kind === "capture") handle({ event: "hotkey_capture", status: "listening" });
  if (kind === "refused") handle({ event: "hotkey_capture", status: "refused", label: "A", reason: "it types text" });
  if (kind === "confirm") clearButton.click();
  if (kind === "menu") openMenu();
  if (kind === "update") showUpdate("0.2.0");
  if (kind === "models") {
    showView("models");
    handle({ event: "model_download", model: "distil-large-v3.5", status: "downloading", done_bytes: 910_000_000, total_bytes: 1_510_000_000 });
  }
}

renderLog();

if (DEMO) {
  runDemo(DEMO);
} else {
  setHero("starting");
  client.connect();

  // The shell starts the engine at launch, so a few seconds without a
  // connection is normal - the model is loading. Only report it offline, and
  // offer the button, once that has clearly not worked.
  const openedAt = Date.now();
  window.setInterval(() => {
    const connected = client.isConnected();
    scope.setConnected(connected);
    if (connected || starting) return;
    if (Date.now() - openedAt >= OFFLINE_AFTER_MS) setHero("offline");
  }, 1500);
}
