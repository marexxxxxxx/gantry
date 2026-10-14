"""Job-Registry (nur RAM) fuer den aiserver-monitor.

Haelt je Request einen Job mit Status, Zeiten, Tokenzaehlern und den letzten
Zeichen von Antwort/Denken (TextRing). Kein I/O, kein asyncio. Vorschau- und
Prompttexte werden nie persistiert.
"""

from dataclasses import dataclass
from enum import StrEnum

# prompt_start wandert in UI und History; Konvention: die ersten 100 Zeichen
# (entspricht dem Config-Default prompt_start_chars).
PROMPT_START_CHARS = 100


class JobStatus(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"
    ABORTED = "aborted"
    ERROR = "error"


ACTIVE_STATUSES = frozenset((JobStatus.WAITING, JobStatus.RUNNING))


class TextRing:
    """Ringpuffer fuer Text: haelt immer die letzten ``cap`` Zeichen.

    Gezaehlt werden Zeichen (Codepoints), nicht Bytes; Mehrbyte-UTF-8 bleibt
    damit auch an Puffergrenzen intakt. ``cap <= 0`` liefert immer "".
    """

    def __init__(self, cap: int):
        self.cap = cap
        self._text = ""

    def add(self, s: str) -> None:
        if s and self.cap > 0:
            self._text = (self._text + s)[-self.cap:]

    def text(self) -> str:
        return self._text


@dataclass
class Job:
    """Ein Request von der Annahme bis zum Abschluss (nur RAM)."""

    rid: str
    source: str
    model: str
    full_prompt: str
    prompt_start: str
    created_at: float
    content_ring: TextRing
    reasoning_ring: TextRing
    status: JobStatus = JobStatus.WAITING
    first_token_at: float | None = None
    finished_at: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    fallback_completion_tokens: int = 0
    error: str | None = None


class JobRegistry:
    """Alle Jobs im Speicher; Schluessel ist die rid.

    ``preview_max_chars`` begrenzt die Textvorschauen je Kanal. Zeiten kommen
    von aussen herein (keine Uhr im Modul), unbekannte rids loesen KeyError aus.
    """

    def __init__(self, preview_max_chars: int):
        self.preview_max_chars = preview_max_chars
        self._jobs: dict[str, Job] = {}

    def register(self, rid: str, *, source: str, model: str, full_prompt: str,
                 now: float) -> Job:
        """Legt einen Job im Status waiting an; gleiche rid ersetzt den Alten."""
        job = Job(
            rid=rid,
            source=source,
            model=model,
            full_prompt=full_prompt,
            prompt_start=full_prompt[:PROMPT_START_CHARS],
            created_at=now,
            content_ring=TextRing(self.preview_max_chars),
            reasoning_ring=TextRing(self.preview_max_chars),
        )
        self._jobs[rid] = job
        return job

    def get(self, rid: str) -> Job | None:
        return self._jobs.get(rid)

    def _require(self, rid: str) -> Job:
        job = self._jobs.get(rid)
        if job is None:
            raise KeyError(f"Unbekannte rid: {rid}")
        return job

    def note_running(self, rid: str, now: float) -> None:
        """Erstes Token gesehen: Status running, TTFT ab created_at."""
        job = self._require(rid)
        if job.status is JobStatus.WAITING:
            job.status = JobStatus.RUNNING
        if job.first_token_at is None:
            job.first_token_at = now

    def add_text(self, rid: str, *, content: str | None = None,
                 reasoning: str | None = None) -> None:
        """Haengt Deltas an die Ringe; nicht-leere Deltas zaehlen als Fallback."""
        job = self._require(rid)
        if content:
            job.content_ring.add(content)
            job.fallback_completion_tokens += 1
        if reasoning:
            job.reasoning_ring.add(reasoning)
            job.fallback_completion_tokens += 1

    def finish(self, rid: str, status: JobStatus, *, prompt_tokens: int | None = None,
               completion_tokens: int | None = None, error: str | None = None,
               now: float) -> None:
        """Schliesst den Job ab; usage-Werte schlagen die Fallback-Zaehlung.

        First-wins: ist der Job schon terminal (done/aborted/error), passiert
        nichts mehr. Damit kann weder ein Relay-close einen Abbruch
        ueberschreiben noch umgekehrt.
        """
        job = self._require(rid)
        if job.status not in ACTIVE_STATUSES:
            return
        job.status = status
        job.finished_at = now
        if prompt_tokens is not None:
            job.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            job.completion_tokens = completion_tokens
        if error is not None:
            job.error = error

    def snapshot(self) -> list[dict]:
        """Alle Jobs als dicts: aktive zuerst, je Gruppe nach created_at."""
        sortiert = sorted(
            self._jobs.values(),
            key=lambda job: (0 if job.status in ACTIVE_STATUSES else 1,
                             job.created_at),
        )
        return [job_to_dict(job) for job in sortiert]


def job_to_dict(job: Job) -> dict:
    """Serialisiert einen Job fuer Snapshot/SSE/History (Feldnamen sind stabil).

    ``completion_tokens`` ist die usage-Angabe aus ``finish``; fehlt sie, gilt
    die gezaehlte Fallback-Zahl. ``prompt_tokens`` hat keinen Fallback und
    bleibt ohne usage None ("nicht gemessen" heisst None, nie 0). ``tok_per_s``
    ist die Dekodierrate ab dem ersten Token; ohne TTFT/Ende bleibt sie None.
    """
    completion_tokens = job.completion_tokens
    if completion_tokens is None and job.fallback_completion_tokens > 0:
        completion_tokens = job.fallback_completion_tokens

    ttft_ms = None
    if job.first_token_at is not None:
        ttft_ms = (job.first_token_at - job.created_at) * 1000.0

    duration_ms = None
    if job.finished_at is not None:
        duration_ms = (job.finished_at - job.created_at) * 1000.0

    tok_per_s = None
    if (completion_tokens is not None and job.first_token_at is not None
            and job.finished_at is not None
            and job.finished_at > job.first_token_at):
        tok_per_s = completion_tokens / (job.finished_at - job.first_token_at)

    return {
        "rid": job.rid,
        "source": job.source,
        "model": job.model,
        "full_prompt": job.full_prompt,
        "prompt_start": job.prompt_start,
        "status": job.status.value,
        "created_at_ms": int(round(job.created_at * 1000.0)),
        "ttft_ms": ttft_ms,
        "duration_ms": duration_ms,
        "prompt_tokens": job.prompt_tokens,
        "completion_tokens": completion_tokens,
        "tok_per_s": tok_per_s,
        "content_preview": job.content_ring.text(),
        "reasoning_preview": job.reasoning_ring.text(),
        "error": job.error,
    }
