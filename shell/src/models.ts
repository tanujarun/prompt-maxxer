import type {
  EngineCommand,
  ModelDownloadEvent,
  ModelFit,
  ModelInfo,
  ModelSwitchEvent,
  ModelsEvent,
} from "./engine";

/**
 * The Models section: what this PC can run, the pick for it, and every model
 * with its fit, download and switch controls.
 */

export type ModelsPanelEvent = ModelsEvent | ModelDownloadEvent | ModelSwitchEvent;

const FIT_TAG: Record<ModelFit, string> = {
  good: "",
  slow: "Slow here",
  too_big: "Won't fit",
  english_only: "English only",
};

const byId = (id: string) => document.getElementById(id) as HTMLElement;

const gb = (value: number) => (Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1));

export function formatSize(model: Pick<ModelInfo, "size_gb" | "size_exact">): string {
  const prefix = model.size_exact ? "" : "≈";
  if (model.size_gb < 1) return `${prefix}${Math.round(model.size_gb * 1000)} MB`;
  return `${prefix}${model.size_gb.toFixed(model.size_exact ? 2 : 1)} GB`;
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function segmentBar(filled: number, total: number, className: string, label: string): HTMLElement {
  const bar = el("div", className);
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", label);
  for (let i = 0; i < total; i += 1) bar.append(el("span", i < filled ? "seg on" : "seg"));
  return bar;
}

export class ModelsPanel {
  /** The model currently being loaded, if a switch is under way. */
  switching: string | null = null;

  private data: ModelsEvent | null = null;
  private readonly progress = new Map<string, { done: number; total: number | null }>();
  private readonly errors = new Map<string, string>();

  private readonly sysGpu = byId("sys-gpu");
  private readonly sysCpu = byId("sys-cpu");
  private readonly sysRam = byId("sys-ram");
  private readonly pickName = byId("pick-name");
  private readonly pickReason = byId("pick-reason");
  private readonly pickAction = byId("pick-action");
  private readonly list = byId("models");

  constructor(
    private readonly send: (command: EngineCommand) => boolean,
    private readonly onChange: () => void,
  ) {
    byId("models-refresh").addEventListener("click", () => this.send({ cmd: "list_models" }));
  }

  get activeModel(): string | null {
    return this.data?.active ?? null;
  }

  get busy(): boolean {
    return this.switching !== null;
  }

  downloaded(): ModelInfo[] {
    return this.data?.models.filter((model) => model.downloaded) ?? [];
  }

  handle(event: ModelsPanelEvent): void {
    switch (event.event) {
      case "models":
        this.data = event;
        if (event.downloading && !this.progress.has(event.downloading)) {
          this.progress.set(event.downloading, { done: 0, total: null });
        }
        for (const model of event.models) if (model.downloaded) this.progress.delete(model.id);
        break;

      case "model_download":
        if (event.status === "downloading") {
          this.progress.set(event.model, { done: event.done_bytes ?? 0, total: event.total_bytes ?? null });
          this.errors.delete(event.model);
        } else {
          this.progress.delete(event.model);
          if (event.status === "error") this.errors.set(event.model, event.message ?? "Download failed");
        }
        break;

      case "model_switch":
        if (event.status === "loading") {
          this.switching = event.model;
          this.errors.delete(event.model);
        } else {
          this.switching = null;
          if (event.status === "error") this.errors.set(event.model, event.message ?? "Could not load");
        }
        break;
    }
    this.render();
    this.onChange();
  }

  private render(): void {
    const data = this.data;
    if (!data) return;

    const { system } = data;
    if (system.cuda) {
      this.sysGpu.textContent = [system.gpu ?? "CUDA GPU", system.vram_gb !== null ? `${gb(system.vram_gb)} GB` : null]
        .filter(Boolean)
        .join(" · ");
    } else {
      this.sysGpu.textContent = system.gpu ? `${system.gpu} · no CUDA, runs on CPU` : "No CUDA GPU · runs on CPU";
    }
    this.sysCpu.textContent = `${system.cpu} · ${system.threads} threads`;
    this.sysRam.textContent = `${gb(system.ram_gb)} GB`;

    const recommended = data.models.find((model) => model.id === data.recommended);
    this.pickName.textContent = data.recommended;
    this.pickReason.textContent = data.reason;
    this.pickAction.replaceChildren(...(recommended ? [this.action(recommended)] : []));

    this.list.replaceChildren(...data.models.map((model) => this.row(model, data)));
  }

  private row(model: ModelInfo, data: ModelsEvent): HTMLLIElement {
    const item = el("li", "model");
    item.dataset.fit = model.fit;
    if (model.id === data.active) item.dataset.active = "";
    item.title = model.blurb;

    const title = el("div", "model-title");
    title.append(el("span", "model-name", model.id));
    if (model.id === data.recommended) title.append(el("span", "tag strong", "Recommended"));
    if (FIT_TAG[model.fit]) {
      title.append(el("span", model.fit === "slow" ? "tag warn" : "tag", FIT_TAG[model.fit]));
    }

    const error = this.errors.get(model.id);
    const language = model.english_only && model.fit !== "english_only" ? "English · " : "";
    const note = error
      ? el("span", "model-note warn", `[Error: ${error}]`)
      : el("span", "model-note", `${language}${model.note}`);

    const main = el("div", "model-main");
    main.append(title, note);

    item.append(
      main,
      segmentBar(model.accuracy, 5, "bar5", `Accuracy ${model.accuracy} of 5`),
      segmentBar(model.speed, 5, "bar5", `Speed ${model.speed} of 5`),
      el("span", "model-size", formatSize(model)),
      this.action(model),
    );
    return item;
  }

  private action(model: ModelInfo): HTMLElement {
    const box = el("div", "model-action");
    const progress = this.progress.get(model.id);
    const usable = model.fit !== "too_big" && model.fit !== "english_only";

    if (this.switching === model.id) {
      box.append(el("span", "label blink", "Loading"));
    } else if (model.id === this.data?.active) {
      box.append(el("span", "label is-active", "Active"));
    } else if (progress) {
      const pct = progress.total ? Math.min(100, Math.floor((progress.done / progress.total) * 100)) : null;
      box.append(
        segmentBar(
          pct === null ? 0 : Math.round(pct / 12.5),
          8,
          pct === null ? "segbar mini blink" : "segbar mini",
          pct === null ? "Downloading" : `Downloaded ${pct}%`,
        ),
        el("span", "label", pct === null ? "…" : `${pct}%`),
      );
    } else if (!usable) {
      box.append(el("span", "label muted", "—"));
    } else if (model.downloaded) {
      const use = el("button", "button small", "Use");
      use.type = "button";
      use.disabled = this.busy;
      use.addEventListener("click", () => this.send({ cmd: "switch_model", model: model.id }));
      box.append(use);
    } else {
      const get = el("button", "button small", "Download");
      get.type = "button";
      get.title = `Download ${formatSize(model)}`;
      // One download at a time: the engine refuses a second anyway.
      get.disabled = this.progress.size > 0;
      get.addEventListener("click", () => {
        if (this.send({ cmd: "download_model", model: model.id })) {
          this.progress.set(model.id, { done: 0, total: null });
          this.render();
        }
      });
      box.append(get);
    }
    return box;
  }
}
