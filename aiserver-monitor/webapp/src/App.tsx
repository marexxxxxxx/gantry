import { useEffect, useState } from "react";
import { cancelAll, connectEvents } from "./api";
import type { Job, Sample, VerbindungsStatus } from "./api";

/** Serverspalte der Kopfleiste als deutsche Beschriftung. */
const STATUS_TEXT: Record<VerbindungsStatus, string> = {
  verbinde: "verbinde ...",
  verbunden: "verbunden",
  getrennt: "getrennt",
};

type Tab = "live" | "stats";

/** Echter Wert (nicht null/undefined), damit die UI "-" fuer Ungemessenes zeigt. */
function vorhanden(wert: number | null | undefined): wert is number {
  return wert !== null && wert !== undefined;
}

/** Megabyte als Gigabyte mit einer Nachkommastelle und deutschem Komma. */
function gb(mb: number): string {
  return (mb / 1024).toFixed(1).replace(".", ",");
}

/** Kompaktanzeige der GPU aus dem letzten Sample, z. B. "GPU 42 % - 12,3 / 24,0 GB". */
function gpuAnzeige(sample: Sample | null): string {
  if (sample === null) {
    return "-";
  }
  const teile: string[] = [];
  if (vorhanden(sample.gpu_util)) {
    teile.push(`GPU ${Math.round(sample.gpu_util)} %`);
  }
  if (vorhanden(sample.vram_used_mb) && vorhanden(sample.vram_total_mb)) {
    teile.push(`${gb(sample.vram_used_mb)} / ${gb(sample.vram_total_mb)} GB`);
  }
  return teile.length > 0 ? teile.join(" - ") : "-";
}

/** Geruest: Kopfleiste (Status, Modell, GPU, Tabs, Notaus) und leerer Inhalt. */
export default function App() {
  const [status, setStatus] = useState<VerbindungsStatus>("verbinde");
  const [modell, setModell] = useState<string | null>(null);
  const [sample, setSample] = useState<Sample | null>(null);
  const [tab, setTab] = useState<Tab>("live");

  useEffect(() => {
    const verbindung = connectEvents((ereignis, daten) => {
      if (ereignis === "snapshot") {
        const inhalt = daten as { jobs: Job[]; stats: Sample | null };
        // "zuletzt gesehen": der zuletzt angelegte Job aus dem Snapshot.
        const neuester = inhalt.jobs.reduce<Job | null>(
          (bisher, job) =>
            bisher === null || job.created_at_ms > bisher.created_at_ms
              ? job
              : bisher,
          null,
        );
        if (neuester !== null) {
          setModell(neuester.model);
        }
        setSample(inhalt.stats);
      } else if (ereignis === "job") {
        setModell((daten as Job).model);
      } else if (ereignis === "stats") {
        setSample(daten as Sample);
      }
    }, setStatus);
    return () => verbindung.close();
  }, []);

  const notaus = () => {
    // Bestaetigung (confirm-cancel-all) und Rueckmeldung folgen mit Task 13;
    // hier nur der Aufruf, Fehler verwirft die Geruest-Oberflaeche still.
    void cancelAll().catch(() => undefined);
  };

  return (
    <div className="app">
      <header className="kopfleiste">
        <span className="marke">aiserver-monitor</span>
        <span className="kennwert">
          <span className="kennwert-label">Server</span>
          <span className="server-status" data-testid="conn-status">
            <span className={`punkt punkt-${status}`} aria-hidden="true" />
            {STATUS_TEXT[status]}
          </span>
        </span>
        <span className="kennwert">
          <span className="kennwert-label">Modell</span>
          <span className="kennwert-wert">{modell ?? "-"}</span>
        </span>
        <span className="kennwert">
          <span className="kennwert-wert">{gpuAnzeige(sample)}</span>
        </span>
        <span className="fuelle" />
        <nav className="tabs" aria-label="Ansichten">
          <button
            type="button"
            className={tab === "live" ? "tab tab-aktiv" : "tab"}
            aria-pressed={tab === "live"}
            data-testid="tab-live"
            onClick={() => setTab("live")}
          >
            Live
          </button>
          <button
            type="button"
            className={tab === "stats" ? "tab tab-aktiv" : "tab"}
            aria-pressed={tab === "stats"}
            data-testid="tab-stats"
            onClick={() => setTab("stats")}
          >
            Statistik
          </button>
        </nav>
        <button
          type="button"
          className="notaus"
          data-testid="cancel-all"
          onClick={notaus}
        >
          Notaus
        </button>
      </header>
      <main className="inhalt">
        <p className="platzhalter">
          Der Inhalt folgt: Live-Ansicht und Statistik sind noch nicht eingebaut.
        </p>
      </main>
    </div>
  );
}
