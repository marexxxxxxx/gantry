import { useEffect, useState } from "react";
import {
  fetchHindsightSettings,
  fetchPrioritySettings,
  updateHindsightSettings,
  updatePrioritySettings,
} from "./api";

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
  const [hindsightEnabled, setHindsightEnabled] = useState(false);
  const [hindsightMarkers, setHindsightMarkers] = useState<string[]>([]);
  const [hindsightNeuerMarker, setHindsightNeuerMarker] = useState("");
  const [hindsightLaedt, setHindsightLaedt] = useState(true);
  const [hindsightSpeichert, setHindsightSpeichert] = useState(false);
  const [hindsightMeldung, setHindsightMeldung] = useState<string | null>(null);
  const [hindsightFehler, setHindsightFehler] = useState<string | null>(null);

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

  useEffect(() => {
    let aktiv = true;
    fetchHindsightSettings()
      .then((einstellungen) => {
        if (!aktiv) return;
        setHindsightEnabled(einstellungen.enabled);
        setHindsightMarkers(einstellungen.markers);
      })
      .catch((grund: unknown) => {
        if (aktiv) {
          setHindsightFehler(`Laden fehlgeschlagen: ${fehlerText(grund)}`);
        }
      })
      .finally(() => {
        if (aktiv) {
          setHindsightLaedt(false);
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

  const hindsightHinzufuegen = () => {
    const wert = hindsightNeuerMarker.trim();
    if (wert === "") {
      return;
    }
    const vorhanden = hindsightMarkers.some(
      (marker) => marker.trim().toLowerCase() === wert.toLowerCase(),
    );
    if (vorhanden) {
      setHindsightFehler("Dieser Marker ist bereits vorhanden.");
      return;
    }
    setHindsightMarkers([...hindsightMarkers, wert]);
    setHindsightNeuerMarker("");
    setHindsightMeldung(null);
    setHindsightFehler(null);
  };

  const hindsightEntfernen = (index: number) => {
    setHindsightMarkers(hindsightMarkers.filter((_, i) => i !== index));
    setHindsightMeldung(null);
    setHindsightFehler(null);
  };

  const hindsightAendern = (index: number, wert: string) => {
    setHindsightMarkers(
      hindsightMarkers.map((marker, i) => (i === index ? wert : marker)),
    );
    setHindsightMeldung(null);
    setHindsightFehler(null);
  };

  const hindsightUmschalten = (aktiv: boolean) => {
    const vorher = hindsightEnabled;
    setHindsightEnabled(aktiv);
    setHindsightSpeichert(true);
    setHindsightMeldung(null);
    setHindsightFehler(null);
    updateHindsightSettings({
      enabled: aktiv,
      markers: hindsightMarkers.map((marker) => marker.trim()).filter(Boolean),
    })
      .then((gespeichert) => {
        setHindsightEnabled(gespeichert.enabled);
        setHindsightMarkers(gespeichert.markers);
        if (gespeichert.enabled) {
          const anzahl = gespeichert.aborted;
          setHindsightMeldung(
            `Hindsight blockiert – ${anzahl} ${
              anzahl === 1 ? "Job" : "Jobs"
            } abgebrochen.`,
          );
        } else {
          setHindsightMeldung("Hindsight wieder erlaubt.");
        }
      })
      .catch((grund: unknown) => {
        setHindsightEnabled(vorher);
        setHindsightFehler(`Umschalten fehlgeschlagen: ${fehlerText(grund)}`);
      })
      .finally(() => {
        setHindsightSpeichert(false);
      });
  };

  const hindsightMarkerSpeichern = () => {
    setHindsightSpeichert(true);
    setHindsightMeldung(null);
    setHindsightFehler(null);
    updateHindsightSettings({
      enabled: hindsightEnabled,
      markers: hindsightMarkers.map((marker) => marker.trim()).filter(Boolean),
    })
      .then((gespeichert) => {
        setHindsightEnabled(gespeichert.enabled);
        setHindsightMarkers(gespeichert.markers);
        setHindsightMeldung("Hindsight-Marker gespeichert.");
      })
      .catch((grund: unknown) => {
        setHindsightFehler(`Speichern fehlgeschlagen: ${fehlerText(grund)}`);
      })
      .finally(() => {
        setHindsightSpeichert(false);
      });
  };

  return (
    <>
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

      <section className="prioritaet prioritaet-hindsight">
        <h2>Hindsight blockieren</h2>
        <p className="prioritaet-hinweis">
          Hindsight (Memory-Extraktion und Konsolidierung) wird abgewiesen,
          solange der Blocker an ist. Beim Einschalten bricht das Gateway
          wartende und laufende Hindsight-Jobs ab; normale Requests laufen
          weiter. Die Einstellung wirkt sofort, ohne Gateway-Neustart.
        </p>

        {hindsightLaedt ? (
          <p className="platzhalter">Lade Einstellungen ...</p>
        ) : (
          <>
            <label className="prioritaet-schalter">
              <input
                type="checkbox"
                data-testid="hindsight-enabled"
                checked={hindsightEnabled}
                disabled={hindsightSpeichert}
                onChange={(ereignis) =>
                  hindsightUmschalten(ereignis.target.checked)
                }
              />
              <span className="prioritaet-schalter-text">
                Hindsight blockieren
              </span>
              <span className="prioritaet-schalter-hinweis">
                {hindsightEnabled
                  ? "Neue Hindsight-Requests bekommen 503; wartende und laufende wurden beim Einschalten abgebrochen."
                  : "Hindsight-Requests laufen normal."}
              </span>
            </label>

            <h3>Erkennungsmerkmale</h3>
            <p className="prioritaet-hinweis">
              Geprüft werden nur <code>system</code>-Nachrichten. Standard sind
              Fakten-Extraktion und Memory-Konsolidierung; weitere
              Hindsight-Prompts können hier ergänzt werden.
            </p>

            <ul className="marker-liste">
              {hindsightMarkers.map((marker, index) => (
                <li className="marker-zeile" key={index}>
                  <input
                    type="text"
                    aria-label={`Hindsight-Marker ${index + 1}`}
                    data-testid={`hindsight-marker-${index}`}
                    value={marker}
                    onChange={(ereignis) =>
                      hindsightAendern(index, ereignis.target.value)
                    }
                  />
                  <button
                    type="button"
                    className="prioritaet-aktion"
                    data-testid={`hindsight-remove-${index}`}
                    onClick={() => hindsightEntfernen(index)}
                  >
                    Entfernen
                  </button>
                </li>
              ))}
            </ul>
            {hindsightMarkers.length === 0 && (
              <p className="platzhalter">
                Keine Marker: Es wird kein Request blockiert.
              </p>
            )}

            <div className="marker-neu">
              <input
                type="text"
                placeholder="Weiteren Hindsight-Marker hinzufügen"
                aria-label="Neuer Hindsight-Marker"
                data-testid="hindsight-new-marker"
                value={hindsightNeuerMarker}
                onChange={(ereignis) =>
                  setHindsightNeuerMarker(ereignis.target.value)
                }
                onKeyDown={(ereignis) => {
                  if (ereignis.key === "Enter") {
                    ereignis.preventDefault();
                    hindsightHinzufuegen();
                  }
                }}
              />
              <button
                type="button"
                className="prioritaet-aktion"
                data-testid="hindsight-add-marker"
                onClick={hindsightHinzufuegen}
              >
                Hinzufügen
              </button>
            </div>

            <div className="prioritaet-aktionen">
              <button
                type="button"
                className="prioritaet-speichern"
                data-testid="hindsight-save"
                disabled={hindsightSpeichert}
                onClick={hindsightMarkerSpeichern}
              >
                {hindsightSpeichert ? "Speichere ..." : "Marker speichern"}
              </button>
              {hindsightMeldung !== null && (
                <span
                  className="meldung"
                  role="status"
                  data-testid="hindsight-meldung"
                >
                  {hindsightMeldung}
                </span>
              )}
              {hindsightFehler !== null && (
                <span className="meldung meldung-fehler" role="alert">
                  {hindsightFehler}
                </span>
              )}
            </div>
          </>
        )}
      </section>
    </>
  );
}
