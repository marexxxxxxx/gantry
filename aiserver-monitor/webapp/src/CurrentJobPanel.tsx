import { useEffect, useRef, useState } from "react";
import { cancelJob } from "./api";
import type { Job } from "./api";
import { STATUS_TEXT } from "./JobTable";

export interface CurrentJobPanelProps {
  jobs: Job[];
  jetztMs: number;
}

/** Zeit aus Millisekunden: unter einer Minute "42 s", sonst "01:23" (wie JobTable). */
function dauerText(ms: number): string {
  const sekunden = Math.max(0, Math.floor(ms / 1000));
  if (sekunden < 60) {
    return `${sekunden} s`;
  }
  const minuten = Math.floor(sekunden / 60);
  const rest = sekunden % 60;
  return `${String(minuten).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

/** Ganze Zahl oder "-": null heisst "nicht gemessen", nie 0. */
function zahlOderStrich(wert: number | null): string {
  return wert === null ? "-" : String(Math.round(wert));
}

/** Rate mit einer Nachkommastelle und deutschem Komma, sonst "-". */
function rateText(wert: number | null): string {
  return wert === null ? "-" : wert.toFixed(1).replace(".", ",");
}

/** Text oder "-" fuer leere Live-Bereiche. */
function textOderStrich(text: string): string {
  return text === "" ? "-" : text;
}

/**
 * Aktuell behandelter Job: der laufende Job (bei mehreren der aelteste),
 * sonst der aelteste wartende als Vorschau des Queue-Kopfs, sonst null.
 */
function aktiverJob(jobs: Job[]): Job | null {
  let laufend: Job | null = null;
  let wartend: Job | null = null;
  for (const job of jobs) {
    if (job.status === "running") {
      if (laufend === null || job.created_at_ms < laufend.created_at_ms) {
        laufend = job;
      }
    } else if (job.status === "waiting") {
      if (wartend === null || job.created_at_ms < wartend.created_at_ms) {
        wartend = job;
      }
    }
  }
  return laufend ?? wartend;
}

/** Bearbeitete Zeit: seit dem ersten Token (ttft_ms), sonst seit Job-Start. */
function bearbeitetMs(job: Job, jetztMs: number): number {
  return Math.max(0, jetztMs - job.created_at_ms - (job.ttft_ms ?? 0));
}

/**
 * Live-Dekodierrate des laufenden Jobs: Generierte Tokens geteilt durch die
 * bearbeiteten Sekunden. Nicht berechenbar (kein Messwert, noch keine
 * bearbeitete Zeit) -> null, die Anzeige faellt auf "-" zurueck.
 */
function tokProSekunde(job: Job, jetztMs: number): number | null {
  if (job.status !== "running" || job.completion_tokens === null) {
    return null;
  }
  const ms = bearbeitetMs(job, jetztMs);
  if (ms <= 0) {
    return null;
  }
  return job.completion_tokens / (ms / 1000);
}

/** Auto-Scroll ans Ende eines Live-Bereichs bei jedem Textupdate. */
function useAutoScroll(inhalt: string) {
  const bereich = useRef<HTMLPreElement>(null);
  useEffect(() => {
    const element = bereich.current;
    if (element !== null) {
      element.scrollTop = element.scrollHeight;
    }
  }, [inhalt]);
  return bereich;
}

/**
 * Grosses Panel im Live-Tab, das ausschliesslich den aktuell behandelten Job
 * zeigt: Status, Quelle, Laufzeit, Bearbeitungszeit, ctx/Tokens und die
 * mitlaufenden Bereiche Denken/Antwort. Die Daten kommen aus den
 * bestehenden Job-Props und dem jetztMs-Ticker; nur der Abbrechen-Knopf
 * loest einen Netzwerkaufruf (cancelJob) aus.
 */
export default function CurrentJobPanel({ jobs, jetztMs }: CurrentJobPanelProps) {
  const job = aktiverJob(jobs);
  const [abbrechenLaeuft, setAbbrechenLaeuft] = useState(false);
  const [abbruchFehler, setAbbruchFehler] = useState<string | null>(null);

  const denken = useAutoScroll(job?.reasoning_preview ?? "");
  const antwort = useAutoScroll(job?.content_preview ?? "");

  // Wechselt der angezeigte Job, beginnt der Abbrechen-Knopf im Normalzustand.
  const rid = job?.rid ?? null;
  useEffect(() => {
    setAbbrechenLaeuft(false);
    setAbbruchFehler(null);
  }, [rid]);

  if (job === null) {
    return (
      <section
        className="current-job current-job-leer"
        data-testid="current-job"
        aria-label="Aktueller Job"
      >
        <h2 className="current-titel">Aktueller Job</h2>
        <p className="current-platzhalter">Aktuell wird nichts gerechnet.</p>
      </section>
    );
  }

  const abbrechen = () => {
    setAbbruchFehler(null);
    setAbbrechenLaeuft(true);
    cancelJob(job.rid)
      .then((ergebnis) => {
        if (!ergebnis.ok) {
          setAbbruchFehler("Abbruch am Server nicht möglich (ok=false)");
        }
      })
      .catch((fehler: unknown) => {
        setAbbruchFehler(
          fehler instanceof Error
            ? `Abbruch fehlgeschlagen: ${fehler.message}`
            : "Abbruch fehlgeschlagen",
        );
      })
      .finally(() => setAbbrechenLaeuft(false));
  };

  const gesamt =
    job.prompt_tokens !== null && job.completion_tokens !== null
      ? String(Math.round(job.prompt_tokens + job.completion_tokens))
      : "-";

  return (
    <section
      className={`current-job current-job-${job.status}`}
      data-testid="current-job"
      aria-label="Aktueller Job"
    >
      <header className="current-kopf">
        <h2 className="current-titel">Aktueller Job</h2>
        <span className="current-status" data-testid="current-status">
          <span className={`punkt punkt-${job.status}`} aria-hidden="true" />
          {STATUS_TEXT[job.status]}
        </span>
        <span className="current-quelle">
          Quelle: {job.source === "" ? "-" : job.source}
        </span>
        <span className="current-laufzeit">
          Laufzeit: {dauerText(jetztMs - job.created_at_ms)}
        </span>
      </header>

      {job.status === "running" ? (
        <div className="current-zeiten">
          <span data-testid="current-since">
            wird bearbeitet seit: {dauerText(bearbeitetMs(job, jetztMs))}
          </span>
          {job.ttft_ms !== null && (
            <span className="current-wartete">
              Wartete: {dauerText(job.ttft_ms)}
            </span>
          )}
        </div>
      ) : (
        <div className="current-zeiten">
          <span data-testid="current-since">
            wartet seit: {dauerText(jetztMs - job.created_at_ms)}
          </span>
        </div>
      )}

      <p className="current-ctx" data-testid="current-ctx">
        Prompt: {zahlOderStrich(job.prompt_tokens)} | Generiert:{" "}
        {zahlOderStrich(job.completion_tokens)} | Gesamt: {gesamt} | tok/s:{" "}
        {rateText(tokProSekunde(job, jetztMs))}
      </p>

      <div className="current-bereiche">
        <section className="current-bereich">
          <h3 className="current-bereich-titel">Denken</h3>
          <pre className="current-text" data-testid="current-thinking" ref={denken}>
            {textOderStrich(job.reasoning_preview)}
          </pre>
        </section>
        <section className="current-bereich">
          <h3 className="current-bereich-titel">Antwort</h3>
          <pre className="current-text" data-testid="current-answer" ref={antwort}>
            {textOderStrich(job.content_preview)}
          </pre>
        </section>
      </div>

      <div className="current-aktionen">
        <button
          type="button"
          className="current-abbrechen"
          data-testid="current-cancel"
          title="Job abbrechen"
          aria-label="Job abbrechen"
          disabled={abbrechenLaeuft}
          onClick={abbrechen}
        >
          Abbrechen
        </button>
        {abbruchFehler !== null && (
          <span className="current-hinweis" role="alert">
            {abbruchFehler}
          </span>
        )}
      </div>
    </section>
  );
}
