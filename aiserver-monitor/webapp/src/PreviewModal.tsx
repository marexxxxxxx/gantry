import { useEffect, useRef } from "react";
import type { Job, JobStatus } from "./api";

export interface PreviewModalProps {
  job: Job;
  onSchliessen: () => void;
}

/** Deutsche Statustexte (wie in der Job-Tabelle). */
const STATUS_TEXT: Record<JobStatus, string> = {
  waiting: "wartend",
  running: "läuft",
  done: "fertig",
  aborted: "abgebrochen",
  error: "fehler",
};

/** Text oder "-" fuer leere Vorschauen. */
function textOderStrich(text: string): string {
  return text === "" ? "-" : text;
}

/**
 * Vorschau-Fenster zum angeklickten Job: voller Prompt, eingeklapptes
 * "Denken" (<details>) und mitlaufende Antwort mit Auto-Scroll. Das Modal
 * folgt dem Job per rid und zeigt damit Live-Updates aus den SSE-Events.
 * Schliessen: X-Button, Escape oder Klick auf den Hintergrund.
 */
export default function PreviewModal({ job, onSchliessen }: PreviewModalProps) {
  const antwort = useRef<HTMLPreElement>(null);

  // Auto-Scroll: bei jedem Update der Antwort ans Ende springen.
  useEffect(() => {
    const bereich = antwort.current;
    if (bereich !== null) {
      bereich.scrollTop = bereich.scrollHeight;
    }
  }, [job.content_preview]);

  // Escape schliesst das Fenster.
  useEffect(() => {
    const aufTaste = (ereignis: KeyboardEvent) => {
      if (ereignis.key === "Escape") {
        onSchliessen();
      }
    };
    window.addEventListener("keydown", aufTaste);
    return () => window.removeEventListener("keydown", aufTaste);
  }, [onSchliessen]);

  return (
    <div className="modal-hintergrund" onClick={onSchliessen}>
      <div
        className="modal"
        data-testid="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Job-Vorschau"
        onClick={(ereignis) => ereignis.stopPropagation()}
      >
        <header className="modal-kopf">
          <span className="modal-titel">Vorschau</span>
          <span className="modal-status">
            <span className={`punkt punkt-${job.status}`} aria-hidden="true" />
            {STATUS_TEXT[job.status]}
          </span>
          <span className="modal-rid">{job.rid}</span>
          <button
            type="button"
            className="modal-schliessen"
            title="Schließen"
            aria-label="Schließen"
            onClick={onSchliessen}
          >
            ✕
          </button>
        </header>

        <section className="modal-abschnitt">
          <h3 className="modal-ueberschrift">Prompt</h3>
          <pre className="modal-prompt" data-testid="modal-prompt">
            {textOderStrich(job.full_prompt)}
          </pre>
        </section>

        <details className="modal-denken" data-testid="modal-thinking">
          <summary className="modal-denken-schalter">Denken</summary>
          <pre className="modal-denken-text">
            {textOderStrich(job.reasoning_preview)}
          </pre>
        </details>

        <section className="modal-abschnitt">
          <h3 className="modal-ueberschrift">Antwort</h3>
          <pre className="modal-antwort" data-testid="modal-answer" ref={antwort}>
            {textOderStrich(job.content_preview)}
          </pre>
        </section>
      </div>
    </div>
  );
}
