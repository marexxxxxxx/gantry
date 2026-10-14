import { useEffect, useMemo, useState } from "react";
import { fetchStats } from "./api";
import type { StatsBucket, StatsWindow } from "./api";
import TimeChart from "./TimeChart";
import type { Zeitreihe } from "./TimeChart";

/** Kennung eines Zeitfensters (Teil der Chip-Testids). */
type FensterId = "1h" | "24h" | "7d" | "30d";

/** Ein Zeitfenster: Anzeige, Spanne in Millisekunden, Bucket-Breite in Sekunden. */
interface Zeitfenster {
  label: string;
  spanMs: number;
  bucketS: number;
}

/** Fensterdaten (Spanne/Bucket) je Kennung. */
const ZEITFENSTER: Record<FensterId, Zeitfenster> = {
  "1h": { label: "1 h", spanMs: 3_600_000, bucketS: 60 },
  "24h": { label: "24 h", spanMs: 86_400_000, bucketS: 300 },
  "7d": { label: "7 Tage", spanMs: 604_800_000, bucketS: 1800 },
  "30d": { label: "30 Tage", spanMs: 2_592_000_000, bucketS: 10800 },
};

/** Chip-Reihenfolge (kurz nach lang). */
const FENSTER_REIHENFOLGE: FensterId[] = ["1h", "24h", "7d", "30d"];

/** Standardfenster beim Oeffnen der Ansicht. */
const STANDARD_FENSTER: FensterId = "24h";

/** Stabile leere Bucketliste, solange noch nichts geladen ist. */
const LEERE_BUCKETS: StatsBucket[] = [];

/** Zahl oder "-": null heisst "nicht gemessen", 0 ist ein echter Messwert. */
function zahlOderStrich(wert: number | null): string {
  return wert === null ? "-" : String(wert);
}

/** Millisekunden als ganze Zahl mit Einheit, "-" wenn nicht gemessen. */
function millisekunden(wert: number | null): string {
  return wert === null ? "-" : `${Math.round(wert)} ms`;
}

/** Millisekunden als Sekunden mit einer Nachkommastelle und deutschem Komma. */
function sekunden(wert: number | null): string {
  return wert === null ? "-" : `${(wert / 1000).toFixed(1).replace(".", ",")} s`;
}

/** Fehlertext fuer die Meldung; nie ein unbehandelter Wurf. */
function fehlerText(fehler: unknown): string {
  return fehler instanceof Error ? fehler.message : "unbekannter Fehler";
}

/** [Zeit, Wert]-Punkte eines Bucket-Feldes; nicht gemessene Werte fehlen (Luecke). */
function punkte(
  buckets: StatsBucket[],
  feld: (bucket: StatsBucket) => number | null,
): [number, number][] {
  const ergebnis: [number, number][] = [];
  for (const bucket of buckets) {
    const wert = feld(bucket);
    if (wert !== null) {
      ergebnis.push([bucket.ts, wert]);
    }
  }
  return ergebnis;
}

/** Eine Zeitreihe aus einem Bucket-Feld (Memoisierung im Aufrufer). */
function reihe(
  buckets: StatsBucket[],
  label: string,
  feld: (bucket: StatsBucket) => number | null,
): Zeitreihe[] {
  return [{ label, points: punkte(buckets, feld) }];
}

/** Kennwert-Karte: Beschriftung, grosser Wert und optionale Zusatzzeile. */
function Karte({
  testid,
  label,
  wert,
  zusatz,
}: {
  testid: string;
  label: string;
  wert: string;
  zusatz?: string;
}) {
  return (
    <div className="kennwert-karte" data-testid={testid}>
      <span className="kennwert-karte-label">{label}</span>
      <span className="kennwert-karte-wert">{wert}</span>
      {zusatz !== undefined && (
        <span className="kennwert-karte-zusatz">{zusatz}</span>
      )}
    </div>
  );
}

/**
 * Statistik-Ansicht: Zeitfenster-Chips, KPI-Karten und vier Verlaufs-Charts.
 * Laedt beim Mounten und bei jedem Fensterwechsel genau ein Fenster
 * (to = Date.now()); kein Polling, kein Refresh - der Tabwechsel baut die
 * Ansicht neu auf. Die Zeitreihen sind memoisiert, damit die Charts bei
 * unveraenderten Daten nicht neu aufgebaut werden.
 */
