import { useCallback, useEffect, useMemo, useState } from "react";
import { cancelAll, connectEvents } from "./api";
import type { Job, Sample, VerbindungsStatus } from "./api";
import LiveView from "./LiveView";
import PreviewModal from "./PreviewModal";
import PriorityView from "./PriorityView";
import StatsView from "./StatsView";

/** Serverspalte der Kopfleiste als deutsche Beschriftung. */
const STATUS_TEXT: Record<VerbindungsStatus, string> = {
  verbinde: "verbinde ...",
  verbunden: "verbunden",
  getrennt: "getrennt",
};

/** Ohne Bestaetigung faellt der Notaus-Schalter nach dieser Zeit zurueck. */
const NOTAUS_FENSTER_MS = 5000;

/** So viele Stats-Samples haelt der Ringpuffer fuer die Sparkline vor. */
const RINGLAENGE = 30;

type Tab = "live" | "stats" | "priority";

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

/** waiting/running gelten als aktiv (wie ACTIVE_STATUSES im Backend). */
function istAktiv(job: Job): boolean {
  return job.status === "waiting" || job.status === "running";
}

/**
 * Banner, solange der letzte Messwert SGLang als nicht erreichbar meldet
 * (``available === false``). Ohne Sample (noch nie gemessen) bleibt es aus.
 * Eigene Komponente, damit sie sich isoliert rendern laesst.
 */
export function OfflineHinweis({ sample }: { sample: Sample | null }) {
  if (sample === null || sample.available) {
    return null;
  }
  return (
    <div
      className="offline-hinweis"
      role="status"
      data-testid="offline-hinweis"
    >
      SGLang ist nicht erreichbar — die Messwerte fehlen.
    </div>
  );
}

/** Laufende zuerst, dann Wartende (aelteste oben), dann Fertige (neueste oben). */
function sortiereJobs(jobs: Job[]): Job[] {
  const rang = (job: Job): number => {
    if (job.status === "running") return 0;
    if (job.status === "waiting") return 1;
    return 2;
  };
  return [...jobs].sort((a, b) => {
    const rangA = rang(a);
    const rangB = rang(b);
    if (rangA !== rangB) {
      return rangA - rangB;
    }
    if (rangA === 2) {
      return b.created_at_ms - a.created_at_ms;
    }
    return a.created_at_ms - b.created_at_ms;
  });
}

/** Upsert per rid: vorhandenen Eintrag voll ersetzen, sonst anhaengen. */
function setzeJob(jobs: Job[], neu: Job): Job[] {
  if (!jobs.some((job) => job.rid === neu.rid)) {
    return [...jobs, neu];
  }
  return jobs.map((job) => (job.rid === neu.rid ? neu : job));
}

/** Fehlertext fuer kurze Meldungen; nie ein unbehandelter Wurf. */
function fehlerText(fehler: unknown): string {
  return fehler instanceof Error ? fehler.message : "unbekannter Fehler";
}

/**
 * Zentraler Zustand (nur React-Hooks): Jobs, letztes Stats-Sample,
 * Ringpuffer der letzten 30 Stats-Samples (Sparkline) und Verbindungsstatus.
 * "snapshot" ersetzt die Job-Liste vollstaendig, "job" upsertet per rid.
 */
