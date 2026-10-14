/** Typen und Zugriffe des Dashboards auf die Gateway-Schnittstellen.
 *
 * Typen spiegeln exakt die Backend-Formen: ``Job`` kommt aus
 * ``job_to_dict`` (jobs.py), ``Sample`` aus einem Sammler-Sample
 * (metrics_parse.extract_sample bzw. history.last_sample) und
 * ``StatsWindow`` aus ``GET /monitor/stats`` (history.query_stats).
 * Nicht gemessene Werte sind ``null`` (bzw. im SSE-Sample nicht vorhanden),
 * nie 0.
 */

/** Status eines Jobs wie in ``JobStatus`` (jobs.py). */
export type JobStatus = "waiting" | "running" | "done" | "aborted" | "error";

/** Ein Job wie in ``job_to_dict`` serialisiert. */
export interface Job {
  rid: string;
  source: string;
  model: string;
  full_prompt: string;
  prompt_start: string;
  status: JobStatus;
  created_at_ms: number;
  ttft_ms: number | null;
  duration_ms: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  tok_per_s: number | null;
  content_preview: string;
  reasoning_preview: string;
  error: string | null;
}

/** Ein Sample des Metrik-Sammlers (SSE-Event ``stats``, Snapshot-``stats``).
 *
 * ``ts`` sind Epoche-Sekunden. Nicht gemessene Felder fehlen im SSE-Sample
 * ganz (extract_sample) oder sind im Snapshot ``null`` (history.last_sample);
 * ``max_tokens`` gibt es nur im SSE-Sample.
 */
export interface Sample {
  ts: number;
  available: boolean;
  running?: number | null;
  waiting?: number | null;
  throughput?: number | null;
  token_usage?: number | null;
  gpu_util?: number | null;
  vram_used_mb?: number | null;
  vram_total_mb?: number | null;
  max_tokens?: number | null;
}

/** Ein gemitteltes Bucket aus ``GET /monitor/stats``; ``ts`` = Bucket-Start (Sekunden). */
export interface StatsBucket {
  ts: number;
  available: number;
  running: number | null;
  waiting: number | null;
  throughput: number | null;
  token_usage: number | null;
  gpu_util: number | null;
  vram_used_mb: number | null;
  vram_total_mb: number | null;
}

/** Kopfzahlen aus ``GET /monitor/stats`` (jobs nach created_at im Fenster). */
export interface StatsOverview {
  count: number;
  done: number;
  aborted: number;
  error: number;
  tokens_in: number | null;
  tokens_out: number | null;
  ttft: { p50: number | null; p95: number | null };
  duration: { p50: number | null; p95: number | null };
}

/** Antwort von ``GET /monitor/stats`` (from/to in Millisekunden, bucket in Sekunden). */
export interface StatsWindow {
  overview: StatsOverview;
  buckets: StatsBucket[];
}

/** Status der Dashboard-Verbindung (SSE). */
export type VerbindungsStatus = "verbinde" | "verbunden" | "getrennt";

/** Wartezeiten des eigenen Reconnects in Millisekunden (1 s / 2 s / 5 s). */
const WARTEZEITEN_MS = [1000, 2000, 5000];

/** SSE-Ereignisnamen, deren ``data`` als JSON an ``on`` weitergereicht wird. */
const SSE_EREIGNISSE = ["snapshot", "job", "stats"];

/**
 * Abonniert ``GET /monitor/events`` und reicht die JSON-Payloads der
 * Ereignisse ``snapshot`` / ``job`` / ``stats`` an ``on`` weiter.
 *
 * Eigener Reconnect: bei ``error`` wird die Quelle geschlossen und nach
 * 1 s / 2 s / 5 s erneut verbunden; ``open`` setzt den Versuch wieder auf 1 s.
 * ``onStatus`` meldet "verbinde", "verbunden" oder "getrennt". Die
 * zurueckgegebene EventSource beendet mit ``close()`` auch die
 * Reconnect-Schleife (Aufraeumen im React-Effekt).
 */
export function connectEvents(
  on: (event: string, data: any) => void,
  onStatus?: (status: VerbindungsStatus) => void,
): EventSource {
  let aktuell: EventSource | null = null;
  let timer: number | null = null;
  let versuch = 0;
  let beendet = false;

  const verbinden = (): EventSource => {
    onStatus?.("verbinde");
    const neu = new EventSource("/monitor/events");
    aktuell = neu;

    neu.onopen = () => {
      versuch = 0;
      onStatus?.("verbunden");
    };

    neu.onerror = () => {
      // close() direkt am Prototyp: das Handle-close() beendet die Schleife
      // dauerhaft und darf hier nicht greifen.
      EventSource.prototype.close.call(neu);
      if (beendet) {
        return;
      }
      onStatus?.("getrennt");
      const wartezeit =
        WARTEZEITEN_MS[Math.min(versuch, WARTEZEITEN_MS.length - 1)];
      versuch += 1;
      timer = window.setTimeout(verbinden, wartezeit);
    };

    for (const name of SSE_EREIGNISSE) {
      neu.addEventListener(name, (ereignis) => {
        try {
          on(name, JSON.parse((ereignis as MessageEvent).data));
        } catch {
          // Ungueltiges JSON ignorieren; das naechste Ereignis kommt bestimmt.
        }
      });
    }
    return neu;
  };

  const handle = verbinden();
  // close() am zurueckgegebenen Objekt stoppt Reconnect-Timer und Quelle.
  handle.close = () => {
    beendet = true;
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    if (aktuell !== null) {
      EventSource.prototype.close.call(aktuell);
    }
  };
  return handle;
}

/** Wirft bei HTTP-Fehlern einen Fehler; die UI faengt ihn ab. */
async function jsonAntwort<T>(pfad: string, init?: RequestInit): Promise<T> {
  const antwort = await fetch(pfad, init);
  if (!antwort.ok) {
    throw new Error(`${pfad}: HTTP ${antwort.status}`);
  }
  return (await antwort.json()) as T;
}

/** ``GET /monitor/jobs`` -> Snapshot aller Jobs (aktive zuerst). */
export async function fetchJobs(): Promise<Job[]> {
  const daten = await jsonAntwort<{ jobs: Job[] }>("/monitor/jobs");
  return daten.jobs;
}

/** ``GET /monitor/stats``; ``fromMs``/``toMs`` in Millisekunden, ``bucketS`` in Sekunden. */
export async function fetchStats(
  fromMs: number,
  toMs: number,
  bucketS: number,
): Promise<StatsWindow> {
  const parameter = new URLSearchParams({
    from: String(fromMs),
    to: String(toMs),
    bucket: String(bucketS),
  });
  return jsonAntwort<StatsWindow>(`/monitor/stats?${parameter}`);
}

/** ``POST /monitor/jobs/{rid}/cancel`` -> ``{ok}`` (Abbruch des Upstream-Requests). */
export async function cancelJob(rid: string): Promise<{ ok: boolean }> {
  return jsonAntwort<{ ok: boolean }>(
    `/monitor/jobs/${encodeURIComponent(rid)}/cancel`,
    { method: "POST" },
  );
}

/** ``POST /monitor/cancel_all`` -> ``{ok}`` (Notaus). */
export async function cancelAll(): Promise<{ ok: boolean }> {
  return jsonAntwort<{ ok: boolean }>("/monitor/cancel_all", { method: "POST" });
}
