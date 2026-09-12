/**
 * QA hooks driven by the query string: screenshot every state in both themes
 * without a live engine and without changing the Windows theme. The app's own
 * windows load with no query string, so none of this runs in normal use.
 */
const params = new URLSearchParams(window.location.search);

export function themeFromQuery(): void {
  const theme = params.get("theme");
  if (theme === "light" || theme === "dark") {
    document.documentElement.dataset.theme = theme;
    // Marks the theme as forced, so the saved preference cannot override it.
    document.documentElement.dataset.qaTheme = theme;
  }
}

export function demoFromQuery(): string | null {
  const demo = params.get("demo");
  if (demo) {
    // Headless screenshots run on virtual time, which does not advance CSS
    // transitions, so a capture could land mid-fade and show a false colour.
    const style = document.createElement("style");
    style.textContent = "*, *::before, *::after { transition: none !important; }";
    document.head.append(style);
  }
  return demo;
}
