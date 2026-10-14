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
        conn.commit()
        self._conn = conn

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
        Token-Summen, TTFT-/Dauer-Perzentile in Sekunden; nichts gemessen ->
        None). ``buckets`` mittelt die Samples je ``bucket_s``-Fenster
        (Start = floor(ts / bucket_s) * bucket_s); je Spalte werden nur
        gefuellte Werte gemittelt, Spalten ohne Wert bleiben None;
        ``available`` ist der Anteil verfuegbarer Samples. ``bucket_s`` muss
        positiv sein (sonst ValueError statt stiller NULL-Buckets).
        """
        if bucket_s <= 0:
            raise ValueError(f"bucket_s muss positiv sein: {bucket_s}")
        zeile = self._conn.execute(
            "SELECT COUNT(*),"
            " COALESCE(SUM(status = 'done'), 0),"
            " COALESCE(SUM(status = 'aborted'), 0),"
            " COALESCE(SUM(status = 'error'), 0),"
            " SUM(prompt_tokens), SUM(completion_tokens)"
            " FROM jobs WHERE created_at >= ? AND created_at <= ?",
            (from_ts, to_ts)).fetchone()
        count, done, aborted, error, tokens_in, tokens_out = zeile

        ttft = [row[0] for row in self._conn.execute(
            "SELECT first_token_at - created_at FROM jobs"
            " WHERE created_at >= ? AND created_at <= ?"
            " AND first_token_at IS NOT NULL", (from_ts, to_ts))]
        duration = [row[0] for row in self._conn.execute(
            "SELECT finished_at - created_at FROM jobs"
            " WHERE created_at >= ? AND created_at <= ?"
            " AND finished_at IS NOT NULL", (from_ts, to_ts))]

        buckets = [{
            "ts": row[0],
            "available": row[1],
            "running": row[2],
            "waiting": row[3],
            "throughput": row[4],
            "token_usage": row[5],
            "gpu_util": row[6],
            "vram_used_mb": row[7],
            "vram_total_mb": row[8],
        } for row in self._conn.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS fenster,"
            " AVG(available), AVG(running), AVG(waiting), AVG(throughput),"
            " AVG(token_usage), AVG(gpu_util), AVG(vram_used_mb),"
            " AVG(vram_total_mb)"
            " FROM samples WHERE ts >= ? AND ts <= ?"
            " GROUP BY fenster ORDER BY fenster",
            (bucket_s, bucket_s, from_ts, to_ts))]

        return {
            "overview": {
                "count": count, "done": done, "aborted": aborted,
                "error": error, "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "ttft": {"p50": percentile(ttft, 50),
                         "p95": percentile(ttft, 95)},
                "duration": {"p50": percentile(duration, 50),
                             "p95": percentile(duration, 95)},
            },
            "buckets": buckets,
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
