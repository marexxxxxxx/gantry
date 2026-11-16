"""SQLite-History fuer Job-Metadaten und Metrik-Messreihen.

Persistiert nur die Spalten aus Schema v1: Jobs ohne vollen Prompt (nur der
Anfang ``prompt_start``) und Samples ohne Zusatzfelder (z. B. ``max_tokens``).
Alle Zeiten sind Epoche-Sekunden; die Millisekunden-Felder aus
:func:`aiserver_monitor.jobs.job_to_dict` werden beim Schreiben umgerechnet.
Nicht gemessene Werte bleiben NULL, nie 0.

Die Aufrufe laufen single-threaded ueber die asyncio-Schleife der App, daher
wird die SQLite-Verbindung ohne ``check_same_thread=False`` gefuehrt.
``init()`` legt Schema und WAL-Modus an, ``close()`` schliesst die Verbindung.
"""

import math
import sqlite3
from pathlib import Path

#: Retention-Defaults analog Config; die App kann die Config-Werte durchreichen.
RETENTION_JOBS_DAYS = 30
RETENTION_SERIES_DAYS = 7

#: So viele Fehlerursachen liefert ``query_stats`` hoechstens (Top nach Anzahl).
FEHLER_TOP = 10

#: Laengste angezeigte Fehlerursache in Zeichen.
FEHLER_MAX_LAENGE = 80

_SCHEMA_JOBS = """\
CREATE TABLE IF NOT EXISTS jobs(
  rid TEXT PRIMARY KEY, source TEXT, model TEXT, prompt_start TEXT,
  status TEXT NOT NULL, created_at REAL NOT NULL,
  first_token_at REAL, finished_at REAL,
  prompt_tokens INTEGER, completion_tokens INTEGER, error TEXT)"""

_SCHEMA_SAMPLES = """\
CREATE TABLE IF NOT EXISTS samples(
  ts REAL PRIMARY KEY, available INTEGER NOT NULL,
  running INTEGER, waiting INTEGER, throughput REAL, token_usage REAL,
  gpu_util INTEGER, vram_used_mb INTEGER, vram_total_mb INTEGER)"""

_SCHEMA_SETTINGS = """\
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY, value TEXT NOT NULL)"""


def percentile(values: list[float], p: int) -> float | None:
    """Nearest-Rank-Perzentil ohne Interpolation; leere Liste ergibt None.

    ``p`` liegt zwischen 0 und 100; das Ergebnis ist immer ein vorhandener
    Messwert (z. B. p95 aus 5 Werten = groesster Wert).
    """
    if not values:
        return None
    sortiert = sorted(values)
    rang = math.ceil(p / 100.0 * len(sortiert))
    index = min(max(rang - 1, 0), len(sortiert) - 1)
    return float(sortiert[index])


def fehler_ursache(text: str) -> str:
    """Kurze, gruppierbare Fehlerursache aus einem Job-Fehlertext.

    Der Teil vor dem ersten Doppelpunkt ist die Ursachenklasse
    (z. B. ``RemoteProtocolError``); ohne Doppelpunkt bleibt der ganze Text.
    Getrimmt und auf :data:`FEHLER_MAX_LAENGE` Zeichen gekuerzt; ein leerer
    Text ergibt ``"Unbekannt"``.
    """
    roh = text.strip()
    if not roh:
        return "Unbekannt"
    klasse = roh.split(":", 1)[0].strip()
    return (klasse or roh)[:FEHLER_MAX_LAENGE]


