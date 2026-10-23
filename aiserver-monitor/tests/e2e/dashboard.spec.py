"""Playwright-E2E fuer das aiserver-monitor-Dashboard (Fake-SGLang).

Der Lauf braucht zwei Prozesse, die run_e2e.sh startet: das Gateway auf
E2E_BASE_URL (Default http://127.0.0.1:39000) und den Fake-SGLang aus
fake_sglang.py als Upstream (127.0.0.1:39001). Chromium kommt ueber EXE
(Pfad aus dem Playwright-Cache, von run_e2e.sh exportiert).

Sieben Szenarien laufen nacheinander in einem Browserfenster:

  1. Live-Ansicht: Verbindung, langsamer Job (wartend -> laeuft), Queue-
     Kennzahlen, Vorschau-Modal (Denken eingeklappt, Antwort waechst).
  2. Einzelabbruch ueber job-cancel, Status "abgebrochen" nach Filterwechsel.
  3. Warteschlange: zwei parallele Queue-Jobs, beide erst wartend, dann
     laufend/fertig; die Queue-Kennzahl "wartend" ist ein Wert > 0.
  4. Notaus: cancel-all armt, confirm-cancel-all bestaetigt, Auto-Reset.
  5. Statistik-Tab: KPIs und vier Charts sichtbar, kein Leerzustand,
     Zeitfenster-Chip 7 Tage wird gedrueckt.
  6. Reconnect: Gateway stoppen/starten, conn-status und Job-Tabelle kommen
     zurueck (neuer Job wird wieder live verfolgt).
  7. Prioritaet: Default-Marker und Schalter, Ausschalten, Marker ergaenzen,
     speichern und nach einem Tabwechsel erneut aus der DB laden; danach
     wieder auf den Ausgangszustand zuruecksetzen.

Jedes Szenario druckt eine deutsche PASS/FAIL-Zeile; der Exit-Code ist
ungleich 0, sobald eines fehlschlaegt. Es gibt keine festen Warteschlafen:
eigene Bedingungen laufen ueber warte_bis (Poll-Schleife mit Deadline),
Playwright-Assertions ueber expect mit Deadline (hoechstens 15 s).

Die Chat-Anfragen kommen aus Daemon-Hilfsthreads (stdlib urllib), damit die
langsamen SSE-Antworten parallel zum Browser laufen; Verbindungsfehler in den
Threads sind erwartbar (Abbruch, Gateway-Stopp) und werden ignoriert.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from playwright.sync_api import Locator, Page, expect, sync_playwright

#: Projektwurzel (tests/e2e/dashboard.spec.py -> tests -> Projekt); run.sh
#: wird im Reconnect-Szenario von hier aus aufgerufen.
ROOT = Path(__file__).resolve().parents[2]

#: Basis-URL des Gateways; run_e2e.sh exportiert sie.
BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:39000")

#: Chromium-Binary aus dem Playwright-Cache (Pflicht).
EXE = os.environ["EXE"]

#: Deadline aller Wartebedingungen in Millisekunden (Ruling: hoechstens 15 s).
DEADLINE_MS = 15000

T = TypeVar("T")


# --- Anfragen aus Hilfsthreads ---------------------------------------------


def _anfrage_senden(koerper: dict) -> None:
    """Ein POST aus einem Hilfsthread; Fehler werden bewusst ignoriert.

    Nach einem Gateway-Stopp oder einem Abbruch bricht die Verbindung ab -
    das ist Teil des Tests und kein Fehler. Der Strom wird komplett gelesen,
    damit sich Client und Gateway wie im echten Betrieb verhalten.
    """
    daten = json.dumps(koerper).encode("utf-8")
    anfrage = urllib.request.Request(
        f"{BASE}/v1/chat/completions", data=daten,
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(anfrage, timeout=120) as antwort:
            while antwort.read(4096):
                pass
    except Exception:
        pass


def chat_im_hintergrund(rid: str, *, prompt: str) -> threading.Thread:
    """Startet einen Streaming-Request (stream=true) als Daemon-Thread."""
    koerper = {
        "model": "fake-modell",
        "rid": rid,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    thread = threading.Thread(target=_anfrage_senden, args=(koerper,),
                              daemon=True, name=f"chat-{rid}")
    thread.start()
    return thread


# --- Warten und Lesen ------------------------------------------------------


def warte_bis(bedingung: Callable[[], T], beschreibung: str,
              deadline_s: float = 15.0) -> T:
    """Poll-Schleife mit Deadline; liefert den ersten wahrhaften Wert.

    Kein fester Schlaf: alle 0,05 s neu pruefen, bis die Bedingung wahr ist
    oder die Deadline ablaeuft. Ausnahmen der Bedingung (z. B. eine Zeile,
    die noch nicht im DOM ist) gelten als "noch nicht wahr".
    """
    ende = time.monotonic() + deadline_s
    letzter: object = None
    while True:
        try:
            wert = bedingung()
        except Exception as fehler:                 # Element (noch) nicht da
            letzter = fehler
        else:
            if wert:
                return wert
            letzter = wert
        if time.monotonic() >= ende:
            raise AssertionError(
                f"Zeitlimit {deadline_s:g}s ueberschritten: {beschreibung}"
                f" (zuletzt: {letzter!r})")
        time.sleep(0.05)


def verbindungs_text(page: Page) -> str:
    """Text der Kopfleiste: verbinde ... / verbunden / getrennt."""
    return page.get_by_test_id("conn-status").inner_text()


def status_text(page: Page, rid: str) -> str:
    """Statustext der Job-Zeile; fehlt die Zeile, gibt es eine Ausnahme.

    ``count()`` fragt ohne Wartezeit ab: ein fehlendes Element darf die
    Poll-Schleife nicht in die 30-s-Standardwartezeit von ``inner_text``
    laufen lassen.
    """
    zeile = page.get_by_test_id(f"job-row-{rid}")
    if zeile.count() == 0:
        raise LookupError(f"job-row-{rid} ist noch nicht da")
    return zeile.get_by_test_id("job-status").inner_text(timeout=2000)


def queue_werte(page: Page) -> tuple[str, str]:
    """Die beiden Queue-Zahlen als Text: (laufend, wartend)."""
    werte = page.get_by_test_id("queue-stats").locator(".queue-wert")
    expect(werte).to_have_count(2)
    return werte.nth(0).inner_text(), werte.nth(1).inner_text()


def job_zeile(page: Page, rid: str) -> Locator:
    """Die Job-Zeile (tbody-Testid aus dem T13-Vertrag)."""
    return page.get_by_test_id(f"job-row-{rid}")


# --- Szenarien -------------------------------------------------------------


def szenario_1_live(page: Page) -> None:
    """Live-Ansicht: langsamer Job, Queue-Werte, Vorschau-Modal."""
    expect(page.get_by_test_id("conn-status")).to_contain_text(
        "verbunden", timeout=DEADLINE_MS)

    # "langsam" im rid skriptet im Fake: Rollen-Chunk sofort, erstes Token
    # nach ~0,4 s, danach eines je ~0,5 s - lang genug fuer Modal-Wachstum
    # und Einzelabbruch. Der erste gelesene Status muss "wartend" sein,
    # danach laeuft der Job.
    chat_im_hintergrund("e2e-langsam", prompt="Bitte langsam antworten")
    erster_status = warte_bis(lambda: status_text(page, "e2e-langsam"),
                              "job-row-e2e-langsam ist sichtbar", 15.0)
    if "wartend" not in erster_status:
        raise AssertionError(
            f"e2e-langsam startete nicht wartend, sondern {erster_status!r}")
    expect(job_zeile(page, "e2e-langsam").get_by_test_id("job-status")
           ).to_contain_text("l\u00e4uft", timeout=DEADLINE_MS)

    # Queue-Kennzahlen aus dem letzten Sample (Fake: 3 laufend, 1 wartend).
    laufend, wartend = queue_werte(page)
    if not (laufend.isdigit() and wartend.isdigit()):
        raise AssertionError(
            f"queue-stats zeigt keine Werte: laufend={laufend!r},"
            f" wartend={wartend!r}")

    # Modal per Zeilenklick oeffnen: sichtbar, Denken eingeklappt, Antwort
    # waechst waehrend des Streams.
    zeile = job_zeile(page, "e2e-langsam")
    zeile.get_by_test_id("job-row").click()
    modal = page.get_by_test_id("modal")
    expect(modal).to_be_visible(timeout=DEADLINE_MS)
    if page.get_by_test_id("modal-thinking").get_attribute("open") is not None:
        raise AssertionError("modal-thinking ist nicht eingeklappt (open-Attribut)")
    antwort = page.get_by_test_id("modal-answer")
    erste_laenge = len(antwort.inner_text())
    warte_bis(lambda: len(antwort.inner_text()) > erste_laenge,
              "modal-answer waechst weiter", 11.0)

    page.locator(".modal-schliessen").click()
    expect(modal).to_have_count(0, timeout=5000)


def szenario_2_abbruch(page: Page) -> None:
    """Einzelabbruch: Filter Alle, job-cancel, Status "abgebrochen"."""
    # Erst auf "Alle" schalten: ein abgebrochener Job faellt aus dem Filter
    # "Aktiv", die Zeile bleibt so fuer die Statuspruefung sichtbar
    # (T13-Hinweis: Filter vor der Statuspruefung umschalten).
    umschalter = page.get_by_test_id("filter-active")
    expect(umschalter).to_have_text("Aktiv", timeout=DEADLINE_MS)
    umschalter.click()
    expect(umschalter).to_have_text("Alle", timeout=DEADLINE_MS)

    zeile = job_zeile(page, "e2e-langsam")
    expect(zeile).to_be_visible(timeout=DEADLINE_MS)
    zeile.get_by_test_id("job-cancel").click()

    expect(job_zeile(page, "e2e-langsam").get_by_test_id("job-status")
           ).to_contain_text("abgebrochen", timeout=DEADLINE_MS)


def szenario_3_warteschlange(page: Page) -> None:
    """Zwei parallele Queue-Jobs: erst wartend, dann laufend oder fertig."""
    # Filter steht seit Szenario 2 auf "Alle"; beide Zeilen bleiben sichtbar.
    expect(page.get_by_test_id("filter-active")).to_have_text(
        "Alle", timeout=DEADLINE_MS)

    # "queue" im rid skriptet im Fake: ~2,5 s vor dem ersten Token.
    chat_im_hintergrund("e2e-queue-1", prompt="Erster Queue-Fall")
    chat_im_hintergrund("e2e-queue-2", prompt="Zweiter Queue-Fall")

    def beide_wartend() -> bool:
        return ("wartend" in status_text(page, "e2e-queue-1")
                and "wartend" in status_text(page, "e2e-queue-2"))

    warte_bis(beide_wartend, "beide Queue-Jobs sind wartend", 10.0)

    # Die Queue-Kennzahl "wartend" ist ein echter Messwert > 0.
    _, wartend = queue_werte(page)
    if not wartend.isdigit() or int(wartend) <= 0:
        raise AssertionError(f"queue-stats wartend ist kein Wert > 0: {wartend!r}")

    # Danach sind beide Zeilen gelaufen oder fertig (kein Haenger).
    for rid in ("e2e-queue-1", "e2e-queue-2"):
        expect(job_zeile(page, rid).get_by_test_id("job-status")
               ).to_contain_text(re.compile("l\u00e4uft|fertig"), timeout=DEADLINE_MS)


def szenario_4_notaus(page: Page) -> None:
    """Notaus: zwei Klicks mit Auto-Reset der Bestaetigung."""
    bestaetigen = page.get_by_test_id("confirm-cancel-all")
    expect(bestaetigen).to_have_count(0)

    page.get_by_test_id("cancel-all").click()
    expect(bestaetigen).to_be_visible(timeout=5000)

    bestaetigen.click()
    expect(bestaetigen).to_have_count(0, timeout=5000)


def szenario_5_statistik(page: Page) -> None:
    """Statistik-Tab: KPIs, vier Charts, Zeitfenster-Chip."""
    page.get_by_test_id("tab-stats").click()
    expect(page.get_by_test_id("kpi-count")).to_be_visible(timeout=DEADLINE_MS)

    for testid in ("chart-queue", "chart-throughput", "chart-tokens", "chart-gpu"):
        expect(page.get_by_test_id(testid)).to_be_visible(timeout=DEADLINE_MS)

    if page.get_by_test_id("stat-leer").count() > 0:
        raise AssertionError("Statistik zeigt den Leerzustand statt KPIs")
    if "keine Daten im Zeitfenster" in page.content():
        raise AssertionError("Statistik enthaelt 'keine Daten im Zeitfenster'")

    chip = page.get_by_test_id("stat-window-7d")
    chip.click()
    expect(chip).to_have_attribute("aria-pressed", "true", timeout=DEADLINE_MS)
    expect(page.get_by_test_id("kpi-count")).to_be_visible(timeout=DEADLINE_MS)


def szenario_6_reconnect(page: Page) -> None:
    """Gateway stoppen/starten: Verbindung und Live-Tabelle kommen zurueck."""
    # Verbindung kappen; der EventSource meldet getrennt (und versucht
    # danach selbst wieder zu verbinden: "verbinde").
    subprocess.run(["bash", "run.sh", "stop"], cwd=ROOT, check=True)

    def getrennt_oder_verbinde() -> bool:
        text = verbindungs_text(page)
        return "getrennt" in text or "verbinde" in text

    warte_bis(getrennt_oder_verbinde,
              "conn-status zeigt getrennt/verbinde", 15.0)

    # Wieder starten (run.sh pollt /healthz selbst) und warten, bis der
    # eigene EventSource-Reconnect durch ist.
    subprocess.run(["bash", "run.sh", "start"], cwd=ROOT, check=True)
    warte_bis(lambda: "verbunden" in verbindungs_text(page),
              "conn-status zeigt verbunden", 10.0)

    # Live-Ansicht: der Snapshot des neuen Prozesses ist da (Queue-Kennzahlen
    # aus der History, kein "-"), und ein neuer Job wird wieder live verfolgt.
    # Die Job-Registry liegt nur im RAM, alte Eintraege sind daher weg.
    page.get_by_test_id("tab-live").click()
    umschalter = page.get_by_test_id("filter-active")
    expect(umschalter).to_have_text("Aktiv", timeout=DEADLINE_MS)
    umschalter.click()
    expect(umschalter).to_have_text("Alle", timeout=DEADLINE_MS)

    chat_im_hintergrund("e2e-nach-reconnect-langsam",
                        prompt="Job nach dem Reconnect")

    def queue_werte_da() -> bool:
        stats = page.get_by_test_id("queue-stats")
        return stats.count() > 0 and "-" not in stats.inner_text(timeout=2000)

    warte_bis(queue_werte_da, "queue-stats zeigt wieder Werte", 10.0)
    zeile = job_zeile(page, "e2e-nach-reconnect-langsam")
    expect(zeile).to_be_visible(timeout=DEADLINE_MS)
    expect(zeile.get_by_test_id("job-status")).to_contain_text(
        "l\u00e4uft", timeout=DEADLINE_MS)

    # Aufraeumen: der neue Job laesst sich auch nach dem Reconnect abbrechen.
    zeile.get_by_test_id("job-cancel").click()
    expect(zeile.get_by_test_id("job-status")).to_contain_text(
        "abgebrochen", timeout=DEADLINE_MS)


def szenario_7_prioritaet(page: Page) -> None:
    """Prioritaets-Tab: Defaults, Speichern, Persistenz und Zuruecksetzen."""
    page.get_by_test_id("tab-priority").click()

    schalter = page.get_by_test_id("priority-enabled")
    standard_marker = page.get_by_test_id("priority-marker-0")
    expect(schalter).to_be_checked(timeout=DEADLINE_MS)
    expect(standard_marker).to_have_value(
        "You are Max, built for Marec", timeout=DEADLINE_MS)

    # Ausschalten und einen zusaetzlichen Marker anlegen.
    schalter.uncheck()
    page.get_by_test_id("priority-new-marker").fill("E2E Marker")
    page.get_by_test_id("priority-add-marker").click()
    expect(page.get_by_test_id("priority-marker-1")).to_have_value("E2E Marker")
    page.get_by_test_id("priority-save").click()
    expect(page.get_by_test_id("priority-meldung")).to_have_text(
        "Gespeichert.", timeout=DEADLINE_MS)

    # Tabwechsel erzwingt einen Neustart der Ansicht: die Werte muessen aus
    # der DB nachgeladen werden, nicht nur im React-State stehen.
    page.get_by_test_id("tab-live").click()
    page.get_by_test_id("tab-priority").click()
    expect(page.get_by_test_id("priority-enabled")).not_to_be_checked(
        timeout=DEADLINE_MS)
    expect(page.get_by_test_id("priority-marker-1")).to_have_value("E2E Marker")

    # Ausgangszustand wiederherstellen (die Temp-DB wird ohnehin verworfen).
    page.get_by_test_id("priority-enabled").check()
    page.get_by_test_id("priority-remove-1").click()
    page.get_by_test_id("priority-save").click()
    expect(page.get_by_test_id("priority-meldung")).to_have_text(
        "Gespeichert.", timeout=DEADLINE_MS)
    expect(page.get_by_test_id("priority-enabled")).to_be_checked()
    expect(page.get_by_test_id("priority-marker-1")).to_have_count(0)


#: Reihenfolge der Szenarien (Name fuer die PASS/FAIL-Zeile, Funktion).
SZENARIEN: list[tuple[str, Callable[[Page], None]]] = [
    ("Szenario 1 Live-Ansicht und Vorschau-Modal", szenario_1_live),
    ("Szenario 2 Einzelabbruch", szenario_2_abbruch),
    ("Szenario 3 Warteschlange", szenario_3_warteschlange),
    ("Szenario 4 Notaus", szenario_4_notaus),
    ("Szenario 5 Statistik", szenario_5_statistik),
    ("Szenario 6 Reconnect", szenario_6_reconnect),
    ("Szenario 7 Prioritaet", szenario_7_prioritaet),
]


def main() -> int:
    """Startet Chromium, faehrt alle Szenarien und liefert den Exit-Code."""
    expect.set_options(timeout=DEADLINE_MS)
    fehlgeschlagen: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=EXE, headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(DEADLINE_MS)
            page.set_default_navigation_timeout(DEADLINE_MS)
            try:
                page.goto(BASE, wait_until="domcontentloaded")
            except Exception as fehler:
                print(f"FAIL: Startseite {BASE} nicht erreichbar:"
                      f" {' '.join(str(fehler).split())}")
                return 1

            for name, funktion in SZENARIEN:
                try:
                    funktion(page)
                except Exception as fehler:
                    kurz = " ".join(str(fehler).split())
                    print(f"FAIL: {name}: {kurz}")
                    fehlgeschlagen.append(name)
                else:
                    print(f"PASS: {name}")
        finally:
            browser.close()

    if fehlgeschlagen:
        print(f"\n{len(fehlgeschlagen)} von {len(SZENARIEN)} Szenarien"
              f" fehlgeschlagen: {', '.join(fehlgeschlagen)}")
        return 1
    print("\nAlle Szenarien bestanden")
    return 0


if __name__ == "__main__":
    sys.exit(main())
