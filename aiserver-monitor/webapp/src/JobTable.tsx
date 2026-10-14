import { useState } from "react";
import { cancelJob } from "./api";
import type { Job, JobStatus } from "./api";

export interface JobTableProps {
  jobs: Job[];
  jetztMs: number;
  onVorschau: (rid: string) => void;
}

/** Deutsche Statustexte zum Backend-Status (job_to_dict). */
const STATUS_TEXT: Record<JobStatus, string> = {
  waiting: "wartend",
  running: "läuft",
  done: "fertig",
  aborted: "abgebrochen",
  error: "fehler",
};

/** Zeit aus Millisekunden: unter einer Minute "42 s", sonst "01:23". */
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

/**
 * Laufzeit: aktive Jobs ticken mit jetztMs (Der 1-s-Ticker laeuft, solange ein
 * aktiver Job oder das Modal offen ist), fertige Jobs zeigen duration_ms,
 * dauerhaft Ungemessenes "-".
 */
function laufzeitText(job: Job, jetztMs: number): string {
  if (job.duration_ms !== null) {
    return dauerText(job.duration_ms);
  }
  if (job.status === "waiting") {
    return `wartet ${dauerText(jetztMs - job.created_at_ms)}`;
  }
  if (job.status === "running") {
    return dauerText(jetztMs - job.created_at_ms);
  }
  return "-";
}

/** Promptanfang oder "-", wenn kein Prompt vorliegt. */
function promptAnfang(job: Job): string {
  return job.prompt_start === "" ? "-" : job.prompt_start;
}

/** Tokens ein/aus; ohne beide Messwerte bleibt nur "-". */
function tokensText(job: Job): string {
  if (job.prompt_tokens === null && job.completion_tokens === null) {
    return "-";
  }
  return `${zahlOderStrich(job.prompt_tokens)} → ${zahlOderStrich(job.completion_tokens)}`;
}

interface ZeileProps {
  job: Job;
  jetztMs: number;
  onVorschau: (rid: string) => void;
}

/** Eine Job-Zeile mit Statusampel, Kennzahlen und den Aktionen Vorschau/Abbruch. */
function JobZeile({ job, jetztMs, onVorschau }: ZeileProps) {
  const [hinweis, setHinweis] = useState<string | null>(null);

  const abbrechen = () => {
    setHinweis(null);
    cancelJob(job.rid)
      .then((antwort) => {
        if (!antwort.ok) {
          setHinweis("Abbruch am Server nicht möglich (ok=false)");
        }
      })
      .catch((fehler: unknown) => {
        setHinweis(
          fehler instanceof Error
            ? `Abbruch fehlgeschlagen: ${fehler.message}`
            : "Abbruch fehlgeschlagen",
        );
      });
  };

  return (
    <tr
      className={`job-zeile job-zeile-${job.status}`}
      data-testid="job-row"
      onClick={() => onVorschau(job.rid)}
    >
      <td>
        <span className="job-status" data-testid="job-status">
          <span className={`punkt punkt-${job.status}`} aria-hidden="true" />
          {STATUS_TEXT[job.status]}
        </span>
      </td>
      <td className="spalte-quelle">{job.source === "" ? "-" : job.source}</td>
      <td>
        <span className="prompt-anfang">{promptAnfang(job)}</span>
      </td>
      <td className="spalte-zahl">{laufzeitText(job, jetztMs)}</td>
      <td className="spalte-zahl">{zahlOderStrich(job.ttft_ms)}</td>
      <td className="spalte-zahl">{tokensText(job)}</td>
      <td className="spalte-zahl">{rateText(job.tok_per_s)}</td>
      <td className="spalte-aktionen">
        <button
          type="button"
          className="aktion"
          data-testid="job-preview"
          title="Vorschau öffnen"
          aria-label="Vorschau öffnen"
          onClick={(ereignis) => {
            ereignis.stopPropagation();
            onVorschau(job.rid);
          }}
        >
          👁
        </button>
        <button
          type="button"
          className="aktion aktion-abbruch"
          data-testid="job-cancel"
          title="Job abbrechen"
          aria-label="Job abbrechen"
          onClick={(ereignis) => {
            // stopPropagation: der Zeilenklick darf nicht das Modal oeffnen.
            ereignis.stopPropagation();
            abbrechen();
          }}
        >
          ✕
        </button>
        {hinweis !== null && (
          <span className="zeilen-hinweis" role="alert">
            {hinweis}
          </span>
        )}
      </td>
    </tr>
  );
}

/**
 * Job-Tabelle. Der testid-Vertrag verlangt je Zeile "job-row" und
 * "job-row-{rid}"; ein Element traegt nur eine data-testid, daher traegt das
 * tbody die rid-spezifische und das tr darin die generische Kennung.
 */
export default function JobTable({ jobs, jetztMs, onVorschau }: JobTableProps) {
  return (
    <div className="tabelle-rahmen">
      <table className="job-tabelle">
        <thead>
          <tr>
            <th>Status</th>
            <th>Quelle</th>
            <th>Promptanfang</th>
            <th>Laufzeit</th>
            <th>TTFT (ms)</th>
            <th>Tokens ein→aus</th>
            <th>tok/s</th>
            <th className="spalte-aktionen">Aktionen</th>
          </tr>
        </thead>
        {jobs.length === 0 ? (
          <tbody>
            <tr>
              <td className="leer-zelle" colSpan={8}>
                Keine Jobs in dieser Ansicht.
              </td>
            </tr>
          </tbody>
        ) : (
          jobs.map((job) => (
            <tbody key={job.rid} data-testid={`job-row-${job.rid}`}>
              <JobZeile job={job} jetztMs={jetztMs} onVorschau={onVorschau} />
            </tbody>
          ))
        )}
      </table>
    </div>
  );
}
