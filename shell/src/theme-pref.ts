import { getCurrentWindow } from "@tauri-apps/api/window";

/**
 * The theme preference: follow Windows, or force light or dark.
 *
 * Stored in localStorage, which every window of the app shares. The storage
 * event fires in the other windows when it changes, so the overlay pill follows
 * a change made in settings without a round trip through Rust.
 */

export type ThemePref = "auto" | "light" | "dark";

const KEY = "prompt-maxxer.theme";
const IN_TAURI = "__TAURI_INTERNALS__" in window;

export function readThemePref(): ThemePref {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "auto";
  } catch {
    return "auto";
  }
}

function apply(pref: ThemePref): void {
  const root = document.documentElement;
  // A theme forced from the query string, for QA screenshots, wins.
  if (root.dataset.qaTheme) return;

  if (pref === "auto") delete root.dataset.theme;
  else root.dataset.theme = pref;

  // The native title bar follows too, so a dark page never sits under a
  // light frame.
  if (IN_TAURI) {
    getCurrentWindow()
      .setTheme(pref === "auto" ? null : pref)
      .catch(() => {});
  }
}

export function setThemePref(pref: ThemePref): void {
  try {
    localStorage.setItem(KEY, pref);
  } catch {
    /* storage unavailable: still apply it to this window */
  }
  apply(pref);
}

export function initThemePref(onChange?: (pref: ThemePref) => void): void {
  apply(readThemePref());
  window.addEventListener("storage", (event) => {
    if (event.key !== KEY) return;
    const pref = readThemePref();
    apply(pref);
    onChange?.(pref);
  });
}