class History:
    """SQLite-Historie: Job-Metadaten, Messreihen, Stats, Retention, Recovery.

    ``retention_jobs_days``/``retention_series_days`` steuern :meth:`cleanup`
    und entsprechen den Config-Defaults.
    """

    def __init__(self, path: str | Path, *,
                 retention_jobs_days: int = RETENTION_JOBS_DAYS,
                 retention_series_days: int = RETENTION_SERIES_DAYS):
        self._path = Path(path)
        self.retention_jobs_days = retention_jobs_days
        self.retention_series_days = retention_series_days
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        """Oeffnet die DB, legt das Schema an und aktiviert WAL; idempotent."""
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA_JOBS)
        conn.execute(_SCHEMA_SAMPLES)
        conn.execute(_SCHEMA_SETTINGS)
        conn.commit()
        self._conn = conn

    def get_setting(self, key: str) -> str | None:
        """Wert einer allgemeinen Einstellung oder None, wenn sie fehlt."""
        zeile = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if zeile is None else zeile[0]

    def set_setting(self, key: str, value: str) -> None:
        """Schreibt/ersetzt eine allgemeine Einstellung (Upsert auf ``key``)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value))

    def record_job(self, job_dict: dict) -> None:
        """Schreibt/ersetzt eine Job-Zeile (INSERT OR REPLACE auf ``rid``).

        Uebernimmt die Spalten des Jobs-Schemas. ``rid``, ``status`` und
        ``created_at_ms`` sind Pflicht. ``created_at`` entsteht aus
        ``created_at_ms``, ``first_token_at``/``finished_at`` aus ``ttft_ms``/
        ``duration_ms`` (None bleibt NULL). ``full_prompt`` und alle weiteren
        Keys (``tok_per_s``, Vorschauen) werden ignoriert.
        """
        created_at = job_dict["created_at_ms"] / 1000.0
        ttft_ms = job_dict.get("ttft_ms")
        duration_ms = job_dict.get("duration_ms")
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs("
                " rid, source, model, prompt_start, status, created_at,"
                " first_token_at, finished_at, prompt_tokens,"
                " completion_tokens, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_dict["rid"], job_dict.get("source"),
                 job_dict.get("model"), job_dict.get("prompt_start"),
                 job_dict["status"], created_at,
                 None if ttft_ms is None else created_at + ttft_ms / 1000.0,
                 None if duration_ms is None else created_at + duration_ms / 1000.0,
                 job_dict.get("prompt_tokens"),
                 job_dict.get("completion_tokens"),
                 job_dict.get("error")))

    def record_sample(self, sample: dict) -> None:
        """Schreibt/ersetzt eine Sample-Zeile (INSERT OR REPLACE auf ``ts``).

        Uebernimmt exakt die Samples-Spalten; Extra-Keys (z. B. ``max_tokens``)
        werden ignoriert, fehlende optionale Keys landen als NULL in der Spalte
        (``ts`` und ``available`` sind Pflicht).
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO samples("
                " ts, available, running, waiting, throughput, token_usage,"
                " gpu_util, vram_used_mb, vram_total_mb)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sample["ts"], sample["available"], sample.get("running"),
                 sample.get("waiting"), sample.get("throughput"),
                 sample.get("token_usage"), sample.get("gpu_util"),
                 sample.get("vram_used_mb"), sample.get("vram_total_mb")))

    def mark_stale_running(self, now: float) -> int:
        """Recovery nach Gateway-Neustart: waiting/running -> error.

        Setzt ``status='error'``, ``error='gateway restart'`` und (falls noch
        leer) ``finished_at=now``; liefert die Anzahl geaenderter Zeilen.
        """
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE jobs SET status = 'error', error = 'gateway restart',"
                " finished_at = COALESCE(finished_at, ?)"
                " WHERE status IN ('waiting', 'running')", (now,))
        return cursor.rowcount

    def query_stats(self, from_ts: float, to_ts: float, bucket_s: int) -> dict:
        """Statistik ueber ein Zeitfenster (Epoche-Sekunden, beide Grenzen inklusiv).

        ``overview`` zaehlt Jobs nach ``created_at`` (done/aborted/error,
        Token-Summen, TTFT-/Dauer-Perzentile in Sekunden, Dekodierrate aus
        fertigen Jobs; nichts gemessen -> None). ``buckets`` mittelt die
        Samples je ``bucket_s``-Fenster (Start = floor(ts / bucket_s) *
        bucket_s) und ergaenzt Job-Aggregate desselben Fensters; je Spalte
        werden nur gefuellte Werte gemittelt, Spalten ohne Wert bleiben None;
        ``available`` ist der Anteil verfuegbarer Samples. ``models``,
        ``sources`` und ``errors`` schluesseln die Jobs im Fenster auf.
        Job-Fenster ohne Sample und Sample-Fenster ohne Job werden beide
        geliefert (fehlende Seite: 0 bzw. None). ``bucket_s`` muss positiv
        sein (sonst ValueError statt stiller NULL-Buckets).
        """
        if bucket_s <= 0:
            raise ValueError(f"bucket_s muss positiv sein: {bucket_s}")

        zeilen = self._conn.execute(
            "SELECT source, model, status, created_at, first_token_at,"
            " finished_at, prompt_tokens, completion_tokens, error"
            " FROM jobs WHERE created_at >= ? AND created_at <= ?",
            (from_ts, to_ts)).fetchall()

        status_anzahl = {"done": 0, "aborted": 0, "error": 0}
        tokens_in_summe = tokens_out_summe = 0
        tokens_in_gemessen = tokens_out_gemessen = False
        ttft_werte: list[float] = []
        dauer_werte: list[float] = []
        raten: list[float] = []
        model_gruppen: dict[str, dict] = {}
        quellen_gruppen: dict[str, dict] = {}
        fehler_anzahl: dict[str, int] = {}
        bucket_gruppen: dict[int, dict] = {}

        def gruppe(tabelle: dict[str, dict], key: str) -> dict:
            """Aggregat-Eintrag je Modell/Quelle; Listen fuer Mittelwerte."""
            return tabelle.setdefault(key, {
                "jobs": 0, "done": 0, "error": 0,
                "tokens_in": 0, "tokens_in_gemessen": False,
                "tokens_out": 0, "tokens_out_gemessen": False,
                "ttft": [], "raten": [],
            })

        for (source, model, status, created_at, first_token_at,
             finished_at, prompt_tokens, completion_tokens, error) in zeilen:
            ttft = (None if first_token_at is None
                    else first_token_at - created_at)
            dauer = (None if finished_at is None
                     else finished_at - created_at)
            rate = None
            if (status == "done" and completion_tokens is not None
                    and first_token_at is not None
                    and finished_at is not None
                    and finished_at > first_token_at):
                rate = completion_tokens / (finished_at - first_token_at)

            if status in status_anzahl:
                status_anzahl[status] += 1
            if prompt_tokens is not None:
                tokens_in_summe += prompt_tokens
                tokens_in_gemessen = True
            if completion_tokens is not None:
                tokens_out_summe += completion_tokens
                tokens_out_gemessen = True
            if ttft is not None:
                ttft_werte.append(ttft)
            if dauer is not None:
                dauer_werte.append(dauer)
            if rate is not None:
                raten.append(rate)
            if status == "error" and error:
                ursache = fehler_ursache(error)
                fehler_anzahl[ursache] = fehler_anzahl.get(ursache, 0) + 1

            for tabelle, key in ((model_gruppen, model or ""),
                                 (quellen_gruppen, source or "")):
                eintrag = gruppe(tabelle, key)
                eintrag["jobs"] += 1
                if status in ("done", "error"):
                    eintrag[status] += 1
                if prompt_tokens is not None:
                    eintrag["tokens_in"] += prompt_tokens
                    eintrag["tokens_in_gemessen"] = True
                if completion_tokens is not None:
                    eintrag["tokens_out"] += completion_tokens
                    eintrag["tokens_out_gemessen"] = True
                if ttft is not None:
                    eintrag["ttft"].append(ttft)
                if rate is not None:
                    eintrag["raten"].append(rate)

            fenster = int(created_at // bucket_s) * bucket_s
            bucket = bucket_gruppen.setdefault(fenster, {
                "jobs": 0, "done": 0, "aborted": 0, "error": 0,
                "tokens_in": 0, "tokens_in_gemessen": False,
                "tokens_out": 0, "tokens_out_gemessen": False,
                "raten": [], "ttft": [], "dauer": [],
            })
            bucket["jobs"] += 1
            if status in ("done", "aborted", "error"):
                bucket[status] += 1
            if prompt_tokens is not None:
                bucket["tokens_in"] += prompt_tokens
                bucket["tokens_in_gemessen"] = True
            if completion_tokens is not None:
                bucket["tokens_out"] += completion_tokens
                bucket["tokens_out_gemessen"] = True
            if ttft is not None:
                bucket["ttft"].append(ttft)
            if dauer is not None:
                bucket["dauer"].append(dauer)
            if rate is not None:
                bucket["raten"].append(rate)

        def aufschluesselung(tabelle: dict[str, dict], name: str) -> list[dict]:
            """Finale Modell-/Quellenliste, meistbeschaeftigt zuerst."""
            ergebnis = []
            for key, eintrag in tabelle.items():
                ergebnis.append({
                    name: key,
                    "jobs": eintrag["jobs"],
                    "done": eintrag["done"],
                    "error": eintrag["error"],
                    "tokens_in": (eintrag["tokens_in"]
                                  if eintrag["tokens_in_gemessen"] else None),
                    "tokens_out": (eintrag["tokens_out"]
                                   if eintrag["tokens_out_gemessen"] else None),
                    "avg_ttft": (sum(eintrag["ttft"]) / len(eintrag["ttft"])
                                 if eintrag["ttft"] else None),
                    "avg_tok_per_s": (sum(eintrag["raten"])
                                      / len(eintrag["raten"])
                                      if eintrag["raten"] else None),
                })
            ergebnis.sort(key=lambda eintrag: (-eintrag["jobs"], eintrag[name]))
            return ergebnis

        sample_buckets = {row[0]: {
            "ts": row[0], "available": row[1], "running": row[2],
            "waiting": row[3], "throughput": row[4], "token_usage": row[5],
            "gpu_util": row[6], "vram_used_mb": row[7],
            "vram_total_mb": row[8],
        } for row in self._conn.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS fenster,"
            " AVG(available), AVG(running), AVG(waiting), AVG(throughput),"
            " AVG(token_usage), AVG(gpu_util), AVG(vram_used_mb),"
            " AVG(vram_total_mb)"
            " FROM samples WHERE ts >= ? AND ts <= ?"
            " GROUP BY fenster ORDER BY fenster",
            (bucket_s, bucket_s, from_ts, to_ts))}

        buckets = []
        for ts in sorted(set(sample_buckets) | set(bucket_gruppen)):
            bucket = sample_buckets.get(ts)
            if bucket is None:
                bucket = {
                    "ts": ts, "available": 0.0, "running": None,
                    "waiting": None, "throughput": None, "token_usage": None,
                    "gpu_util": None, "vram_used_mb": None,
                    "vram_total_mb": None,
                }
            roh = bucket_gruppen.get(ts)
            if roh is None:
                bucket.update({
                    "jobs": 0, "done": 0, "error": 0, "aborted": 0,
                    "tokens_in": 0, "tokens_out": 0, "tok_per_s": None,
                    "ttft_p50": None, "duration_p50": None,
                })
            else:
                bucket.update({
                    "jobs": roh["jobs"], "done": roh["done"],
                    "error": roh["error"], "aborted": roh["aborted"],
                    "tokens_in": (roh["tokens_in"]
                                  if roh["tokens_in_gemessen"] else None),
                    "tokens_out": (roh["tokens_out"]
                                   if roh["tokens_out_gemessen"] else None),
                    "tok_per_s": (sum(roh["raten"]) / len(roh["raten"])
                                  if roh["raten"] else None),
                    "ttft_p50": percentile(roh["ttft"], 50),
                    "duration_p50": percentile(roh["dauer"], 50),
                })
            buckets.append(bucket)

        return {
            "overview": {
                "count": len(zeilen),
                "done": status_anzahl["done"],
                "aborted": status_anzahl["aborted"],
                "error": status_anzahl["error"],
                "tokens_in": tokens_in_summe if tokens_in_gemessen else None,
                "tokens_out": (tokens_out_summe
                               if tokens_out_gemessen else None),
                "ttft": {"p50": percentile(ttft_werte, 50),
                         "p95": percentile(ttft_werte, 95),
                         "p99": percentile(ttft_werte, 99)},
                "duration": {"p50": percentile(dauer_werte, 50),
                             "p95": percentile(dauer_werte, 95)},
                "tok_per_s": {
                    "avg": sum(raten) / len(raten) if raten else None,
                    "max": max(raten) if raten else None,
                },
            },
            "buckets": buckets,
            "models": aufschluesselung(model_gruppen, "model"),
            "sources": aufschluesselung(quellen_gruppen, "source"),
            "errors": [
                {"kind": kind, "count": anzahl}
                for kind, anzahl in sorted(
                    fehler_anzahl.items(), key=lambda eintrag: (-eintrag[1],
                                                               eintrag[0]))[:FEHLER_TOP]
            ],
        }

    def last_sample(self) -> dict | None:
        """Letzte Sample-Zeile als dict (gleiche Keys wie die query_stats-Buckets).

        ``available`` ist bool, fehlende Spalten sind None; eine leere Tabelle
        ergibt None. Dient dem SSE-Snapshot, damit das Dashboard sofort den
        letzten bekannten Stand zeigt.
        """
        zeile = self._conn.execute(
            "SELECT ts, available, running, waiting, throughput, token_usage,"
            " gpu_util, vram_used_mb, vram_total_mb"
            " FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        if zeile is None:
            return None
        return {
            "ts": zeile[0],
            "available": bool(zeile[1]),
            "running": zeile[2],
            "waiting": zeile[3],
            "throughput": zeile[4],
            "token_usage": zeile[5],
            "gpu_util": zeile[6],
            "vram_used_mb": zeile[7],
            "vram_total_mb": zeile[8],
        }

    def cleanup(self, now: float) -> tuple[int, int]:
        """Loescht Jobs/Samples aelter als die Retention (Stichtag exklusiv).

        Rueckgabe: (geloeschte Jobs, geloeschte Samples).
        """
        jobs_cutoff = now - self.retention_jobs_days * 86400
        series_cutoff = now - self.retention_series_days * 86400
        with self._conn:
            jobs = self._conn.execute(
                "DELETE FROM jobs WHERE created_at < ?", (jobs_cutoff,)).rowcount
            samples = self._conn.execute(
                "DELETE FROM samples WHERE ts < ?", (series_cutoff,)).rowcount
        return jobs, samples

    def close(self) -> None:
        """Schliesst die Verbindung; mehrfacher Aufruf ist ein No-op."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
