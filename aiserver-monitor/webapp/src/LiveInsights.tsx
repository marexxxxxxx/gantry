import type { Job, Sample } from "./api";

export interface LiveInsightsProps {
  jobs: Job[];
  sample: Sample | null;
  jetztMs: number;
}

/** Kurze Dauer mit deutschem Komma. */
function dauer(sekunden: number): string {
  if (sekunden < 60) return `${Math.round(sekunden)} s`;
  if (sekunden < 3600) return `${(sekunden / 60).toFixed(1).replace(".", ",")} min`;
  return `${(sekunden / 3600).toFixed(1).replace(".", ",")} h`;
}

/** Zahl mit einer Nachkommastelle, deutsches Komma. */
function komma(wert: number): string {
  return wert.toFixed(1).replace(".", ",");
}

/** Ein Job ist beendet, wenn er nicht mehr wartet oder laeuft. */
function istBeendet(job: Job): boolean {
  return (
    job.status === "done" || job.status === "error" || job.status === "aborted"
  );
}

/** Zeitfenster der Live-Fehlerquote in Millisekunden (15 Minuten). */
const FENSTER_MS = 15 * 60 * 1000;

/** So viele beendete Jobs braucht die Fehlerquote, sonst "zu wenig Daten". */
const MIN_JOBS_FUER_QUOTE = 3;

/**
 * Lage-Leiste der Live-Ansicht: Queue-Kopf-Wartezeit, Saettigungsgrad,
 * Fehlerquote und Durchsatz der letzten 15 Minuten sowie GPU/VRAM. Alles wird
 * aus dem bereits vorhandenen Job-Snapshot und dem letzten Sample abgeleitet -
 * kein zusaetzlicher Netzwerkaufruf und kein Polling.
 */
export default function LiveInsights({
  jobs,
  sample,
  jetztMs,
}: LiveInsightsProps) {
  const laufend = jobs.filter((job) => job.status === "running").length;
  const wartende = jobs.filter((job) => job.status === "waiting");
  const aeltesterWartender =
    wartende.length > 0
      ? Math.min(...wartende.map((job) => job.created_at_ms))
      : null;
  const kopfWartezeit =
    aeltesterWartender === null
      ? null
      : Math.max(0, (jetztMs - aeltesterWartender) / 1000);

  const beendet = jobs.filter(
    (job) => istBeendet(job) && jetztMs - job.created_at_ms <= FENSTER_MS,
  );
  const fehler = beendet.filter((job) => job.status === "error").length;
  const fehlerQuote =
    beendet.length >= MIN_JOBS_FUER_QUOTE
      ? (fehler / beendet.length) * 100
      : null;

  const raten = beendet
    .filter((job) => job.status === "done" && job.tok_per_s !== null)
    .map((job) => job.tok_per_s as number);
  const mittlereRate =
    raten.length > 0
      ? raten.reduce((summe, rate) => summe + rate, 0) / raten.length
      : null;

  const gpu = sample?.gpu_util ?? null;
  const vramBelegt = sample?.vram_used_mb ?? null;
  const vramGesamt = sample?.vram_total_mb ?? null;
  const vramProzent =
    vramBelegt !== null && vramGesamt !== null && vramGesamt > 0
      ? (vramBelegt / vramGesamt) * 100
      : null;

  const offline = sample !== null && sample.available === false;
  const gesaettigt =
    (laufend > 0 && ((gpu !== null && gpu >= 95) || (vramProzent !== null && vramProzent >= 95))) ||
    (wartende.length > 0 &&
      ((gpu !== null && gpu >= 85) || (vramProzent !== null && vramProzent >= 85)));

  const stufe = offline ? "offline" : gesaettigt ? "gesaettigt" : wartende.length > 0 ? "queue" : "frei";
  const stufenText = offline
    ? "Upstream offline"
    : gesaettigt
      ? "gesättigt"
      : wartende.length > 0
        ? "Queue"
        : "frei";

  return (
    <section
      className="lage-leiste"
      data-testid="live-insights"
      aria-label="Lagebild"
    >
      <span
        className={`lage-badge lage-badge-${stufe}`}
        data-testid="live-saturation"
      >
        {stufenText}
      </span>

      <span className="lage-kennwert" data-testid="live-queue-head">
        {kopfWartezeit === null
          ? "Queue leer"
          : `Queue-Kopf wartet ${dauer(kopfWartezeit)}`}
      </span>

      <span className="lage-kennwert">
        <strong className="lage-wert">{laufend}</strong> laufend ·{" "}
        <strong className="lage-wert">{wartende.length}</strong> wartend
      </span>

      <span className="lage-kennwert" data-testid="live-error-rate">
        {fehlerQuote === null
          ? "15 min: zu wenig Daten"
          : `15 min: ${komma(fehlerQuote)} % Fehler (${fehler}/${
              beendet.length
            })`}
      </span>

      <span className="lage-kennwert" data-testid="live-throughput">
        {mittlereRate === null
          ? "Ø Rate: -"
          : `Ø ${komma(mittlereRate)} tok/s (15 min)`}
      </span>

      <span className="lage-kennwert" data-testid="live-gpu">
        {gpu === null ? "GPU -" : `GPU ${Math.round(gpu)} %`}
        {vramBelegt !== null && vramGesamt !== null && (
          <>
            {" · "}
            VRAM {komma(vramBelegt / 1024)} / {komma(vramGesamt / 1024)} GB
            {vramProzent !== null && ` (${Math.round(vramProzent)} %)`}
          </>
        )}
      </span>
    </section>
  );
}
