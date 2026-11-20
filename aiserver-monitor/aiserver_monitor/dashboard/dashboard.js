/* Gantry dashboard – vanilla JS, no build step, no external dependencies. */
(() => {
  "use strict";

  const REFRESH_PROVIDERS_MS = 5000;
  const REFRESH_SYSTEM_MS = 4000;
  const REFRESH_INFO_MS = 15000;
  const LOG_POLL_MS = 2000;
  const FETCH_TIMEOUT_MS = 9000;
  const ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

  const $ = (id) => document.getElementById(id);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const DEFAULT_SETTINGS = {
    context: 0,
    max_tokens: 0,
    kv: "",
    keep_alive: 0,
    parallel: 1,
    auto_load: true,
    idle_unload_min: 0,
    exclusive_gpu: false,
    vram_reserve_mib: 0,
  };

  const DEFAULT_PROVIDER = {
    id: "",
    name: "",
    kind: "custom",
    enabled: true,
    host: "127.0.0.1",
    port: 8080,
    model: "",
    health_url: "/health",
    pidfile: "",
    logfile: "",
    workdir: ".",
    start_cmd: [],
    stop_cmd: [],
    settings: { ...DEFAULT_SETTINGS },
  };

  const state = {
    providers: [],
    providersLoaded: false,
    providersLoading: false,
    providersError: null,
    system: null,
    systemLoaded: false,
    systemLoading: false,
    systemError: null,
    errors: { providers: null, system: null },
    info: null,
    infoAt: 0,
    lastSyncAt: 0,
    busy: new Map(),
    editor: {
      open: false,
      mode: "create",
      originalId: null,
      dirty: false,
      submitting: false,
      filling: false,
      lastFocus: null,
    },
    logs: {
      open: false,
      id: null,
      lines: 200,
      autoscroll: true,
      loading: false,
      timer: null,
      text: "",
      lastFocus: null,
    },
    confirm: { resolve: null, lastFocus: null },
  };

  // ------------------------------------------------------------------ utils

  class ApiError extends Error {
    constructor(message, status = 0, body = null) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.body = body;
    }
  }

  function errorText(err) {
    if (!err) return "Unbekannter Fehler";
    if (err.name === "AbortError") return "Zeitüberschreitung – das Gateway antwortet nicht.";
    if (err instanceof ApiError) return err.message;
    if (err instanceof TypeError) return "Netzwerkfehler – ist das Gateway erreichbar?";
    return err.message || String(err);
  }

  async function api(path, { method = "GET", body = undefined, timeout = FETCH_TIMEOUT_MS } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const options = {
      method,
      signal: controller.signal,
      headers: { Accept: "application/json" },
      cache: "no-store",
    };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, options);
      const raw = await response.text();
      let data = null;
      if (raw) {
        try {
          data = JSON.parse(raw);
        } catch {
          data = raw;
        }
      }
      if (!response.ok) {
        throw new ApiError(messageFromBody(data, response.status), response.status, data);
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  function messageFromBody(data, status) {
    if (data && typeof data === "object") {
      if (typeof data.error === "string" && data.error.trim()) return data.error;
      if (typeof data.detail === "string" && data.detail.trim()) return data.detail;
      if (Array.isArray(data.detail)) {
        const parts = data.detail
          .map((item) => (item && (item.msg || item.message)) || "")
          .filter(Boolean);
        if (parts.length) return parts.join("; ");
      }
    }
    if (typeof data === "string" && data.trim()) return data.trim();
    return `HTTP ${status}`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function escapeShellSingle(value) {
    return String(value).replaceAll("'", "'\\''");
  }

  function setText(id, value) {
    const node = $(id);
    if (node) node.textContent = value;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  const numberFormat = new Intl.NumberFormat("de-DE");

  function formatInt(value) {
    return Number.isFinite(Number(value)) ? numberFormat.format(Number(value)) : "–";
  }

  function formatTokens(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return "–";
    if (number >= 1024 * 1024) {
      return `${(number / (1024 * 1024)).toLocaleString("de-DE", { maximumFractionDigits: 1 })}M`;
    }
    if (number >= 1024) {
      return `${(number / 1024).toLocaleString("de-DE", { maximumFractionDigits: number % 1024 ? 1 : 0 })}K`;
    }
    return formatInt(number);
  }

  function formatBytes(bytes) {
    const number = Number(bytes);
    if (!Number.isFinite(number) || number < 0) return "–";
    if (number === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    const index = clamp(Math.floor(Math.log(number) / Math.log(1024)), 0, units.length - 1);
    const value = number / 1024 ** index;
    const digits = index === 0 ? 0 : value >= 100 ? 0 : value >= 10 ? 1 : 1;
    return `${value.toLocaleString("de-DE", { minimumFractionDigits: 0, maximumFractionDigits: digits })} ${units[index]}`;
  }

  function formatMiB(mib) {
    const number = Number(mib);
    if (!Number.isFinite(number)) return "–";
    return formatBytes(number * 1024 * 1024);
  }

  function formatUptime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    if (total < 60) return `${total} s`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return `${minutes} Min`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} Std ${minutes % 60} Min`;
    const days = Math.floor(hours / 24);
    return `${days} T ${hours % 24} Std`;
  }

  function formatClock(date = new Date()) {
    return date.toLocaleTimeString("de-DE", { hour12: false });
  }

  function percent(used, total) {
    const u = Number(used);
    const t = Number(total);
    if (!Number.isFinite(u) || !Number.isFinite(t) || t <= 0) return 0;
    return clamp((u / t) * 100, 0, 100);
  }

  function usageClass(pct) {
    if (pct >= 90) return "crit";
    if (pct >= 70) return "warn";
    return "";
  }

  function statusMeta(status) {
    switch (status) {
      case "running":
        return { label: "läuft", cls: "running" };
      case "starting":
        return { label: "startet", cls: "starting" };
      case "error":
        return { label: "Fehler", cls: "error" };
      case "stopped":
        return { label: "gestoppt", cls: "stopped" };
      default:
        return { label: "unbekannt", cls: "unknown" };
    }
  }

  function findProvider(id) {
    return state.providers.find((provider) => provider.id === id) || null;
  }

  function syncBodyLock() {
    const anyOpen = ["editor-overlay", "logs-overlay", "confirm-overlay"].some((id) => {
      const node = $(id);
      return node && !node.hidden;
    });
    document.body.classList.toggle("modal-open", anyOpen);
  }

  // ------------------------------------------------------------------ toasts

  function toast(title, type = "info", detail = "") {
    const container = $("toasts");
    if (!container) return;
    const icon = type === "success" ? "i-check" : type === "error" ? "i-alert" : "i-bolt";
    const node = document.createElement("div");
    node.className = `toast toast-${type}`;
    node.setAttribute("role", type === "error" ? "alert" : "status");
    node.innerHTML = `
      <svg class="icon"><use href="#${icon}"></use></svg>
      <div class="toast-body"><strong></strong>${detail ? "<span></span>" : ""}</div>
      <button class="toast-close" type="button" aria-label="Meldung schließen">
        <svg class="icon"><use href="#i-x"></use></svg>
      </button>`;
    node.querySelector("strong").textContent = title;
    if (detail) node.querySelector(".toast-body span").textContent = detail;
    let removed = false;
    const remove = () => {
      if (removed) return;
      removed = true;
      node.classList.remove("show");
      setTimeout(() => node.remove(), 230);
    };
    node.querySelector(".toast-close").addEventListener("click", remove);
    container.append(node);
    requestAnimationFrame(() => node.classList.add("show"));
    setTimeout(remove, type === "error" ? 7500 : 4500);
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement("textarea");
        area.value = text;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.append(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
      toast("In die Zwischenablage kopiert", "success");
    } catch (err) {
      toast("Kopieren fehlgeschlagen", "error", errorText(err));
    }
  }

  // ------------------------------------------------------------- data loads

  function setLive(online) {
    const dot = $("live-dot");
    const label = $("live-label");
    if (dot) {
      dot.classList.toggle("online", online === true);
      dot.classList.toggle("offline", online === false);
    }
    if (label) label.textContent = online === true ? "Live" : online === false ? "Offline" : "Verbinde…";
    setText("footer-status", online === true ? "Gateway verbunden" : online === false ? "Gateway nicht erreichbar" : "Verbindung wird aufgebaut");
  }

  function setSourceError(source, message) {
    state.errors[source] = message || null;
    const messages = [state.errors.providers, state.errors.system].filter(Boolean);
    const box = $("global-error");
    if (!box) return;
    if (!messages.length) {
      box.hidden = true;
      setText("global-error-text", "");
    } else {
      box.hidden = false;
      setText("global-error-text", messages.join(" · "));
    }
  }

  function markSynced() {
    state.lastSyncAt = Date.now();
    renderSummary();
  }

  async function loadInfo() {
    try {
      const info = await api("/api/info");
      state.info = info;
      state.infoAt = Date.now();
      setText("gateway-version", info.version || "–");
      renderUptime();
    } catch {
      // Der Header bleibt ohne Info einfach bei "–"; die Live-Anzeige
      // übernimmt bereits der Provider-Poll.
    }
  }

  function renderUptime() {
    if (!state.info) return;
    const elapsed = (Date.now() - state.infoAt) / 1000;
    setText("gateway-uptime", formatUptime((state.info.uptime_seconds || 0) + elapsed));
  }

  async function loadProviders() {
    if (state.providersLoading) return;
    state.providersLoading = true;
    try {
      const data = await api("/api/providers");
      if (!Array.isArray(data)) throw new ApiError("Ungültige Provider-Antwort");
      state.providers = data;
      state.providersLoaded = true;
      state.providersError = null;
      setSourceError("providers", null);
      setLive(true);
      renderProviders();
      renderSummary();
      updateApiHelp();
      markSynced();
    } catch (err) {
      if (err && err.name === "AbortError") return;
      state.providersError = errorText(err);
      setSourceError("providers", `Provider: ${state.providersError}`);
      setLive(false);
      if (!state.providersLoaded) renderProviders();
    } finally {
      state.providersLoading = false;
    }
  }

  async function loadSystem() {
    if (state.systemLoading) return;
    state.systemLoading = true;
    try {
      const data = await api("/api/system");
      if (!data || typeof data !== "object") throw new ApiError("Ungültige System-Antwort");
      state.system = data;
      state.systemLoaded = true;
      state.systemError = null;
      setSourceError("system", null);
      renderSystem();
      renderSummary();
      markSynced();
    } catch (err) {
      if (err && err.name === "AbortError") return;
      state.systemError = errorText(err);
      setSourceError("system", `System: ${state.systemError}`);
      renderSystem();
    } finally {
      state.systemLoading = false;
    }
  }

  // --------------------------------------------------------------- rendering

  function renderSummary() {
    const providers = state.providers || [];
    const running = providers.filter((provider) => provider.status === "running").length;
    const starting = providers.filter((provider) => provider.status === "starting").length;
    setText("summary-providers", state.providersLoaded ? formatInt(providers.length) : "–");
    let providersSub = "wird geladen";
    if (state.providersLoaded) {
      providersSub = `${running} aktiv${starting ? `, ${starting} startet` : ""}`;
      if (!providers.length) providersSub = "noch keine Einträge";
    }
    setText("summary-providers-sub", providersSub);

    const gpus = state.system && Array.isArray(state.system.gpus) ? state.system.gpus : [];
    if (gpus.length) {
      const total = gpus.reduce((sum, gpu) => sum + (Number(gpu.vram_total_mib) || 0), 0);
      const used = gpus.reduce((sum, gpu) => sum + (Number(gpu.vram_used_mib) || 0), 0);
      setText("summary-gpu", `${formatMiB(used)} / ${formatMiB(total)}`);
      setText("summary-gpu-sub", `${formatMiB(Math.max(0, total - used))} frei · ${gpus.length} GPU${gpus.length === 1 ? "" : "s"}`);
    } else if (state.systemLoaded) {
      setText("summary-gpu", "Keine GPU");
      setText("summary-gpu-sub", "nvidia-smi lieferte keine Werte");
    } else {
      setText("summary-gpu", "–");
      setText("summary-gpu-sub", "wird geladen");
    }

    const ram = state.system && state.system.ram ? state.system.ram : {};
    if (ram.total_bytes) {
      setText("summary-ram", `${formatBytes(ram.used_bytes)} / ${formatBytes(ram.total_bytes)}`);
      setText("summary-ram-sub", `${Math.round(percent(ram.used_bytes, ram.total_bytes))}% belegt · ${formatBytes(ram.available_bytes ?? ram.free_bytes)} verfügbar`);
    } else {
      setText("summary-ram", state.systemLoaded ? "Keine Daten" : "–");
      setText("summary-ram-sub", state.systemLoaded ? "free lieferte keine Werte" : "wird geladen");
    }

    if (state.lastSyncAt) {
      setText("summary-sync", formatClock(new Date(state.lastSyncAt)));
      setText("summary-sync-sub", "Auto-Refresh alle 4 s");
    } else {
      setText("summary-sync", "–");
      setText("summary-sync-sub", "Auto-Refresh aktiv");
    }
  }

  function providerCard(provider) {
    const status = statusMeta(provider.status);
    const settings = provider.settings || {};
    const busyAction = state.busy.get(provider.id);
    const busy = Boolean(busyAction);
    const running = provider.status === "running";
    const starting = provider.status === "starting";
    const disabled = provider.enabled === false;

    const flags = [];
    if (disabled) flags.push('<span class="flag warn">deaktiviert</span>');
    if (provider.auto_load) flags.push('<span class="flag on">auto-load</span>');
    else flags.push('<span class="flag off">manuell</span>');
    if (settings.exclusive_gpu) flags.push('<span class="flag">exklusiv-GPU</span>');
    if (Number(provider.idle_unload_min) > 0) {
      flags.push(`<span class="flag">idle ${formatInt(provider.idle_unload_min)} min</span>`);
    }
    if (!flags.length) flags.push('<span class="flag off">keine Regeln</span>');

    const model = provider.model
      ? `<div class="provider-model" title="${escapeHtml(provider.model)}">${escapeHtml(provider.model)}</div>`
      : '<div class="provider-model empty">Kein Modell gesetzt</div>';

    let primaryAction;
    if (starting || busyAction === "start") {
      primaryAction = `<button class="btn btn-ghost btn-sm" type="button" disabled>
        <svg class="icon spin"><use href="#i-refresh"></use></svg> Startet…
      </button>`;
    } else if (running) {
      primaryAction = `<button class="btn btn-ghost btn-sm" type="button" data-action="stop" ${busy ? "disabled" : ""}>
        <svg class="icon"><use href="#i-stop"></use></svg> ${busyAction === "stop" ? "Stoppt…" : "Stop"}
      </button>`;
    } else {
      primaryAction = `<button class="btn btn-primary btn-sm" type="button" data-action="start" ${busy ? "disabled" : ""}>
        <svg class="icon"><use href="#i-play"></use></svg> Start
      </button>`;
    }

    return `
      <article class="card provider-card status-${status.cls} ${running ? "is-running" : ""} ${disabled ? "is-disabled" : ""}" data-provider-id="${escapeHtml(provider.id)}">
        <div class="provider-top">
          <span class="status-dot ${status.cls}" title="Status: ${status.label}" aria-hidden="true"></span>
          <div class="provider-ident">
            <h3 class="provider-name" title="${escapeHtml(provider.name)}">${escapeHtml(provider.name)}</h3>
            <div class="provider-id" title="${escapeHtml(provider.id)}">${escapeHtml(provider.id)}</div>
          </div>
          <div class="provider-badges">
            <span class="status-word ${status.cls}">${status.label}</span>
            <span class="kind-badge">${escapeHtml(provider.kind)}</span>
          </div>
        </div>
        ${model}
        <div class="provider-meta">
          <div class="meta-cell">
            <span class="meta-key">Host:Port</span>
            <span class="meta-value mono" title="${escapeHtml(provider.host)}:${escapeHtml(provider.port)}">${escapeHtml(provider.host || "–")}:${escapeHtml(provider.port || "–")}</span>
          </div>
          <div class="meta-cell">
            <span class="meta-key">Uptime</span>
            <span class="meta-value">${running ? formatUptime(provider.uptime) : "–"}</span>
          </div>
          <div class="meta-cell">
            <span class="meta-key">Context</span>
            <span class="meta-value">${formatTokens(settings.context)}</span>
          </div>
          <div class="meta-cell">
            <span class="meta-key">max_tokens</span>
            <span class="meta-value">${formatTokens(settings.max_tokens)}</span>
          </div>
        </div>
        <div class="flags">${flags.join("")}</div>
        <div class="provider-actions">
          ${primaryAction}
          <button class="btn-icon" type="button" data-action="logs" title="Logs von ${escapeHtml(provider.name)}" aria-label="Logs anzeigen" ${busy ? "disabled" : ""}>
            <svg class="icon"><use href="#i-logs"></use></svg>
          </button>
          <button class="btn-icon" type="button" data-action="edit" title="Provider bearbeiten" aria-label="Provider bearbeiten" ${busy ? "disabled" : ""}>
            <svg class="icon"><use href="#i-edit"></use></svg>
          </button>
          <button class="btn-icon action-danger" type="button" data-action="delete" title="Provider löschen" aria-label="Provider löschen" ${busy ? "disabled" : ""}>
            <svg class="icon"><use href="#i-trash"></use></svg>
          </button>
        </div>
      </article>`;
  }

  function renderProviders() {
    const grid = $("provider-grid");
    if (!grid) return;
    setText("provider-count", state.providersLoaded ? formatInt(state.providers.length) : "–");

    if (!state.providersLoaded && state.providersError) {
      grid.innerHTML = `
        <div class="error-state">
          <span class="error-icon"><svg class="icon"><use href="#i-alert"></use></svg></span>
          <h3>Provider konnten nicht geladen werden</h3>
          <p>${escapeHtml(state.providersError)}</p>
          <button class="btn btn-primary btn-sm" type="button" data-action="retry-providers">
            <svg class="icon"><use href="#i-refresh"></use></svg> Erneut versuchen
          </button>
        </div>`;
      return;
    }

    if (!state.providersLoaded) return;

    if (!state.providers.length) {
      grid.innerHTML = `
        <div class="empty-state">
          <span class="empty-icon"><svg class="icon"><use href="#i-server"></use></svg></span>
          <h3>Noch keine Provider</h3>
          <p>Lege den ersten Eintrag in der Registry an. Er wird sofort als <code>providers/&lt;id&gt;.toml</code> gespeichert – ohne Neustart.</p>
          <button class="btn btn-primary" type="button" data-action="add-provider">
            <svg class="icon"><use href="#i-plus"></use></svg> Provider hinzufügen
          </button>
        </div>`;
      return;
    }

    grid.innerHTML = state.providers.map(providerCard).join("");
  }

  function systemHeader(icon, tone, title, subtitle, extra = "") {
    return `
      <div class="system-card-head">
        <span class="system-icon ${tone}"><svg class="icon"><use href="#${icon}"></use></svg></span>
        <div class="system-title"><h3 title="${escapeHtml(title)}">${escapeHtml(title)}</h3><span>${escapeHtml(subtitle)}</span></div>
        ${extra}
      </div>`;
  }

  function gpuCard(gpu, index) {
    const total = Number(gpu.vram_total_mib) || 0;
    const used = Number(gpu.vram_used_mib) || 0;
    const free = gpu.vram_free_mib === null || gpu.vram_free_mib === undefined
      ? Number.NaN
      : Number(gpu.vram_free_mib);
    const vramPct = percent(used, total);
    const util = Number(gpu.utilization_pct);
    const hasUtil = Number.isFinite(util) && gpu.utilization_pct !== null;
    const temp = Number(gpu.temperature_c);
    const hasTemp = Number.isFinite(temp) && gpu.temperature_c !== null;
    const tempCls = hasTemp ? (temp >= 85 ? "crit" : temp >= 75 ? "warn" : "") : "";
    const gaugeCls = hasUtil ? usageClass(util) : tempCls;
    return `
      <article class="card system-card">
        ${systemHeader("i-gpu", "blue", gpu.name || `GPU ${index}`,
          `GPU ${index} · ${formatMiB(total)} VRAM`,
          `<div class="gauge ${gaugeCls}" style="--pct:${hasUtil ? clamp(util, 0, 100) : 0}"><span>${hasUtil ? Math.round(util) : "–"}${hasUtil ? "<small>%</small>" : ""}</span></div>`)}
        <div class="metric-stack">
          <div>
            <div class="metric-row"><span>VRAM belegt</span><strong>${formatMiB(used)} / ${formatMiB(total)}</strong></div>
            <div class="bar ${usageClass(vramPct)}"><span style="width:${vramPct.toFixed(1)}%"></span></div>
          </div>
        </div>
        <div class="gpu-foot">
          <span>Frei <strong>${formatMiB(Number.isFinite(free) ? free : Math.max(0, total - used))}</strong></span>
          <span class="temp ${tempCls}">${hasTemp ? `${Math.round(temp)} °C` : "Temp –"}</span>
          <span>${hasUtil ? `${Math.round(util)}% Auslastung` : "Auslastung –"}</span>
        </div>
      </article>`;
  }

  function memoryCard(ram, swap) {
    const ramTotal = Number(ram.total_bytes) || 0;
    const ramUsed = Number(ram.used_bytes) || Math.max(0, ramTotal - (Number(ram.free_bytes) || 0));
    const ramPct = percent(ramUsed, ramTotal);
    const swapTotal = Number(swap.total_bytes) || 0;
    const swapUsed = Number(swap.used_bytes) || 0;
    const swapPct = percent(swapUsed, swapTotal);
    const ramAvailable = ram.available_bytes ?? ram.free_bytes;
    const detail = ramTotal
      ? `${formatBytes(ramAvailable)} verfügbar · ${Math.round(ramPct)}% belegt`
      : "Keine Arbeitsspeicher-Daten";
    return `
      <article class="card system-card">
        ${systemHeader("i-memory", "violet", "Arbeitsspeicher", "RAM & Swap")}
        <div class="metric-stack">
          <div>
            <div class="metric-row"><span>RAM</span><strong>${ramTotal ? `${formatBytes(ramUsed)} / ${formatBytes(ramTotal)}` : "–"}</strong></div>
            <div class="bar ${usageClass(ramPct)}"><span style="width:${ramPct.toFixed(1)}%"></span></div>
          </div>
          <div>
            <div class="metric-row"><span>Swap</span><strong>${swapTotal ? `${formatBytes(swapUsed)} / ${formatBytes(swapTotal)}` : "kein Swap"}</strong></div>
            <div class="bar ${usageClass(swapPct)}"><span style="width:${swapPct.toFixed(1)}%"></span></div>
          </div>
        </div>
        <div class="mem-foot"><span>${detail}</span></div>
      </article>`;
  }

  function diskCard(disk) {
    const total = Number(disk.total_bytes) || 0;
    const used = Number(disk.used_bytes) || 0;
    const pct = percent(used, total);
    return `
      <article class="card system-card">
        ${systemHeader("i-disk", "amber", "Disk", disk.mount || "/")}
        <div class="metric-stack">
          <div>
            <div class="metric-row"><span>Belegt</span><strong>${total ? `${formatBytes(used)} / ${formatBytes(total)}` : "–"}</strong></div>
            <div class="bar ${usageClass(pct)}"><span style="width:${pct.toFixed(1)}%"></span></div>
          </div>
        </div>
        <div class="mem-foot">
          <span>Frei <strong>${total ? formatBytes(disk.free_bytes) : "–"}</strong></span>
          <span>${total ? `${Math.round(pct)}% belegt` : "Keine Disk-Daten"}</span>
        </div>
      </article>`;
  }

  function renderSystem() {
    const grid = $("system-grid");
    if (!grid) return;
    setText("system-updated", state.lastSyncAt ? `Zuletzt ${formatClock(new Date(state.lastSyncAt))}` : "–");

    if (!state.systemLoaded) {
      if (state.systemError) {
        grid.innerHTML = `
          <div class="error-state">
            <span class="error-icon"><svg class="icon"><use href="#i-alert"></use></svg></span>
            <h3>Systemwerte nicht erreichbar</h3>
            <p>${escapeHtml(state.systemError)}</p>
            <button class="btn btn-primary btn-sm" type="button" data-action="retry-system">
              <svg class="icon"><use href="#i-refresh"></use></svg> Erneut versuchen
            </button>
          </div>`;
      }
      return;
    }

    const data = state.system || {};
    const gpus = Array.isArray(data.gpus) ? data.gpus : [];
    const parts = gpus.map(gpuCard);
    if (!gpus.length) {
      parts.push(`
        <article class="card system-card system-empty">
          <svg class="icon"><use href="#i-gpu"></use></svg>
          <strong>Keine GPU erkannt</strong>
          <p>nvidia-smi lieferte keine Geräte. Der Rest des Gateways läuft unabhängig davon weiter.</p>
        </article>`);
    }
    parts.push(memoryCard(data.ram || {}, data.swap || {}));
    parts.push(diskCard(data.disk || {}));
    grid.innerHTML = parts.join("");

    const errors = Array.isArray(data.errors) ? data.errors.filter(Boolean) : [];
    const note = $("system-note");
    if (note) {
      if (errors.length) {
        note.hidden = false;
        note.textContent = `Teilweise keine Live-Werte: ${errors.join(" · ")}`;
      } else {
        note.hidden = true;
        note.textContent = "";
      }
    }
  }

  function updateApiHelp() {
    const origin = window.location.origin;
    setText("api-endpoint", `${origin}/v1`);
    const providerWithModel = state.providers.find((provider) => provider.model && provider.status === "running")
      || state.providers.find((provider) => provider.model);
    const model = providerWithModel ? providerWithModel.model : "qwen-coder";
    const payload = {
      model,
      messages: [{ role: "user", content: "Hallo Gantry" }],
      max_tokens: 256,
    };
    const pretty = JSON.stringify(payload, null, 2);
    const curl = `curl ${origin}/v1/chat/completions \\\n  -H 'Content-Type: application/json' \\\n  -d '${escapeShellSingle(pretty)}'`;
    const pre = $("curl-example");
    if (pre) pre.textContent = curl;
  }

  // ---------------------------------------------------------------- actions

  async function providerAction(action, id) {
    const provider = findProvider(id);
    if (!provider || state.busy.has(id)) return;
    if (action === "logs") return openLogs(id);
    if (action === "edit") return openEditor("edit", id);
    if (action === "delete") return deleteProvider(id);
    if (action !== "start" && action !== "stop") return;

    state.busy.set(id, action);
    if (action === "start") {
      provider.status = "starting";
      provider.uptime = 0;
    }
    renderProviders();
    try {
      const result = await api(`/api/providers/${encodeURIComponent(id)}/${action}`, {
        method: "POST",
        timeout: action === "start" ? 620000 : 80000,
      });
      toast(
        action === "start" ? "Provider gestartet" : "Provider gestoppt",
        "success",
        `${result.name || provider.name} (${result.id || id})`,
      );
    } catch (err) {
      const detail = errorText(err);
      const tail = err && err.body && Array.isArray(err.body.log_tail)
        ? err.body.log_tail.slice(-3).join(" · ")
        : "";
      toast(
        action === "start" ? "Start fehlgeschlagen" : "Stop fehlgeschlagen",
        "error",
        tail ? `${detail} — ${tail}` : detail,
      );
    } finally {
      state.busy.delete(id);
      await loadProviders();
      renderProviders();
    }
  }

  async function deleteProvider(id) {
    const provider = findProvider(id);
    if (!provider) return;
    const confirmed = await confirmDialog({
      title: "Provider löschen?",
      message: `„${provider.name}“ (${provider.id}) wird aus providers/${provider.id}.toml entfernt. Ein laufender Modellprozess wird dabei nicht automatisch gestoppt.`,
      confirmLabel: "Löschen",
      danger: true,
    });
    if (!confirmed) return;

    state.busy.set(id, "delete");
    renderProviders();
    try {
      await api(`/api/providers/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (state.logs.open && state.logs.id === id) closeLogs();
      toast("Provider gelöscht", "success", `${provider.name} (${provider.id})`);
      await loadProviders();
    } catch (err) {
      toast("Löschen fehlgeschlagen", "error", errorText(err));
    } finally {
      state.busy.delete(id);
      renderProviders();
    }
  }

  // ----------------------------------------------------------------- editor

  function openOverlay(id) {
    const overlay = $(id);
    if (!overlay) return;
    overlay.hidden = false;
    syncBodyLock();
  }

  function closeOverlay(id) {
    const overlay = $(id);
    if (!overlay) return;
    overlay.hidden = true;
    syncBodyLock();
  }

  async function openEditor(mode, id = null) {
    let config;
    if (mode === "edit") {
      try {
        config = await api(`/api/providers/${encodeURIComponent(id)}/edit`);
      } catch (err) {
        toast("Provider konnte nicht geladen werden", "error", errorText(err));
        return;
      }
    } else {
      config = JSON.parse(JSON.stringify(DEFAULT_PROVIDER));
    }

    state.editor.open = true;
    state.editor.mode = mode;
    state.editor.originalId = mode === "edit" ? id : null;
    state.editor.dirty = false;
    state.editor.submitting = false;
    state.editor.lastFocus = document.activeElement;

    setText("editor-eyebrow", mode === "edit" ? "Eintrag bearbeiten" : "Neuer Eintrag");
    setText("editor-title", mode === "edit" ? config.name || id : "Provider hinzufügen");
    setText("editor-subtitle", mode === "edit"
      ? "Änderungen werden direkt in der Registry gespeichert."
      : "Startbefehl, Port und Einstellungen für die Registry.");
    setText("editor-submit-label", mode === "edit" ? "Speichern" : "Provider anlegen");
    setText("id-hint", mode === "edit"
      ? "Eine neue ID benennt den Provider beim Speichern um. Kleinbuchstaben, Ziffern, - und _."
      : "Kleinbuchstaben, Ziffern, - und _. Wird zum Dateinamen providers/<id>.toml.");

    fillEditor(config);
    clearEditorErrors();
    hideEditorError();
    setEditorBusy(false);
    openOverlay("editor-overlay");
    setTimeout(() => {
      const target = mode === "edit" ? $("f-name") : $("f-id");
      if (target) target.focus();
    }, 30);
  }

  function fillEditor(config) {
    const settings = { ...DEFAULT_SETTINGS, ...(config.settings || {}) };
    state.editor.filling = true;
    const setValue = (id, value) => {
      const node = $(id);
      if (node) node.value = value ?? "";
    };
    const setChecked = (id, value) => {
      const node = $(id);
      if (node) node.checked = Boolean(value);
    };
    setValue("f-id", config.id || "");
    setValue("f-name", config.name || "");
    setValue("f-kind", config.kind || "custom");
    setChecked("f-enabled", config.enabled !== false);
    setValue("f-host", config.host ?? "127.0.0.1");
    setValue("f-port", config.port ?? 0);
    setValue("f-model", config.model || "");
    setValue("f-health-url", config.health_url || "");
    setValue("f-workdir", config.workdir || ".");
    setValue("f-logfile", config.logfile || "");
    setValue("f-pidfile", config.pidfile || "");
    setValue("f-start-cmd", Array.isArray(config.start_cmd) ? config.start_cmd.join("\n") : (config.start_cmd || ""));
    setValue("f-stop-cmd", Array.isArray(config.stop_cmd) ? config.stop_cmd.join("\n") : (config.stop_cmd || ""));
    setValue("f-context", settings.context ?? 0);
    setValue("f-max-tokens", settings.max_tokens ?? 0);
    setValue("f-kv", settings.kv ?? "");
    setValue("f-keep-alive", settings.keep_alive ?? 0);
    setValue("f-parallel", settings.parallel ?? 1);
    setValue("f-idle-unload", settings.idle_unload_min ?? 0);
    setValue("f-vram-reserve", settings.vram_reserve_mib ?? 0);
    setChecked("f-auto-load", settings.auto_load !== false);
    setChecked("f-exclusive-gpu", settings.exclusive_gpu === true);
    state.editor.dirty = false;
    state.editor.filling = false;
    setEditorBusy(false);
  }

  function inputValue(id) {
    const node = $(id);
    return node ? node.value.trim() : "";
  }

  function inputChecked(id) {
    const node = $(id);
    return Boolean(node && node.checked);
  }

  function inputNumber(id) {
    const raw = inputValue(id);
    return raw === "" ? 0 : Number(raw);
  }

  function textareaLines(id) {
    return inputValue(id)
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function readEditor() {
    return {
      id: inputValue("f-id"),
      name: inputValue("f-name"),
      kind: inputValue("f-kind") || "custom",
      enabled: inputChecked("f-enabled"),
      host: inputValue("f-host"),
      port: inputNumber("f-port"),
      model: inputValue("f-model"),
      health_url: inputValue("f-health-url"),
      pidfile: inputValue("f-pidfile"),
      logfile: inputValue("f-logfile"),
      workdir: inputValue("f-workdir"),
      start_cmd: textareaLines("f-start-cmd"),
      stop_cmd: textareaLines("f-stop-cmd"),
      settings: {
        context: inputNumber("f-context"),
        max_tokens: inputNumber("f-max-tokens"),
        kv: inputValue("f-kv"),
        keep_alive: inputNumber("f-keep-alive"),
        parallel: inputNumber("f-parallel"),
        auto_load: inputChecked("f-auto-load"),
        idle_unload_min: inputNumber("f-idle-unload"),
        exclusive_gpu: inputChecked("f-exclusive-gpu"),
        vram_reserve_mib: inputNumber("f-vram-reserve"),
      },
    };
  }

  function validateEditor(data) {
    const errors = {};
    if (!data.id) {
      errors.id = "ID ist erforderlich.";
    } else if (!ID_RE.test(data.id)) {
      errors.id = "Nur Kleinbuchstaben, Ziffern, - und _ (max. 64 Zeichen).";
    }
    if (!data.name) errors.name = "Anzeigename ist erforderlich.";
    if (!data.host) errors.host = "Host ist erforderlich.";
    if (!Number.isInteger(data.port) || data.port < 0 || data.port > 65535) {
      errors.port = "Port muss eine ganze Zahl zwischen 0 und 65535 sein.";
    }
    if (!Number.isInteger(data.settings.context) || data.settings.context < 0) {
      errors.context = "Context muss eine ganze Zahl ≥ 0 sein.";
    }
    if (!Number.isInteger(data.settings.max_tokens) || data.settings.max_tokens < 0) {
      errors.max_tokens = "max_tokens muss eine ganze Zahl ≥ 0 sein.";
    }
    if (!Number.isInteger(data.settings.keep_alive) || data.settings.keep_alive < -1) {
      errors.keep_alive = "keep_alive muss eine ganze Zahl ≥ -1 sein.";
    }
    if (!Number.isInteger(data.settings.parallel) || data.settings.parallel < 1) {
      errors.parallel = "Mindestens ein Parallel-Slot.";
    }
    if (!Number.isInteger(data.settings.idle_unload_min) || data.settings.idle_unload_min < 0) {
      errors.idle_unload_min = "Idle-Unload muss eine ganze Zahl ≥ 0 sein.";
    }
    if (!Number.isInteger(data.settings.vram_reserve_mib) || data.settings.vram_reserve_mib < 0) {
      errors.vram_reserve_mib = "VRAM-Reserve muss eine ganze Zahl ≥ 0 sein.";
    }
    return errors;
  }

  function clearEditorErrors() {
    $$("[data-error-for]").forEach((node) => {
      node.textContent = "";
    });
    $$("#editor-form .field").forEach((field) => field.classList.remove("invalid"));
  }

  function showFieldErrors(errors) {
    clearEditorErrors();
    let firstField = null;
    for (const [key, message] of Object.entries(errors)) {
      const errorNode = document.querySelector(`[data-error-for="${key}"]`);
      if (!errorNode) continue;
      errorNode.textContent = message;
      const field = errorNode.closest(".field");
      if (field) {
        field.classList.add("invalid");
        if (!firstField) firstField = field;
      }
    }
    if (firstField) {
      const focusable = firstField.querySelector("input, select, textarea");
      if (focusable) focusable.focus();
    }
  }

  function showEditorError(message) {
    const box = $("editor-error");
    if (!box) return;
    box.hidden = false;
    setText("editor-error-text", message);
  }

  function hideEditorError() {
    const box = $("editor-error");
    if (!box) return;
    box.hidden = true;
    setText("editor-error-text", "");
  }

  function setEditorBusy(busy) {
    const submit = $("editor-submit");
    const close = $("editor-close");
    const cancel = $("editor-cancel");
    if (submit) submit.disabled = busy;
    if (close) close.disabled = busy;
    if (cancel) cancel.disabled = busy;
    const label = $("editor-submit-label");
    if (label) {
      label.textContent = busy
        ? "Speichert…"
        : state.editor.mode === "edit" ? "Speichern" : "Provider anlegen";
    }
  }

  async function submitEditor(event) {
    event.preventDefault();
    if (state.editor.submitting) return;
    const data = readEditor();
    const errors = validateEditor(data);
    showFieldErrors(errors);
    if (Object.keys(errors).length) {
      toast("Bitte Eingaben prüfen", "error", Object.values(errors)[0]);
      return;
    }

    state.editor.submitting = true;
    setEditorBusy(true);
    hideEditorError();
    try {
      let editId = state.editor.originalId;
      if (state.editor.mode === "edit" && data.id !== editId) {
        await api(`/api/providers/${encodeURIComponent(editId)}/rename`, {
          method: "POST",
          body: { id: data.id },
        });
        editId = data.id;
        state.editor.originalId = data.id;
      }

      const payload = {
        name: data.name,
        kind: data.kind,
        enabled: data.enabled,
        host: data.host,
        port: data.port,
        model: data.model,
        health_url: data.health_url,
        pidfile: data.pidfile,
        logfile: data.logfile,
        workdir: data.workdir,
        start_cmd: data.start_cmd,
        stop_cmd: data.stop_cmd,
        settings: data.settings,
      };

      if (state.editor.mode === "create") {
        payload.id = data.id;
        const created = await api("/api/providers", { method: "POST", body: payload });
        toast("Provider angelegt", "success", `${created.name} (${created.id})`);
      } else {
        const updated = await api(`/api/providers/${encodeURIComponent(editId)}`, {
          method: "PATCH",
          body: payload,
        });
        toast("Änderungen gespeichert", "success", `${updated.name} (${updated.id})`);
      }
      state.editor.dirty = false;
      closeEditor(true);
      await loadProviders();
    } catch (err) {
      const message = errorText(err);
      showEditorError(message);
      toast(
        state.editor.mode === "create" ? "Anlegen fehlgeschlagen" : "Speichern fehlgeschlagen",
        "error",
        message,
      );
    } finally {
      state.editor.submitting = false;
      setEditorBusy(false);
    }
  }

  async function requestCloseEditor() {
    if (state.editor.submitting) return;
    if (state.editor.dirty) {
      const discard = await confirmDialog({
        title: "Änderungen verwerfen?",
        message: "Die Eingaben in diesem Formular wurden noch nicht gespeichert.",
        confirmLabel: "Verwerfen",
        danger: true,
      });
      if (!discard) return;
    }
    closeEditor();
  }

  function closeEditor(force = false) {
    if (state.editor.submitting && !force) return;
    state.editor.open = false;
    state.editor.dirty = false;
    closeOverlay("editor-overlay");
    const previous = state.editor.lastFocus;
    if (previous && typeof previous.focus === "function") {
      setTimeout(() => previous.focus(), 20);
    }
  }

  // ------------------------------------------------------------------- logs

  async function openLogs(id) {
    const provider = findProvider(id);
    if (!provider) return;
    if (state.logs.timer) clearInterval(state.logs.timer);
    state.logs = {
      open: true,
      id,
      lines: 200,
      autoscroll: true,
      loading: false,
      timer: null,
      text: "",
      lastFocus: document.activeElement,
    };
    setText("logs-title", `Logs · ${provider.name}`);
    setText("logs-path", provider.logfile || "Logdatei aus der Registry");
    setText("logs-status", "lade …");
    const select = $("logs-lines");
    if (select) select.value = "200";
    const autoscroll = $("logs-autoscroll");
    if (autoscroll) autoscroll.checked = true;
    const pre = $("logs-pre");
    if (pre) {
      pre.textContent = "Lade Log …";
      pre.classList.remove("logs-empty");
    }
    const note = $("logs-note");
    if (note) note.hidden = true;
    openOverlay("logs-overlay");
    await fetchLogs();
    if (!state.logs.open || state.logs.id !== id) return;
    state.logs.timer = setInterval(() => {
      if (!document.hidden) fetchLogs();
    }, LOG_POLL_MS);
  }

  async function fetchLogs() {
    if (!state.logs.open || state.logs.loading) return;
    const id = state.logs.id;
    const lines = state.logs.lines;
    state.logs.loading = true;
    try {
      const data = await api(`/api/providers/${encodeURIComponent(id)}/logs?lines=${lines}`, {
        timeout: 10000,
      });
      if (!state.logs.open || state.logs.id !== id || state.logs.lines !== lines) return;
      const pre = $("logs-pre");
      if (!pre) return;
      const scrollTop = pre.parentElement ? pre.parentElement.scrollTop : 0;
      const text = Array.isArray(data.lines) ? data.lines.join("\n") : "";
      state.logs.text = text;
      if (text) {
        pre.textContent = text;
        pre.classList.remove("logs-empty");
      } else if (data.error) {
        pre.textContent = data.error;
        pre.classList.add("logs-empty");
      } else {
        pre.textContent = "Noch keine Logzeilen vorhanden.";
        pre.classList.add("logs-empty");
      }
      setText("logs-path", data.logfile || "kein logfile konfiguriert");
      setText("logs-status", `${formatClock()} · ${Array.isArray(data.lines) ? data.lines.length : 0} Zeilen`);
      const note = $("logs-note");
      if (note) {
        if (data.error) {
          note.hidden = false;
          note.textContent = data.error;
        } else {
          note.hidden = true;
          note.textContent = "";
        }
      }
      const body = pre.parentElement;
      if (body) {
        if (state.logs.autoscroll) body.scrollTop = body.scrollHeight;
        else body.scrollTop = scrollTop;
      }
    } catch (err) {
      if (!state.logs.open || state.logs.id !== id) return;
      setText("logs-status", "Fehler");
      const note = $("logs-note");
      if (note) {
        note.hidden = false;
        note.textContent = errorText(err);
      }
    } finally {
      state.logs.loading = false;
    }
  }

  function closeLogs() {
    if (state.logs.timer) {
      clearInterval(state.logs.timer);
      state.logs.timer = null;
    }
    state.logs.open = false;
    state.logs.id = null;
    closeOverlay("logs-overlay");
    const previous = state.logs.lastFocus;
    if (previous && typeof previous.focus === "function") {
      setTimeout(() => previous.focus(), 20);
    }
  }

  // ---------------------------------------------------------------- confirm

  function confirmDialog({ title, message, confirmLabel = "Bestätigen", danger = true }) {
    return new Promise((resolve) => {
      if (state.confirm.resolve) state.confirm.resolve(false);
      state.confirm.resolve = resolve;
      state.confirm.lastFocus = document.activeElement;
      setText("confirm-title", title);
      setText("confirm-message", message);
      setText("confirm-ok", confirmLabel);
      const ok = $("confirm-ok");
      if (ok) ok.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
      openOverlay("confirm-overlay");
      setTimeout(() => {
        if (ok) ok.focus();
      }, 30);
    });
  }

  function settleConfirm(value) {
    const overlay = $("confirm-overlay");
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    syncBodyLock();
    const resolve = state.confirm.resolve;
    state.confirm.resolve = null;
    if (resolve) resolve(value);
    const previous = state.confirm.lastFocus;
    if (previous && typeof previous.focus === "function") {
      setTimeout(() => previous.focus(), 20);
    }
  }

  // ----------------------------------------------------------------- events

  function bindEvents() {
    document.addEventListener("click", (event) => {
      const add = event.target.closest("[data-action='add-provider']");
      if (add) {
        event.preventDefault();
        openEditor("create");
        return;
      }
      const retryProviders = event.target.closest("[data-action='retry-providers']");
      if (retryProviders) {
        loadProviders();
        return;
      }
      const retrySystem = event.target.closest("[data-action='retry-system']");
      if (retrySystem) {
        loadSystem();
        return;
      }
      const actionButton = event.target.closest("[data-action]");
      if (actionButton) {
        const card = actionButton.closest("[data-provider-id]");
        if (!card) return;
        const action = actionButton.dataset.action;
        if (["start", "stop", "logs", "edit", "delete"].includes(action)) {
          event.preventDefault();
          providerAction(action, card.dataset.providerId);
        }
      }
    });

    $("refresh-providers")?.addEventListener("click", () => loadProviders());
    $("refresh-system")?.addEventListener("click", () => loadSystem());
    $("global-error-retry")?.addEventListener("click", () => {
      loadProviders();
      loadSystem();
      loadInfo();
    });

    $("editor-form")?.addEventListener("submit", submitEditor);
    $("editor-form")?.addEventListener("input", (event) => {
      if (!state.editor.filling) state.editor.dirty = true;
      const field = event.target.closest(".field");
      if (field) {
        field.classList.remove("invalid");
        const error = field.querySelector("[data-error-for]");
        if (error) error.textContent = "";
      }
      hideEditorError();
    });
    $("editor-form")?.addEventListener("change", () => {
      if (!state.editor.filling) state.editor.dirty = true;
    });
    $("editor-close")?.addEventListener("click", requestCloseEditor);
    $("editor-cancel")?.addEventListener("click", requestCloseEditor);

    $("logs-close")?.addEventListener("click", closeLogs);
    $("logs-refresh")?.addEventListener("click", () => fetchLogs());
    $("logs-copy")?.addEventListener("click", () => {
      const text = state.logs.text || $("logs-pre")?.textContent || "";
      if (text) copyText(text);
      else toast("Keine Logzeilen zum Kopieren", "info");
    });
    $("logs-lines")?.addEventListener("change", (event) => {
      state.logs.lines = Number(event.target.value) || 200;
      fetchLogs();
    });
    $("logs-autoscroll")?.addEventListener("change", (event) => {
      state.logs.autoscroll = event.target.checked;
      if (state.logs.autoscroll) {
        const body = $("logs-pre")?.parentElement;
        if (body) body.scrollTop = body.scrollHeight;
      }
    });

    $("confirm-ok")?.addEventListener("click", () => settleConfirm(true));
    $("confirm-cancel")?.addEventListener("click", () => settleConfirm(false));

    $("copy-endpoint")?.addEventListener("click", () => copyText($("api-endpoint")?.textContent || ""));
    $("copy-curl")?.addEventListener("click", () => copyText($("curl-example")?.textContent || ""));

    $("editor-overlay")?.addEventListener("click", (event) => {
      if (event.target === $("editor-overlay")) requestCloseEditor();
    });
    $("logs-overlay")?.addEventListener("click", (event) => {
      if (event.target === $("logs-overlay")) closeLogs();
    });
    $("confirm-overlay")?.addEventListener("click", (event) => {
      if (event.target === $("confirm-overlay")) settleConfirm(false);
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (!$("confirm-overlay")?.hidden) {
          event.preventDefault();
          settleConfirm(false);
          return;
        }
        if (!$("logs-overlay")?.hidden) {
          event.preventDefault();
          closeLogs();
          return;
        }
        if (!$("editor-overlay")?.hidden) {
          event.preventDefault();
          requestCloseEditor();
          return;
        }
      }
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        if (!$("editor-overlay")?.hidden) {
          event.preventDefault();
          $("editor-form")?.requestSubmit();
        }
      }
      if (event.key === "Tab") trapFocus(event);
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        loadProviders();
        loadSystem();
        loadInfo();
      }
    });
  }

  function visibleOverlay() {
    for (const id of ["confirm-overlay", "logs-overlay", "editor-overlay"]) {
      const node = $(id);
      if (node && !node.hidden) return node;
    }
    return null;
  }

  function trapFocus(event) {
    const overlay = visibleOverlay();
    if (!overlay) return;
    const focusables = $$(
      "a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])",
      overlay,
    ).filter((node) => node.offsetParent !== null || node.getClientRects().length > 0);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  // -------------------------------------------------------------------- init

  function init() {
    bindEvents();
    setLive(null);
    updateApiHelp();
    loadProviders();
    loadSystem();
    loadInfo();
    setInterval(() => {
      if (!document.hidden) loadProviders();
    }, REFRESH_PROVIDERS_MS);
    setInterval(() => {
      if (!document.hidden) loadSystem();
    }, REFRESH_SYSTEM_MS);
    setInterval(() => {
      if (!document.hidden) loadInfo();
    }, REFRESH_INFO_MS);
    setInterval(renderUptime, 1000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
