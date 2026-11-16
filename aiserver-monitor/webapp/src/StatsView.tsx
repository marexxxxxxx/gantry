import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { fetchStats } from "./api";
import type {
  StatsBucket,
  StatsError,
  StatsModel,
  StatsSource,
  StatsWindow,
} from "./api";
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

/** Festkommazahl mit deutschem Komma; null heisst "nicht gemessen". */
function komma(wert: number | null, stellen = 1): string {
  if (wert === null) return "-";
  return wert.toFixed(stellen).replace(".", ",");
}

/** Ganzzahl mit Tausenderpunkten; null heisst "nicht gemessen". */
function zahl(wert: number | null): string {
  return wert === null ? "-" : Math.round(wert).toLocaleString("de-DE");
}

/** Anteil als Prozentwert; ein leerer Nenner ist "nicht messbar". */
function prozent(teil: number, ganz: number): string {
  return ganz > 0 ? `${komma((teil / ganz) * 100)} %` : "-";
}

/** Serverwert in Sekunden lesbar: ms unter 1 s, sonst s bzw. min. */
function millisekunden(wert: number | null): string {
  if (wert === null) return "-";
  if (wert >= 60) return `${komma(wert / 60)} min`;
  if (wert >= 1) return `${komma(wert)} s`;
  return `${Math.round(wert * 1000)} ms`;
}

/** Serverwert in Sekunden mit einer Nachkommastelle; ab 60 s in Minuten. */
function sekunden(wert: number | null): string {
  if (wert === null) return "-";
  if (wert >= 60) return `${komma(wert / 60)} min`;
  return `${komma(wert)} s`;
}

