/**
 * The dot-matrix visualizer beside the engine state: sixteen voice bands, low
 * pitches on the left, each rising from the bottom row as the microphone picks
 * up sound in its range.
 *
 * The engine sends the bands already analysed, about thirty times a second.
 * This side adds the ballistics that make it read as a meter rather than
 * flicker: bars snap up and fall back smoothly, and each band leaves a peak
 * dot that holds for a moment before dropping, like the level meter on the
 * recording pill. Clicking it (or Space / Enter while focused) holds the
 * display.
 */

const COLS = 16;
const ROWS = 7;
const PITCH = 12;
const DOT_RADIUS = 2;
const READOUT_MS = 250;
/** No level frames for this long means the stream has stopped: fall silent. */
const STALE_MS = 400;
/** Share of the remaining distance a rising bar closes each frame. */
const ATTACK = 0.65;
/** How fast a bar falls, in full heights per second. */
const RELEASE_PER_S = 2.2;
const PEAK_HOLD_MS = 500;
const PEAK_FALL_PER_S = 1.4;
/** Peak-level mapping, used only if a frame arrives without bands. */
const FLOOR_DB = -54;
const CEILING_DB = -6;

export class Scope {
  private readonly ctx: CanvasRenderingContext2D;
  private readonly target = new Float32Array(COLS);
  private readonly shown = new Float32Array(COLS);
  private readonly peaks = new Float32Array(COLS);
  private readonly peakAt = new Float64Array(COLS);
  /** Dot counts last drawn, so an unchanged frame costs nothing. */
  private readonly drawnBars = new Int8Array(COLS).fill(-1);
  private readonly drawnCaps = new Int8Array(COLS).fill(-1);
  private windowPeakDb = -Infinity;
  private lastSignalAt = -Infinity;
  private lastFrameAt = 0;
  private lastReadoutAt = 0;
  private held = false;
  private hovered = false;
  private recording = false;
  private connected = false;
  private dirty = true;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly readout: HTMLElement,
  ) {
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("2D canvas unavailable");
    this.ctx = ctx;

    this.resize();
    // Moving the window to another monitor can change the pixel ratio.
    window.addEventListener("resize", () => this.resize());

    canvas.addEventListener("click", () => this.toggleHold());
    canvas.addEventListener("keydown", (event) => {
      if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        this.toggleHold();
      }
    });
    canvas.addEventListener("pointerenter", () => this.setHovered(true));
    canvas.addEventListener("pointerleave", () => this.setHovered(false));

    // Dot colours come from theme tokens, so redraw whenever the theme changes.
    const invalidate = () => {
      this.dirty = true;
    };
    new MutationObserver(invalidate).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", invalidate);

    requestAnimationFrame((now) => this.frame(now));
  }

  /** Feed one level frame: peak sample magnitude (0 to 1) and band heights. */
  push(peak: number, bands?: number[]): void {
    const db = 20 * Math.log10(Math.max(peak, 1e-6));
    this.windowPeakDb = Math.max(this.windowPeakDb, db);
    this.lastSignalAt = performance.now();

    if (bands && bands.length) {
      for (let i = 0; i < COLS; i += 1) {
        this.target[i] = bands[Math.floor((i * bands.length) / COLS)] ?? 0;
      }
    } else {
      this.target.fill(Math.min(1, Math.max(0, (db - FLOOR_DB) / (CEILING_DB - FLOOR_DB))));
    }
  }

  setRecording(on: boolean): void {
    if (this.recording === on) return;
    this.recording = on;
    this.dirty = true;
  }

  setConnected(on: boolean): void {
    this.connected = on;
  }

  private setHovered(on: boolean): void {
    this.hovered = on;
    this.dirty = true;
  }

  private toggleHold(): void {
    this.held = !this.held;
    this.canvas.setAttribute("aria-pressed", String(this.held));
    this.dirty = true;
    this.renderReadout();
  }

  private resize(): void {
    const ratio = window.devicePixelRatio || 1;
    const width = COLS * PITCH;
    const height = ROWS * PITCH;
    this.canvas.style.width = `${width}px`;
    this.canvas.style.height = `${height}px`;
    this.canvas.width = Math.round(width * ratio);
    this.canvas.height = Math.round(height * ratio);
    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.dirty = true;
  }

  private frame(now: number): void {
    const dt = this.lastFrameAt ? Math.min(0.1, (now - this.lastFrameAt) / 1000) : 0;
    this.lastFrameAt = now;

    if (!this.held) {
      if (now - this.lastSignalAt > STALE_MS) this.target.fill(0);
      for (let i = 0; i < COLS; i += 1) {
        const goal = this.target[i];
        const current = this.shown[i];
        const next =
          goal > current ? current + (goal - current) * ATTACK : Math.max(goal, current - RELEASE_PER_S * dt);
        this.shown[i] = next;

        if (next >= this.peaks[i]) {
          this.peaks[i] = next;
          this.peakAt[i] = now;
        } else if (now - this.peakAt[i] > PEAK_HOLD_MS) {
          this.peaks[i] = Math.max(next, this.peaks[i] - PEAK_FALL_PER_S * dt);
        }
      }
    }

    if (now - this.lastReadoutAt >= READOUT_MS) {
      this.lastReadoutAt = now;
      this.renderReadout();
    }
    this.draw();
    requestAnimationFrame((t) => this.frame(t));
  }

  private renderReadout(): void {
    let text: string;
    if (this.held) text = "Hold";
    else if (!this.connected || performance.now() - this.lastSignalAt > STALE_MS * 2) text = "Input —";
    else if (this.windowPeakDb < -60) text = "Input −∞ dB";
    else text = `Input ${Math.round(this.windowPeakDb)} dB`;

    // The loudest moment since the last update, so the number is readable.
    if (!this.held) this.windowPeakDb = -Infinity;
    if (this.readout.textContent !== text) this.readout.textContent = text;
  }

  private static dots(value: number): number {
    return value > 0.02 ? Math.max(1, Math.round(value * ROWS)) : 0;
  }

  private draw(): void {
    let changed = this.dirty;
    for (let col = 0; col < COLS; col += 1) {
      const bar = Scope.dots(this.shown[col]);
      const cap = Scope.dots(this.peaks[col]);
      if (bar !== this.drawnBars[col] || cap !== this.drawnCaps[col]) {
        this.drawnBars[col] = bar;
        this.drawnCaps[col] = cap;
        changed = true;
      }
    }
    if (!changed) return;
    this.dirty = false;

    const tokens = getComputedStyle(document.documentElement);
    const token = (name: string) => tokens.getPropertyValue(name).trim();
    // Red only while recording: the same signal as the REC state beside it.
    const lit = token(this.held ? "--text-secondary" : this.recording ? "--rec" : "--text-display");
    const capColour = token("--text-secondary");
    const unlit = token(this.hovered ? "--text-disabled" : "--border-visible");

    this.ctx.clearRect(0, 0, COLS * PITCH, ROWS * PITCH);
    for (let col = 0; col < COLS; col += 1) {
      const bar = this.drawnBars[col];
      const cap = this.drawnCaps[col];
      for (let row = 0; row < ROWS; row += 1) {
        const fromBottom = ROWS - row; // 1 is the bottom row
        this.ctx.fillStyle = fromBottom <= bar ? lit : cap > bar && fromBottom === cap ? capColour : unlit;
        this.ctx.beginPath();
        this.ctx.arc(col * PITCH + PITCH / 2, row * PITCH + PITCH / 2, DOT_RADIUS, 0, Math.PI * 2);
        this.ctx.fill();
      }
    }
  }
}
