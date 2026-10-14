import { useState } from "react";
import type { Job, Sample } from "./api";
import JobTable from "./JobTable";

export interface LiveViewProps {
  jobs: Job[];
  sample: Sample | null;
  samples: Sample[];
  jetztMs: number;
  onVorschau: (rid: string) => void;
}

/** waiting/running gelten als aktiv (wie ACTIVE_STATUSES im Backend). */
function istAktiv(job: Job): boolean {
  return job.status === "waiting" || job.status === "running";
}

/** Zahl oder "-": null/undefined heisst "nicht gemessen", nie 0. */
function wertOderStrich(wert: number | null | undefined): string {
  return wert === null || wert === undefined ? "-" : String(wert);
}

/** Nutzerkoordinaten der Sparkline (Breite x Hoehe des viewBox). */
const SPARK_BREITE = 150;
const SPARK_HOEHE = 22;
const SPARK_RAND = 2;
const SPARK_PUNKT_RADIUS = 1.7;

interface SparkPunkt {
  x: number;
  y: number;
}

/**
 * Zerlegt eine Messreihe in zusammenhaengende Segmente. Luecken (null bzw.
 * nicht gemessene Felder) trennen die Segmente, damit keine Linie zwischen
 * zwei Messungen ueber eine Luecke hinweg gezeichnet wird.
 */
function sparkSegmente(
  werte: (number | null | undefined)[],
  maximum: number,
): SparkPunkt[][] {
  const anzahl = werte.length;
  const schritt = anzahl <= 1 ? 0 : SPARK_BREITE / (anzahl - 1);
  const segmente: SparkPunkt[][] = [];
  let aktuell: SparkPunkt[] = [];
  werte.forEach((wert, index) => {
    if (wert === null || wert === undefined) {
      if (aktuell.length > 0) {
        segmente.push(aktuell);
        aktuell = [];
      }
      return;
    }
    const x = anzahl === 1 ? SPARK_BREITE / 2 : index * schritt;
    const y = SPARK_RAND + (SPARK_HOEHE - 2 * SPARK_RAND) * (1 - wert / maximum);
    aktuell.push({ x, y });
  });
  if (aktuell.length > 0) {
    segmente.push(aktuell);
  }
  return segmente;
}

/** SVG-Pfad durch alle Segmente mit mehr als einem Punkt. */
function sparkPfad(segmente: SparkPunkt[][]): string {
  return segmente
    .filter((segment) => segment.length > 1)
    .map((segment) =>
      segment
        .map(
          (punkt, index) =>
            `${index === 0 ? "M" : "L"}${punkt.x.toFixed(1)} ${punkt.y.toFixed(1)}`,
        )
        .join(" "),
    )
    .join(" ");
}

/** Einzelpunkte (Segmente der Laenge 1) werden als kleine Kreise gezeichnet. */
function sparkEinzelpunkte(segmente: SparkPunkt[][]): SparkPunkt[] {
  return segmente
    .filter((segment) => segment.length === 1)
    .map((segment) => segment[0]);
}

/**
 * Inline-SVG-Sparkline (kein uPlot) fuer laufende und wartende Anfragen ueber
 * die zuletzt empfangenen Stats-Samples; beide Reihen teilen eine y-Skala.
 */
function Sparkline({ samples }: { samples: Sample[] }) {
  const werteLaufend = samples.map((stats) => stats.running);
  const werteWartend = samples.map((stats) => stats.waiting);
  const zahlen = [...werteLaufend, ...werteWartend].filter(
    (wert): wert is number => wert !== null && wert !== undefined,
  );
  const maximum = Math.max(1, ...zahlen);
  const segmenteLaufend = sparkSegmente(werteLaufend, maximum);
  const segmenteWartend = sparkSegmente(werteWartend, maximum);

  return (
    <svg
      className="sparkline"
      width={SPARK_BREITE}
      height={SPARK_HOEHE}
      viewBox={`0 0 ${SPARK_BREITE} ${SPARK_HOEHE}`}
      aria-hidden="true"
    >
      <path
        className="spark-linie spark-linie-running"
        d={sparkPfad(segmenteLaufend)}
      />
      <path
        className="spark-linie spark-linie-waiting"
        d={sparkPfad(segmenteWartend)}
      />
      {sparkEinzelpunkte(segmenteLaufend).map((punkt) => (
        <circle
          className="spark-punkt-running"
          key={`laufend-${punkt.x}`}
          cx={punkt.x}
          cy={punkt.y}
          r={SPARK_PUNKT_RADIUS}
        />
      ))}
      {sparkEinzelpunkte(segmenteWartend).map((punkt) => (
        <circle
          className="spark-punkt-waiting"
          key={`wartend-${punkt.x}`}
          cx={punkt.x}
          cy={punkt.y}
          r={SPARK_PUNKT_RADIUS}
        />
      ))}
    </svg>
  );
}

/**
 * Live-Ansicht: Queue-Leiste mit Sparkline, Filter Aktiv/Alle und Job-Tabelle.
 * Die Jobs kommen bereits sortiert aus App (aktive zuerst).
 */
export default function LiveView({
  jobs,
  sample,
  samples,
  jetztMs,
  onVorschau,
}: LiveViewProps) {
  const [nurAktiv, setNurAktiv] = useState(true);

  const sichtbar = nurAktiv ? jobs.filter(istAktiv) : jobs;

  return (
    <div className="live-ansicht">
      <section
        className="queue-leiste"
        data-testid="queue-stats"
        aria-label="Warteschlange"
      >
        <span className="queue-kennwert">
          <span className="punkt punkt-running" aria-hidden="true" />
          <strong className="queue-wert">{wertOderStrich(sample?.running)}</strong>{" "}
          laufend
        </span>
        <span className="queue-kennwert">
          <span className="punkt punkt-waiting" aria-hidden="true" />
          <strong className="queue-wert">{wertOderStrich(sample?.waiting)}</strong>{" "}
          wartend
        </span>
        <Sparkline samples={samples} />
      </section>

      <div className="filter-leiste">
        <button
          type="button"
          className="filter-schalter"
          data-testid="filter-active"
          aria-pressed={nurAktiv}
          onClick={() => setNurAktiv((alt) => !alt)}
        >
          {nurAktiv ? "Aktiv" : "Alle"}
        </button>
      </div>

      <JobTable jobs={sichtbar} jetztMs={jetztMs} onVorschau={onVorschau} />
    </div>
  );
}