export default function StatsView() {
  const [fensterId, setFensterId] = useState<FensterId>(STANDARD_FENSTER);
  const [daten, setDaten] = useState<StatsWindow | null>(null);
  const [laedt, setLaedt] = useState(true);
  const [fehler, setFehler] = useState<string | null>(null);

  const fenster = ZEITFENSTER[fensterId];

  useEffect(() => {
    let veraltet = false;
    const to = Date.now();
    setLaedt(true);
    setFehler(null);
    fetchStats(to - fenster.spanMs, to, fenster.bucketS)
      .then((antwort) => {
        if (!veraltet) {
          setDaten(antwort);
          setLaedt(false);
        }
      })
      .catch((grund: unknown) => {
        if (!veraltet) {
          setFehler(fehlerText(grund));
          setLaedt(false);
        }
      });
    return () => {
      veraltet = true;
    };
  }, [fenster]);

  const buckets = daten === null ? LEERE_BUCKETS : daten.buckets;

  const queueSeries = useMemo<Zeitreihe[]>(
    () => [
      { label: "laufend", points: punkte(buckets, (bucket) => bucket.running) },
      { label: "wartend", points: punkte(buckets, (bucket) => bucket.waiting) },
    ],
    [buckets],
  );
  const durchsatzSeries = useMemo(
    () => reihe(buckets, "tok/s", (bucket) => bucket.throughput),
    [buckets],
  );
  const tokenSeries = useMemo(
    () => reihe(buckets, "Token", (bucket) => bucket.token_usage),
    [buckets],
  );
  const gpuSeries = useMemo(
    () => reihe(buckets, "GPU", (bucket) => bucket.gpu_util),
    [buckets],
  );

  return (
    <div className="stat-ansicht">
      <div className="stat-fenster-leiste" role="group" aria-label="Zeitfenster">
        {FENSTER_REIHENFOLGE.map((id) => (
          <button
            key={id}
            type="button"
            className="stat-chip"
            data-testid={`stat-window-${id}`}
            aria-pressed={id === fensterId}
            onClick={() => setFensterId(id)}
          >
            {ZEITFENSTER[id].label}
          </button>
        ))}
        {laedt && <span className="stat-ladehinweis">lade ...</span>}
      </div>

      {fehler !== null && (
        <p className="stat-fehler" role="alert">
          {fehler}
        </p>
      )}

      {daten !== null &&
        (daten.overview.count === 0 ? (
          <p className="stat-leer" data-testid="stat-leer">
            keine Daten im Zeitfenster
          </p>
        ) : (
          <div className="kennwert-raster">
            <Karte
              testid="kpi-count"
              label="Jobs"
              wert={zahlOderStrich(daten.overview.count)}
              zusatz={`fertig: ${daten.overview.done}`}
            />
            <Karte
              testid="kpi-error"
              label="Fehler"
              wert={zahlOderStrich(daten.overview.error)}
              zusatz={`abgebrochen: ${daten.overview.aborted}`}
            />
            <Karte
              testid="kpi-ttft"
              label="TTFT"
              wert={millisekunden(daten.overview.ttft.p50)}
              zusatz={`p95: ${millisekunden(daten.overview.ttft.p95)}`}
            />
            <Karte
              testid="kpi-duration"
              label="Dauer"
              wert={sekunden(daten.overview.duration.p50)}
              zusatz={`p95: ${sekunden(daten.overview.duration.p95)}`}
            />
            <Karte
              testid="kpi-tokens"
              label="Token"
              wert={`in: ${zahlOderStrich(daten.overview.tokens_in)} / out: ${zahlOderStrich(
                daten.overview.tokens_out,
              )}`}
            />
          </div>
        ))}

      <div className="stat-diagramme">
        <div className="stat-diagramm" data-testid="chart-queue">
          <TimeChart title="Warteschlange" unit="Jobs" series={queueSeries} />
        </div>
        <div className="stat-diagramm" data-testid="chart-throughput">
          <TimeChart title="Durchsatz" unit="tok/s" series={durchsatzSeries} />
        </div>
        <div className="stat-diagramm" data-testid="chart-tokens">
          <TimeChart title="Token" unit="Token" series={tokenSeries} />
        </div>
        <div className="stat-diagramm" data-testid="chart-gpu">
          <TimeChart title="GPU" unit="%" series={gpuSeries} />
        </div>
      </div>
    </div>
  );
}