/** Dekodierrate; null bleibt "-", nie 0. */
function tokRate(wert: number | null): string {
  return wert === null ? "-" : `${komma(wert)} tok/s`;
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

/** Groesster gemessener Wert eines Bucket-Feldes; null wenn keiner existiert. */
function maximum(
  buckets: StatsBucket[],
  feld: (bucket: StatsBucket) => number | null,
): number | null {
  let ergebnis: number | null = null;
  for (const bucket of buckets) {
    const wert = feld(bucket);
    if (wert !== null && (ergebnis === null || wert > ergebnis)) {
      ergebnis = wert;
    }
  }
  return ergebnis;
}

/** Mittel der gemessenen Werte eines Bucket-Feldes; null wenn keiner existiert. */
function mittelwert(
  buckets: StatsBucket[],
  feld: (bucket: StatsBucket) => number | null,
): number | null {
  let summe = 0;
  let anzahl = 0;
  for (const bucket of buckets) {
    const wert = feld(bucket);
    if (wert !== null) {
      summe += wert;
      anzahl += 1;
    }
  }
  return anzahl > 0 ? summe / anzahl : null;
}

/** VRAM-Auslastung eines Buckets in Prozent; ohne Gesamtgroesse nicht messbar. */
function vramProzent(bucket: StatsBucket): number | null {
  if (
    bucket.vram_used_mb === null ||
    bucket.vram_total_mb === null ||
    bucket.vram_total_mb <= 0
  ) {
    return null;
  }
  return (bucket.vram_used_mb / bucket.vram_total_mb) * 100;
}

/** Megabyte als Gigabyte mit einer Nachkommastelle. */
function gigabyte(wert: number | null): string {
  return wert === null ? "-" : `${komma(wert / 1024)} GB`;
}

/** Ein automatisch formulierter Hinweis fuer das Lagebild. */
interface Hinweis {
  ton: "warn" | "info";
  text: string;
}

/**
 * Leitet aus den aggregierten Daten kurze, pruefbare Aussagen ab - keine
 * Schaetzung, sondern nur Saetze, die direkt aus Kennzahlen folgen.
 */
function auffaelligkeiten(daten: StatsWindow): Hinweis[] {
  const { overview: uebersicht, buckets } = daten;
  const hinweise: Hinweis[] = [];

  if (uebersicht.count > 0 && uebersicht.error > 0) {
    const quote = (uebersicht.error / uebersicht.count) * 100;
    if (quote >= 5) {
      const top = daten.errors[0];
      hinweise.push({
        ton: "warn",
        text: `Fehlerquote ${komma(quote)} % - ${uebersicht.error} von ${
          uebersicht.count
        } Jobs${top ? `, häufigste Ursache: ${top.kind} (${top.count}x)` : ""}.`,
      });
    }
  }

  const gpuSpitze = maximum(buckets, (bucket) => bucket.gpu_util);
  const vramSpitze = maximum(buckets, vramProzent);
  const wartendSpitze = maximum(buckets, (bucket) => bucket.waiting);
  if (
    wartendSpitze !== null &&
    wartendSpitze > 0 &&
    ((gpuSpitze !== null && gpuSpitze >= 85) ||
      (vramSpitze !== null && vramSpitze >= 90))
  ) {
    const last = [
      gpuSpitze === null ? null : `GPU ${zahl(gpuSpitze)} %`,
      vramSpitze === null ? null : `VRAM ${zahl(vramSpitze)} %`,
    ].filter((teil): teil is string => teil !== null);
    hinweise.push({
      ton: "warn",
      text: `Sättigung: bis zu ${zahl(
        wartendSpitze,
      )} wartend; Spitzen ${last.join(" / ")}.`,
    });
  }

  const { p50, p95 } = uebersicht.ttft;
  if (p50 !== null && p95 !== null && p50 > 0 && p95 >= p50 * 2) {
    hinweise.push({
      ton: "info",
      text: `TTFT streut: p95 (${sekunden(
        p95,
      )}) liegt beim ${komma(p95 / p50)}-Fachen des p50 (${sekunden(p50)}).`,
    });
  }

  if (buckets.length > 0) {
    const verfuegbar =
      buckets.reduce((summe, bucket) => summe + bucket.available, 0) /
      buckets.length;
    if (verfuegbar < 0.5) {
      hinweise.push({
        ton: "warn",
        text: `Upstream war nur in ${komma(
          verfuegbar * 100,
          0,
        )} % der Messungen erreichbar.`,
      });
    }
  }

  if (uebersicht.tok_per_s.avg !== null && uebersicht.done > 0) {
    hinweise.push({
      ton: "info",
      text: `Durchschnitt ${tokRate(
        uebersicht.tok_per_s.avg,
      )} über ${uebersicht.done} fertige Jobs, Spitze ${tokRate(
        uebersicht.tok_per_s.max,
      )}.`,
    });
  }

  if (hinweise.length === 0) {
    hinweise.push({
      ton: "info",
      text:
        uebersicht.count > 0
          ? `Keine Auffälligkeiten: ${uebersicht.done} von ${uebersicht.count} Jobs fertig, ${uebersicht.error} Fehler.`
          : "Keine Jobs im Zeitfenster; es werden nur Messwerte angezeigt.",
    });
  }
  return hinweise.slice(0, 4);
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

/** Kompakte Tabelle fuer die Aufschluesselung nach Modell/Quelle/Fehler. */
function Tabelle({
  testid,
  titel,
  kopf,
  zeilen,
}: {
  testid: string;
  titel: string;
  kopf: string[];
  zeilen: ReactNode[][];
}) {
  return (
    <section className="stat-gruppe" data-testid={testid}>
      <h3 className="stat-gruppe-titel">{titel}</h3>
      <div className="stat-tabelle-rahmen">
        <table className="stat-tabelle">
          <thead>
            <tr>
              {kopf.map((spalte) => (
                <th key={spalte}>{spalte}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {zeilen.map((zeile, index) => (
              <tr key={index}>
                {zeile.map((zelle, spalte) => (
                  <td key={spalte}>{zelle}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** Zeilen fuer die Modell- bzw. Quellen-Tabelle (Top 10). */
function aggregatZeilen(
  eintraege: (StatsModel | StatsSource)[],
  name: (eintrag: StatsModel | StatsSource) => string,
): ReactNode[][] {
  return eintraege.slice(0, 10).map((eintrag) => [
    name(eintrag) || "-",
    zahl(eintrag.jobs),
    zahl(eintrag.done),
    zahl(eintrag.error),
    sekunden(eintrag.avg_ttft),
    tokRate(eintrag.avg_tok_per_s),
    zahl(eintrag.tokens_out),
  ]);
}

/** Zeilen fuer die Fehlerursachen-Tabelle inklusive Anteil. */
function fehlerZeilen(fehler: StatsError[]): ReactNode[][] {
  const gesamt = fehler.reduce((summe, eintrag) => summe + eintrag.count, 0);
  return fehler.map((eintrag) => [
    eintrag.kind || "Unbekannt",
    zahl(eintrag.count),
    prozent(eintrag.count, gesamt),
  ]);
}

/**
 * Statistik-Ansicht: Zeitfenster-Chips, Lagebild, Kennwerte, sechs
 * Verlaufs-Charts und Aufschluesselungen nach Modell, Quelle und
 * Fehlerursache. Laedt beim Mounten und bei jedem Fensterwechsel genau ein
 * Fenster (to = Date.now()); kein Polling, kein Refresh - der Tabwechsel baut
 * die Ansicht neu auf. Die Zeitreihen sind memoisiert, damit die Charts bei
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
  const jobSeries = useMemo<Zeitreihe[]>(
    () => [
      { label: "fertig", points: punkte(buckets, (bucket) => bucket.done) },
      { label: "Fehler", points: punkte(buckets, (bucket) => bucket.error) },
      {
        label: "abgebrochen",
        points: punkte(buckets, (bucket) => bucket.aborted),
      },
    ],
    [buckets],
  );
  const latenzSeries = useMemo<Zeitreihe[]>(
    () => [
      { label: "TTFT p50", points: punkte(buckets, (b) => b.ttft_p50) },
      { label: "Dauer p50", points: punkte(buckets, (b) => b.duration_p50) },
    ],
    [buckets],
  );
  const durchsatzSeries = useMemo(
    () =>
      reihe(
        buckets,
        "tok/s",
        (bucket) => bucket.throughput ?? bucket.tok_per_s,
      ),
    [buckets],
  );
  const tokenSeries = useMemo<Zeitreihe[]>(
    () => [
      { label: "Eingabe", points: punkte(buckets, (b) => b.tokens_in) },
      { label: "Ausgabe", points: punkte(buckets, (b) => b.tokens_out) },
    ],
    [buckets],
  );
  const gpuSeries = useMemo<Zeitreihe[]>(
    () => [
      { label: "GPU", points: punkte(buckets, (bucket) => bucket.gpu_util) },
      { label: "VRAM", points: punkte(buckets, vramProzent) },
    ],
    [buckets],
  );

  const hinweise = daten === null ? [] : auffaelligkeiten(daten);

  const gpuMittel = mittelwert(buckets, (bucket) => bucket.gpu_util);
  const gpuSpitze = maximum(buckets, (bucket) => bucket.gpu_util);
  const vramSpitze = maximum(buckets, vramProzent);
  const vramBelegt = maximum(buckets, (bucket) => bucket.vram_used_mb);
  const vramGesamt = maximum(buckets, (bucket) => bucket.vram_total_mb);
  const laufendMittel = mittelwert(buckets, (bucket) => bucket.running);
  const wartendSpitze = maximum(buckets, (bucket) => bucket.waiting);

  const leer =
    daten !== null &&
    daten.overview.count === 0 &&
    daten.buckets.length === 0;

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

      {leer ? (
        <p className="stat-leer" data-testid="stat-leer">
          keine Daten im Zeitfenster
        </p>
      ) : (
        daten !== null && (
          <>
            <section
              className="stat-auffaelligkeiten"
              data-testid="stat-insights"
              aria-label="Auffälligkeiten"
            >
              <h2 className="stat-abschnitt-titel">Auffälligkeiten</h2>
              <ul className="stat-hinweis-liste">
                {hinweise.map((hinweis, index) => (
                  <li
                    className={`stat-hinweis stat-hinweis-${hinweis.ton}`}
                    data-testid="stat-insight"
                    key={index}
                  >
                    {hinweis.text}
                  </li>
                ))}
              </ul>
            </section>

            <div className="kennwert-raster">
              <Karte
                testid="kpi-count"
                label="Jobs"
                wert={zahl(daten.overview.count)}
                zusatz={`${daten.overview.done} fertig · ${daten.overview.aborted} abgebrochen`}
              />
              <Karte
                testid="kpi-success"
                label="Erfolgsquote"
                wert={prozent(daten.overview.done, daten.overview.count)}
                zusatz={`${daten.overview.error} Fehler`}
              />
              <Karte
                testid="kpi-error"
                label="Fehler"
                wert={zahl(daten.overview.error)}
                zusatz={`Quote: ${prozent(
                  daten.overview.error,
                  daten.overview.count,
                )}`}
              />
              <Karte
                testid="kpi-ttft"
                label="TTFT p50"
                wert={millisekunden(daten.overview.ttft.p50)}
                zusatz={`p95: ${millisekunden(
                  daten.overview.ttft.p95,
                )} · p99: ${millisekunden(daten.overview.ttft.p99)}`}
              />
              <Karte
                testid="kpi-duration"
                label="Dauer p50"
                wert={sekunden(daten.overview.duration.p50)}
                zusatz={`p95: ${sekunden(daten.overview.duration.p95)}`}
              />
              <Karte
                testid="kpi-tokens"
                label="Token"
                wert={`in ${zahl(daten.overview.tokens_in)} / out ${zahl(
                  daten.overview.tokens_out,
                )}`}
                zusatz={
                  daten.overview.tokens_in !== null &&
                  daten.overview.tokens_out !== null
                    ? `gesamt: ${zahl(
                        daten.overview.tokens_in + daten.overview.tokens_out,
                      )}`
                    : undefined
                }
              />
              <Karte
                testid="kpi-throughput"
                label="Durchsatz"
                wert={
                  daten.overview.tok_per_s.avg === null
                    ? "-"
                    : `Ø ${tokRate(daten.overview.tok_per_s.avg)}`
                }
                zusatz={`Spitze ${tokRate(daten.overview.tok_per_s.max)}`}
              />
              <Karte
                testid="kpi-gpu"
                label="GPU / VRAM"
                wert={gpuMittel === null ? "-" : `Ø ${zahl(gpuMittel)} %`}
                zusatz={`Spitze ${zahl(gpuSpitze)} % · VRAM ${zahl(
                  vramSpitze,
                )} % (${gigabyte(vramBelegt)} / ${gigabyte(vramGesamt)})`}
              />
              <Karte
                testid="kpi-queue"
                label="Warteschlange"
                wert={`Ø ${komma(laufendMittel)} laufend`}
                zusatz={`Spitze ${zahl(wartendSpitze)} wartend`}
              />
            </div>

            <div className="stat-diagramme">
              <div className="stat-diagramm" data-testid="chart-queue">
                <TimeChart
                  title="Warteschlange"
                  unit="Jobs"
                  series={queueSeries}
                />
              </div>
              <div className="stat-diagramm" data-testid="chart-jobs">
                <TimeChart
                  title="Jobs nach Status"
                  unit="Jobs"
                  series={jobSeries}
                />
              </div>
              <div className="stat-diagramm" data-testid="chart-latency">
                <TimeChart title="Latenz" unit="s" series={latenzSeries} />
              </div>
              <div className="stat-diagramm" data-testid="chart-throughput">
                <TimeChart
                  title="Durchsatz"
                  unit="tok/s"
                  series={durchsatzSeries}
                />
              </div>
              <div className="stat-diagramm" data-testid="chart-tokens">
                <TimeChart title="Token" unit="Token" series={tokenSeries} />
              </div>
              <div className="stat-diagramm" data-testid="chart-gpu">
                <TimeChart
                  title="GPU und VRAM"
                  unit="%"
                  series={gpuSeries}
                />
              </div>
            </div>

            <div className="stat-gruppen">
              <Tabelle
                testid="stat-models"
                titel="Modelle"
                kopf={[
                  "Modell",
                  "Jobs",
                  "Fertig",
                  "Fehler",
                  "Ø TTFT",
                  "Ø Rate",
                  "Tokens out",
                ]}
                zeilen={aggregatZeilen(daten.models, (eintrag) =>
                  "model" in eintrag ? eintrag.model : "",
                )}
              />
              <Tabelle
                testid="stat-sources"
                titel="Quellen"
                kopf={[
                  "Quelle",
                  "Jobs",
                  "Fertig",
                  "Fehler",
                  "Ø TTFT",
                  "Ø Rate",
                  "Tokens out",
                ]}
                zeilen={aggregatZeilen(daten.sources, (eintrag) =>
                  "source" in eintrag ? eintrag.source : "",
                )}
              />
              <Tabelle
                testid="stat-errors"
                titel="Fehlerursachen"
                kopf={["Ursache", "Anzahl", "Anteil"]}
                zeilen={
                  daten.errors.length > 0
                    ? fehlerZeilen(daten.errors)
                    : [["keine Fehler", "0", "-"]]
                }
              />
            </div>
          </>
        )
      )}
    </div>
  );
}