export default function App() {
  const [status, setStatus] = useState<VerbindungsStatus>("verbinde");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [letztesSample, setLetztesSample] = useState<Sample | null>(null);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [tab, setTab] = useState<Tab>("live");
  const [modalRid, setModalRid] = useState<string | null>(null);
  const [notausScharf, setNotausScharf] = useState(false);
  const [notausMeldung, setNotausMeldung] = useState<string | null>(null);
  const [jetztMs, setJetztMs] = useState(() => Date.now());

  useEffect(() => {
    const verbindung = connectEvents((ereignis, daten) => {
      if (ereignis === "snapshot") {
        const inhalt = daten as { jobs: Job[]; stats: Sample | null };
        setJobs(inhalt.jobs);
        const stats = inhalt.stats;
        if (stats !== null) {
          // Snapshot-Sample anhaengen: die Sparkline zeigt die zuletzt
          // empfangenen Samples, auch ueber einen Reconnect hinweg.
          setLetztesSample(stats);
          setSamples((alt) => [...alt, stats].slice(-RINGLAENGE));
        }
      } else if (ereignis === "job") {
        const job = daten as Job;
        setJobs((alt) => setzeJob(alt, job));
      } else if (ereignis === "stats") {
        const stats = daten as Sample;
        setLetztesSample(stats);
        setSamples((alt) => [...alt, stats].slice(-RINGLAENGE));
      }
    }, setStatus);
    return () => verbindung.close();
  }, []);

  // Notaus: ohne Bestaetigung faellt der scharfe Schalter nach 5 s zurueck.
  useEffect(() => {
    if (!notausScharf) {
      return;
    }
    const timer = window.setTimeout(() => setNotausScharf(false), NOTAUS_FENSTER_MS);
    return () => window.clearTimeout(timer);
  }, [notausScharf]);

  const sortiert = useMemo(() => sortiereJobs(jobs), [jobs]);

  // Modell des zuletzt angelegten Jobs, sonst "-".
  const modell = useMemo(() => {
    let neuester: Job | null = null;
    for (const job of jobs) {
      if (neuester === null || job.created_at_ms > neuester.created_at_ms) {
        neuester = job;
      }
    }
    return neuester === null ? null : neuester.model;
  }, [jobs]);

  // Modal folgt dem Job per rid; fehlt er (z. B. nach neuem Snapshot), bleibt es zu.
  const modalJob = jobs.find((job) => job.rid === modalRid) ?? null;

  // Stabile Callbacks: das Modal muss seinen Escape-Listener nicht je Render neu binden.
  const vorschauOeffnen = useCallback((rid: string) => setModalRid(rid), []);
  const vorschauSchliessen = useCallback(() => setModalRid(null), []);

  // Laufzeit tickt, solange mindestens ein aktiver Job oder das Modal offen ist.
  const tickerAktiv = modalJob !== null || jobs.some(istAktiv);
  useEffect(() => {
    if (!tickerAktiv) {
      return;
    }
    setJetztMs(Date.now());
    const timer = window.setInterval(() => setJetztMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [tickerAktiv]);

  const notausArmen = () => {
    setNotausMeldung(null);
    setNotausScharf(true);
  };

  const notausAusfuehren = () => {
    // Auto-Reset nach Ausfuehrung: zurueck in den Normalzustand.
    setNotausScharf(false);
    setNotausMeldung(null);
    cancelAll()
      .then((antwort) => {
        if (!antwort.ok) {
          setNotausMeldung("Notaus: der Server meldet ok=false");
        }
      })
      .catch((fehler: unknown) => {
        setNotausMeldung(`Notaus fehlgeschlagen: ${fehlerText(fehler)}`);
      });
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
          <span className="kennwert-wert">{gpuAnzeige(letztesSample)}</span>
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
          <button
            type="button"
            className={tab === "priority" ? "tab tab-aktiv" : "tab"}
            aria-pressed={tab === "priority"}
            data-testid="tab-priority"
            onClick={() => setTab("priority")}
          >
            Priorität
          </button>
        </nav>
        <span className="notaus-bereich">
          <button
            type="button"
            className={notausScharf ? "notaus notaus-scharf" : "notaus"}
            data-testid="cancel-all"
            onClick={notausArmen}
          >
            Notaus
          </button>
          {notausScharf && (
            <button
              type="button"
              className="notaus notaus-bestaetigen"
              data-testid="confirm-cancel-all"
              onClick={notausAusfuehren}
            >
              Alles stoppen
            </button>
          )}
        </span>
        {notausMeldung !== null && (
          <span className="meldung meldung-fehler" role="alert">
            {notausMeldung}
          </span>
        )}
      </header>
      <OfflineHinweis sample={letztesSample} />
      <main className="inhalt">
        {tab === "live" ? (
          <LiveView
            jobs={sortiert}
            sample={letztesSample}
            samples={samples}
            jetztMs={jetztMs}
            onVorschau={vorschauOeffnen}
          />
        ) : tab === "stats" ? (
          <StatsView />
        ) : (
          <PriorityView />
        )}
      </main>
      {modalJob !== null && (
        <PreviewModal job={modalJob} onSchliessen={vorschauSchliessen} />
      )}
    </div>
  );
}
