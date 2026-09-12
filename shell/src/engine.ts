/**
 * Client for the engine's event stream, and the commands the settings window
 * sends back to it.
 *
 * The engine is the source of truth and may be running before, after, or
 * without the shell, so this reconnects indefinitely and never treats a
 * dropped socket as an error state worth surfacing loudly.
 */

export type EngineState = "idle" | "loading" | "recording" | "transcribing";

export interface StateEvent {
  event: "state";
  state: EngineState;
  reason?: string;
  audio_s?: number;
}

export interface LevelEvent {
  event: "level";
  rms: number;
  peak?: number;
  /** Sixteen voice-band heights, low to high, each 0 to 1. */
  bands?: number[];
}

export interface TranscriptEvent {
  event: "transcript";
  text: string;
  language: string | null;
  audio_s: number;
  asr_ms: number;
  total_ms: number;
  strategy: string;
  /** Position in the session's tape log. */
  take?: number;
}

/** A live, in-progress transcript. Display only - never typed. */
export interface PartialEvent {
  event: "partial";
  /** Text that survived the previous pass unchanged. */
  stable: string;
  /** The tail that is still being revised as more audio arrives. */
  tentative: string;
  audio_s: number;
  latency_ms: number;
}

export interface ReadyEvent {
  event: "ready";
  hotkey: string;
  engine: string;
  model: string;
  device: string;
  streaming: boolean;
}

export interface ErrorEvent {
  event: "error";
  message: string;
}

export interface Take {
  n: number;
  text: string;
  audio_s: number;
  asr_ms: number;
  total_ms: number;
  strategy: string;
}

/** The whole session log, sent on connect and after it is cleared. */
export interface LogEvent {
  event: "log";
  takes: Take[];
}

export interface HotkeyCaptureEvent {
  event: "hotkey_capture";
  status: "listening" | "saved" | "refused" | "cancelled" | "busy";
  label?: string;
  reason?: string;
}

export interface SystemInfo {
  cpu: string;
  threads: number;
  ram_gb: number;
  gpu: string | null;
  vram_gb: number | null;
  cuda: boolean;
}

export type ModelFit = "good" | "slow" | "too_big" | "english_only";

export interface ModelInfo {
  id: string;
  params_m: number;
  size_gb: number;
  /** True once the model is on disk and its size is measured, not estimated. */
  size_exact: boolean;
  english_only: boolean;
  accuracy: number;
  speed: number;
  vram_gb: number;
  downloaded: boolean;
  fit: ModelFit;
  note: string;
  blurb: string;
}

export interface ModelsEvent {
  event: "models";
  system: SystemInfo;
  active: string;
  recommended: string;
  reason: string;
  downloading: string | null;
  models: ModelInfo[];
}

export interface ModelDownloadEvent {
  event: "model_download";
  model: string;
  status: "downloading" | "done" | "error";
  done_bytes?: number;
  total_bytes?: number | null;
  message?: string;
}

/** First launch on an NVIDIA machine: the GPU libraries downloading, once. */
export interface GpuRuntimeEvent {
  event: "gpu_runtime";
  status: "downloading" | "done" | "error";
  done_bytes?: number;
  total_bytes?: number;
  message?: string;
}

export interface ModelSwitchEvent {
  event: "model_switch";
  model: string;
  status: "loading" | "done" | "error";
  message?: string;
}

export type EngineEvent =
  | StateEvent
  | LevelEvent
  | PartialEvent
  | TranscriptEvent
  | ReadyEvent
  | ErrorEvent
  | LogEvent
  | HotkeyCaptureEvent
  | ModelsEvent
  | ModelDownloadEvent
  | ModelSwitchEvent
  | GpuRuntimeEvent;

export type EngineCommand =
  | { cmd: "monitor"; on: boolean }
  | { cmd: "capture_hotkey" }
  | { cmd: "cancel_capture" }
  | { cmd: "clear_log" }
  | { cmd: "list_models" }
  | { cmd: "download_model"; model: string }
  | { cmd: "switch_model"; model: string };

type Handler = (event: EngineEvent) => void;

export class EngineClient {
  private socket: WebSocket | null = null;
  private handlers: Handler[] = [];
  private openHandlers: (() => void)[] = [];
  private retryMs = 500;
  private connected = false;

  constructor(private url: string) {}

  on(handler: Handler): void {
    this.handlers.push(handler);
  }

  /** Runs on every (re)connection, e.g. to re-subscribe to the mic monitor. */
  onOpen(handler: () => void): void {
    this.openHandlers.push(handler);
  }

  isConnected(): boolean {
    return this.connected;
  }

  /** Send a command. Returns false, rather than queueing, when offline. */
  send(command: EngineCommand): boolean {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(command));
    return true;
  }

  connect(): void {
    try {
      this.socket = new WebSocket(this.url);
    } catch {
      this.scheduleRetry();
      return;
    }

    this.socket.onopen = () => {
      this.connected = true;
      this.retryMs = 500;
      this.emit({ event: "state", state: "idle" });
      for (const handler of this.openHandlers) handler();
    };

    this.socket.onmessage = (message) => {
      try {
        this.emit(JSON.parse(message.data) as EngineEvent);
      } catch {
        /* malformed frame: ignore rather than tear down the socket */
      }
    };

    this.socket.onclose = () => {
      this.connected = false;
      this.scheduleRetry();
    };

    this.socket.onerror = () => this.socket?.close();
  }

  private scheduleRetry(): void {
    // Back off to 5s so a shell left running overnight without an engine
    // is not hammering a closed port.
    setTimeout(() => this.connect(), this.retryMs);
    this.retryMs = Math.min(this.retryMs * 1.6, 5000);
  }

  private emit(event: EngineEvent): void {
    for (const handler of this.handlers) handler(event);
  }
}
