import { useEffect, useState } from "react";
import { fetchPrioritySettings, updatePrioritySettings } from "./api";

/** Fehlertext fuer die Meldung; nie ein unbehandelter Wurf. */
function fehlerText(fehler: unknown): string {
  return fehler instanceof Error ? fehler.message : "unbekannter Fehler";
}

/**
 * Prioritaets-Ansicht: Schalter fuer die Max-Bevorzugung und die Liste der
 * System-Prompt-Marker. Aenderungen werden erst mit "Speichern" uebernommen
 * (PUT /monitor/priority); der Server persistiert sie in der Monitor-DB.
 *
 * Ein Marker trifft nur `role: system`-Nachrichten, Gross-/Kleinschreibung
 * und Whitespace sind egal. Weitere Marker (z. B. fuer Max' Memory- oder
 * Summary-Hilfsrequests) lassen sich hier ergaenzen.
 */
export default function PriorityView() {
  const [enabled, setEnabled] = useState(true);
  const [markers, setMarkers] = useState<string[]>([]);
  const [neuerMarker, setNeuerMarker] = useState("");
  const [laedt, setLaedt] = useState(true);
  const [speichert, setSpeichert] = useState(false);
  const [meldung, setMeldung] = useState<string | null>(null);
  const [fehler, setFehler] = useState<string | null>(null);

  useEffect(() => {
    let aktiv = true;
    fetchPrioritySettings()
      .then((einstellungen) => {
        if (!aktiv) return;
        setEnabled(einstellungen.enabled);
        setMarkers(einstellungen.markers);
      })
      .catch((grund: unknown) => {
        if (aktiv) {
          setFehler(`Laden fehlgeschlagen: ${fehlerText(grund)}`);
        }
      })
      .finally(() => {
        if (aktiv) {
          setLaedt(false);
        }
      });
    return () => {
      aktiv = false;
    };
  }, []);

  const hinzufuegen = () => {
    const wert = neuerMarker.trim();
    if (wert === "") {
      return;
    }
    const vorhanden = markers.some(
      (marker) => marker.trim().toLowerCase() === wert.toLowerCase(),
    );
    if (vorhanden) {
      setFehler("Dieser Marker ist bereits vorhanden.");
      return;
    }
    setMarkers([...markers, wert]);
    setNeuerMarker("");
    setMeldung(null);
    setFehler(null);
  };

  const entfernen = (index: number) => {
    setMarkers(markers.filter((_, i) => i !== index));
    setMeldung(null);
    setFehler(null);
  };

  const aendern = (index: number, wert: string) => {
    setMarkers(markers.map((marker, i) => (i === index ? wert : marker)));
    setMeldung(null);
    setFehler(null);
  };

  const speichern = () => {
    setSpeichert(true);
    setMeldung(null);
    setFehler(null);
    updatePrioritySettings({
      enabled,
      markers: markers.map((marker) => marker.trim()).filter(Boolean),
    })
      .then((gespeichert) => {
        setEnabled(gespeichert.enabled);
        setMarkers(gespeichert.markers);
        setMeldung("Gespeichert.");
      })
      .catch((grund: unknown) => {
        setFehler(`Speichern fehlgeschlagen: ${fehlerText(grund)}`);
      })
      .finally(() => {
        setSpeichert(false);
      });
  };

  return (
    <section className="prioritaet">
      <h2>Priorität</h2>
      <p className="prioritaet-hinweis">
        Max-Requests überspringen die Warteschlange, unterbrechen aber keinen
        laufenden Job. Die Einstellung wirkt nach dem Speichern sofort, ohne
        Gateway-Neustart.
      </p>

      <label className="prioritaet-schalter">
        <input
          type="checkbox"
          data-testid="priority-enabled"
          checked={enabled}
          onChange={(ereignis) => {
            setEnabled(ereignis.target.checked);
            setMeldung(null);
            setFehler(null);
          }}
        />
        <span className="prioritaet-schalter-text">Max bevorzugen</span>
        <span className="prioritaet-schalter-hinweis">
          {enabled
            ? "Max bekommt Priorität 100, alle anderen Requests 0."
            : "Aus: Das Gateway entfernt Priority aus allen Chat-Requests."}
        </span>
      </label>

      <h3>System-Prompt-Marker</h3>
      <p className="prioritaet-hinweis">
        Ein Treffer in einer <code>system</code>-Nachricht markiert den Request
        als Max. Groß-/Kleinschreibung und zusätzliche Leerzeichen sind egal.
        Für Max' Hilfsrequests (Memory, Zusammenfassung, Security) jeweils
        einen eigenen Marker eintragen.
      </p>

      {laedt ? (
        <p className="platzhalter">Lade Einstellungen ...</p>
      ) : (
        <>
          <ul className="marker-liste">
            {markers.map((marker, index) => (
              <li className="marker-zeile" key={index}>
                <input
                  type="text"
                  aria-label={`Marker ${index + 1}`}
                  data-testid={`priority-marker-${index}`}
                  value={marker}
                  onChange={(ereignis) => aendern(index, ereignis.target.value)}
                />
                <button
                  type="button"
                  className="prioritaet-aktion"
                  data-testid={`priority-remove-${index}`}
                  onClick={() => entfernen(index)}
                >
                  Entfernen
                </button>
              </li>
            ))}
          </ul>
          {markers.length === 0 && (
            <p className="platzhalter">
              Keine Marker: Es wird kein Request bevorzugt.
            </p>
          )}

          <div className="marker-neu">
            <input
              type="text"
              placeholder="Weiteren Marker hinzufügen"
              aria-label="Neuer Marker"
              data-testid="priority-new-marker"
              value={neuerMarker}
              onChange={(ereignis) => setNeuerMarker(ereignis.target.value)}
              onKeyDown={(ereignis) => {
                if (ereignis.key === "Enter") {
                  ereignis.preventDefault();
                  hinzufuegen();
                }
              }}
            />
            <button
              type="button"
              className="prioritaet-aktion"
              data-testid="priority-add-marker"
              onClick={hinzufuegen}
            >
              Hinzufügen
            </button>
          </div>

          <div className="prioritaet-aktionen">
            <button
              type="button"
              className="prioritaet-speichern"
              data-testid="priority-save"
              disabled={speichert}
              onClick={speichern}
            >
              {speichert ? "Speichere ..." : "Speichern"}
            </button>
            {meldung !== null && (
              <span className="meldung" role="status" data-testid="priority-meldung">
                {meldung}
              </span>
            )}
            {fehler !== null && (
              <span className="meldung meldung-fehler" role="alert">
                {fehler}
              </span>
            )}
          </div>
        </>
      )}
    </section>
  );
}
