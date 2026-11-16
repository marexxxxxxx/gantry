import { useEffect, useState } from "react";
import {
  fetchServerStatus,
  startServer,
  stopServer,
} from "./api";
import type { ServerBackend, ServerState, ServerStatus } from "./api";

/** Deutsche Beschriftung je Zustand. */
const ZUSTAND_TEXT: Record<ServerState, string> = {
  running: "Läuft",
  starting: "Startet …",
  stopping: "Stoppt …",
  stopped: "Gestoppt",
};

/** Erklaerung unter dem Zustand. */
const ZUSTAND_DETAIL: Record<ServerState, string> = {
  running: "Der Modellserver ist erreichbar (Health ok).",
  starting:
    "Gewichte werden geladen oder Health antwortet noch nicht — Qwen braucht 2–4 Minuten, Flint, GPT-OSS und Abliterated wenige Sekunden.",
  stopping: "Der Server fährt herunter; laufende Jobs werden beendet.",
  stopped: "Der Modellserver ist aus. Das Dashboard läuft weiter.",
};

/** Fallback-Beschriftung, falls das Backend (noch) nicht gemeldet wird. */
const BACKEND_TEXT: Record<ServerBackend, string> = {
  qwen: "Qwen (SGLang)",
  flint: "Flint (Ollama)",
  gptoss: "GPT-OSS 20B (Ollama)",
  abl: "Qwen3-14B Abliterated (Ollama)",
  edge0: "Edge0 120B (NVFP4)",
};

/** Bestaetigung faellt ohne Klick nach dieser Zeit zurueck. */
const BESTAETIGUNG_MS = 8000;

/** Abstand der Statusabfrage in Millisekunden. */
const POLL_MS = 3000;

/** Fehlertext fuer die Meldung; nie ein unbehandelter Wurf. */
function fehlerText(fehler: unknown): string {
  return fehler instanceof Error ? fehler.message : "unbekannter Fehler";
}

/**
 * Server-Ansicht: Status des Modellservers und Wahl zwischen Qwen (SGLang),
 * Flint, GPT-OSS 20B und Qwen3-14B Abliterated (alle Ollama).
 *
 * Alle Backends lauschen auf demselben Upstream-Port; ein Wechsel stoppt
 * deshalb das laufende Backend und startet das gewaehlte. Clients und das
 * Gateway bleiben unveraendert. Der Start dauert bei Qwen 2-4 Minuten, die
 * Ansicht pollt deshalb GET /monitor/server.
 */
export default function ServerView() {
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [laedt, setLaedt] = useState(true);
  const [ladefehler, setLadefehler] = useState<string | null>(null);
  const [aktionsfehler, setAktionsfehler] = useState<string | null>(null);
  const [bestaetigung, setBestaetigung] = useState(false);
  const [aktion, setAktion] = useState<"start" | "stop" | null>(null);
  const [auswahl, setAuswahl] = useState<ServerBackend | null>(null);

  useEffect(() => {
    let aktiv = true;
    const laden = () => {
      fetchServerStatus()
        .then((neu) => {
          if (!aktiv) return;
          setStatus(neu);
          setLadefehler(null);
        })
        .catch((grund: unknown) => {
          if (aktiv) {
            setLadefehler(`Status nicht abrufbar: ${fehlerText(grund)}`);
          }
        })
        .finally(() => {
          if (aktiv) {
            setLaedt(false);
          }
        });
    };
    laden();
    const timer = window.setInterval(laden, POLL_MS);
    return () => {
      aktiv = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!bestaetigung) {
      return;
    }
    const timer = window.setTimeout(
      () => setBestaetigung(false),
      BESTAETIGUNG_MS,
    );
    return () => window.clearTimeout(timer);
  }, [bestaetigung]);

  const starten = () => {
    setAktion("start");
    setAktionsfehler(null);
    setBestaetigung(false);
    startServer(gewaehlt)
      .then((neu) => setStatus(neu))
      .catch((grund: unknown) => {
        setAktionsfehler(`Einschalten fehlgeschlagen: ${fehlerText(grund)}`);
      })
      .finally(() => setAktion(null));
  };

  const ausschalten = () => {
    setAktion("stop");
    setAktionsfehler(null);
    setBestaetigung(false);
    stopServer()
      .then((neu) => setStatus(neu))
      .catch((grund: unknown) => {
        setAktionsfehler(`Ausschalten fehlgeschlagen: ${fehlerText(grund)}`);
      })
      .finally(() => setAktion(null));
  };

  const zustand = status?.state ?? null;
  const aktiv = status?.backend ?? null;
  const gewaehlt: ServerBackend = auswahl ?? aktiv ?? "qwen";
  const laeuftGewaehlt = zustand === "running" && aktiv === gewaehlt;
  const beschaeftigt =
    aktion !== null || zustand === "starting" || zustand === "stopping";
  const steuerbar = status?.enabled !== false;
  const label = (name: ServerBackend) =>
    status?.backends.find((eintrag) => eintrag.name === name)?.label
    ?? BACKEND_TEXT[name];
  const aktivesBackend =
    status?.backends.find((eintrag) => eintrag.name === aktiv) ?? null;

  return (
    <section className="server">
      <h2>Server</h2>
      <p className="prioritaet-hinweis">
        Schaltet den Modellserver ein, aus oder wechselt zwischen Qwen
        (SGLang), Flint, GPT-OSS 20B und Qwen3-14B Abliterated (alle
        Ollama). Alle laufen auf demselben Upstream-Port, das Gateway und
        alle Clients bleiben unverändert erreichbar. GPT-OSS läuft mit 64k
        Kontext und den Hindsight-Defaults temperature 0 /
        reasoning_effort low; Abliterated läuft mit 16k Kontext.
      </p>

      {ladefehler !== null && (
        <p className="meldung meldung-fehler" role="alert">
          {ladefehler}
        </p>
      )}
      {aktionsfehler !== null && (
        <p className="meldung meldung-fehler" role="alert">
          {aktionsfehler}
        </p>
      )}

      {laedt && status === null ? (
        <p className="platzhalter">Lade Serverzustand ...</p>
      ) : status === null ? (
        <p className="platzhalter">Serverzustand unbekannt.</p>
      ) : (
        <>
          <div className="server-karte" data-testid="server-status">
            <span
              className={`server-punkt server-punkt-${status.state}`}
              aria-hidden="true"
            />
            <div className="server-karte-text">
              <span
                className="server-zustand"
                data-testid="server-state"
              >
                {aktiv !== null ? `${label(aktiv)} · ` : ""}
                {ZUSTAND_TEXT[status.state]}
              </span>
              <span className="server-detail">
                {ZUSTAND_DETAIL[status.state]}
              </span>
              {aktivesBackend !== null && (
                <span className="server-detail">
                  {aktivesBackend.unit ?? "eigenes Start-/Stop-Skript"}
                  {aktivesBackend.managed_by_systemd
                    ? " (systemd, Autostart aktiv)"
                    : " (ohne systemd-Unit)"}
                </span>
              )}
            </div>
          </div>

          <div
            className="server-wahl"
            role="radiogroup"
            aria-label="Backend"
          >
            {(Object.keys(BACKEND_TEXT) as ServerBackend[]).map((name) => (
              <button
                key={name}
                type="button"
                role="radio"
                aria-checked={gewaehlt === name}
                className={
                  gewaehlt === name
                    ? "server-wahl-knopf server-wahl-knopf-aktiv"
                    : "server-wahl-knopf"
                }
                data-testid={`server-backend-${name}`}
                disabled={beschaeftigt || !steuerbar}
                onClick={() => {
                  setAuswahl(name);
                  setBestaetigung(false);
                }}
              >
                {label(name)}
              </button>
            ))}
          </div>

          {status.error !== null && (
            <p className="meldung meldung-fehler" role="status">
              Letzter Fehler: {status.error}
            </p>
          )}

          {!steuerbar ? (
            <p className="platzhalter" data-testid="server-disabled">
              Die Server-Steuerung ist in dieser Installation deaktiviert.
            </p>
          ) : (
            <div className="server-aktionen">
              {zustand === "stopped" ? (
                <button
                  type="button"
                  className="server-einschalten"
                  data-testid="server-start"
                  disabled={beschaeftigt}
                  onClick={starten}
                >
                  {aktion === "start"
                    ? "Starte ..."
                    : `${label(gewaehlt)} einschalten`}
                </button>
              ) : laeuftGewaehlt || aktiv === null ? (
                <button
                  type="button"
                  className={
                    bestaetigung
                      ? "notaus notaus-scharf"
                      : "notaus server-ausschalten"
                  }
                  data-testid="server-stop"
                  disabled={beschaeftigt}
                  onClick={() => setBestaetigung(true)}
                >
                  {zustand === "stopping"
                    ? "Stoppt ..."
                    : "Server ausschalten"}
                </button>
              ) : (
                <button
                  type="button"
                  className={
                    bestaetigung
                      ? "notaus notaus-scharf"
                      : "server-einschalten"
                  }
                  data-testid="server-switch"
                  disabled={beschaeftigt}
                  onClick={() => setBestaetigung(true)}
                >
                  {aktion === "start"
                    ? "Wechsle ..."
                    : `Auf ${label(gewaehlt)} wechseln`}
                </button>
              )}

              {bestaetigung && zustand !== "stopped" && (
                <button
                  type="button"
                  className="notaus notaus-bestaetigen"
                  data-testid="server-confirm-stop"
                  onClick={laeuftGewaehlt || aktiv === null
                    ? ausschalten
                    : starten}
                >
                  {laeuftGewaehlt || aktiv === null
                    ? "Wirklich ausschalten"
                    : `Wirklich wechseln`}
                </button>
              )}
            </div>
          )}

          {bestaetigung && zustand !== "stopped" && (
            <p className="meldung meldung-fehler" role="alert">
              Laufende Jobs werden dabei abgebrochen.
            </p>
          )}

          <h3>Ohne Dashboard</h3>
          <p className="prioritaet-hinweis">
            Derselbe Weg im Terminal: <code>bash server.sh switch gptoss</code>,
            <code>bash server.sh switch abl</code>,
            <code>bash server.sh switch edge0</code>,
            <code>bash server.sh switch flint</code>,
            <code>bash server.sh start qwen</code>,
            <code>bash server.sh stop</code> oder
            <code>bash server.sh status</code> im Repo-Verzeichnis.
          </p>
        </>
      )}
    </section>
  );
}
