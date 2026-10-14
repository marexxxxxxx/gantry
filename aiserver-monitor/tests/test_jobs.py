import unittest

from aiserver_monitor.jobs import (
    MAX_BEENDETE_JOBS,
    JobRegistry,
    JobStatus,
    TextRing,
    job_to_dict,
)


class TestTextRing(unittest.TestCase):
    def test_haelt_die_letzten_zeichen(self):
        ring = TextRing(5)
        ring.add("abc")
        self.assertEqual(ring.text(), "abc")
        ring.add("def")
        self.assertEqual(ring.text(), "bcdef")

    def test_leerer_ring_und_leeres_add(self):
        ring = TextRing(4)
        self.assertEqual(ring.text(), "")
        ring.add("")
        self.assertEqual(ring.text(), "")

    def test_add_laenger_als_cap(self):
        ring = TextRing(3)
        ring.add("abcdef")
        self.assertEqual(ring.text(), "def")

    def test_mehrbyte_zeichen_bleiben_erhalten(self):
        # Zeichen zaehlen, nicht Bytes: 'gru' + U+0308 + 'eszett'
        ring = TextRing(3)
        ring.add("gru\u0308\u00df")
        self.assertEqual(ring.text(), "u\u0308\u00df")


class TestJobRegistry(unittest.TestCase):
    def test_lebenszyklus_und_ring(self):
        reg = JobRegistry(preview_max_chars=5)
        j = reg.register("a1f3", source="10.0.0.9", model="m",
                         full_prompt="Hallo Welt und mehr", now=1000.0)
        self.assertIs(j.status, JobStatus.WAITING)
        self.assertIs(reg.get("a1f3"), j)
        reg.note_running("a1f3", now=1002.0)
        self.assertIs(reg.get("a1f3").status, JobStatus.RUNNING)
        reg.add_text("a1f3", content="abcdef")
        reg.add_text("a1f3", reasoning="DENK")
        d = job_to_dict(reg.get("a1f3"))
        self.assertEqual(d["rid"], "a1f3")
        self.assertEqual(d["source"], "10.0.0.9")
        self.assertEqual(d["model"], "m")
        self.assertEqual(d["content_preview"], "bcdef")        # letzte 5 Zeichen
        self.assertEqual(d["reasoning_preview"], "DENK")
        self.assertEqual(d["ttft_ms"], 2000.0)
        reg.finish("a1f3", JobStatus.DONE, prompt_tokens=11, completion_tokens=7, now=1009.0)
        d = job_to_dict(reg.get("a1f3"))
        self.assertEqual(d["status"], "done")
        self.assertEqual(d["completion_tokens"], 7)
        self.assertEqual(d["prompt_tokens"], 11)
        self.assertEqual(d["prompt_start"], "Hallo Welt und mehr")  # erster Teil, max 100
        self.assertEqual(d["full_prompt"], "Hallo Welt und mehr")
        self.assertEqual(d["created_at_ms"], 1000000)
        self.assertEqual(d["duration_ms"], 9000.0)
        self.assertEqual(d["tok_per_s"], 1.0)                  # 7 Tokens in 7 s Dekodierzeit
        self.assertIsNone(d["error"])

    def test_fallback_zaehlung_und_none(self):
        # ohne usage: completion_tokens == Anzahl nicht-leerer content/reasoning-Deltas
        # vor finish: prompt_tokens ist None, nicht 0
        reg = JobRegistry(preview_max_chars=5)
        reg.register("b2", source="10.0.0.1", model="m", full_prompt="prompt", now=50.0)
        d = job_to_dict(reg.get("b2"))
        self.assertEqual(d["status"], "waiting")
        self.assertEqual(d["content_preview"], "")
        self.assertEqual(d["reasoning_preview"], "")
        self.assertIsNone(d["prompt_tokens"])
        self.assertIsNone(d["completion_tokens"])
        self.assertIsNone(d["ttft_ms"])
        self.assertIsNone(d["duration_ms"])
        self.assertIsNone(d["tok_per_s"])
        self.assertIsNone(d["error"])
        reg.add_text("b2", content="")                      # leeres Delta zaehlt nicht
        reg.add_text("b2", content="x", reasoning="y")      # zwei nicht-leere Deltas
        d = job_to_dict(reg.get("b2"))
        self.assertEqual(d["completion_tokens"], 2)         # Fallback-Zaehlung
        self.assertIsNone(d["prompt_tokens"])
        reg.finish("b2", JobStatus.DONE, now=55.0)
        d = job_to_dict(reg.get("b2"))
        self.assertEqual(d["completion_tokens"], 2)         # ohne usage bleibt die Zaehlung
        self.assertIsNone(d["prompt_tokens"])               # kein Fallback fuer prompt
        self.assertEqual(d["duration_ms"], 5000.0)

    def test_usage_schlaegt_zaehlung(self):
        reg = JobRegistry(preview_max_chars=5)
        reg.register("c3", source="s", model="m", full_prompt="p", now=0.0)
        reg.note_running("c3", now=1.0)
        reg.add_text("c3", content="a")
        reg.add_text("c3", content="b")
        reg.finish("c3", JobStatus.DONE, completion_tokens=99, now=2.0)
        d = job_to_dict(reg.get("c3"))
        self.assertEqual(d["completion_tokens"], 99)        # usage schlaegt Zaehlung
        self.assertIsNone(d["prompt_tokens"])               # nicht geliefert bleibt None

    def test_note_running_setzt_ttft_nur_einmal(self):
        reg = JobRegistry(preview_max_chars=5)
        reg.register("d4", source="s", model="m", full_prompt="p", now=100.0)
        reg.note_running("d4", now=101.0)
        reg.note_running("d4", now=105.0)                   # zweiter Aufruf zaehlt nicht
        self.assertEqual(job_to_dict(reg.get("d4"))["ttft_ms"], 1000.0)

    def test_fehler_und_abbruch(self):
        reg = JobRegistry(preview_max_chars=5)
        reg.register("e5", source="s", model="m", full_prompt="p", now=10.0)
        reg.note_running("e5", now=11.0)
        reg.finish("e5", JobStatus.ERROR, error="upstream weg", now=12.0)
        d = job_to_dict(reg.get("e5"))
        self.assertEqual(d["status"], "error")
        self.assertEqual(d["error"], "upstream weg")
        self.assertEqual(d["ttft_ms"], 1000.0)
        reg.register("f6", source="s", model="m", full_prompt="p", now=20.0)
        reg.finish("f6", JobStatus.ABORTED, now=20.5)
        d = job_to_dict(reg.get("f6"))
        self.assertEqual(d["status"], "aborted")
        self.assertIsNone(d["ttft_ms"])                     # kein erstes Token gesehen
        self.assertIsNone(d["error"])

    def test_prompt_start_max_100_zeichen(self):
        reg = JobRegistry(preview_max_chars=5)
        lang = "x" * 150
        reg.register("g7", source="s", model="m", full_prompt=lang, now=0.0)
        d = job_to_dict(reg.get("g7"))
        self.assertEqual(d["prompt_start"], "x" * 100)
        self.assertEqual(d["full_prompt"], lang)            # voller Prompt nur im RAM

    def test_snapshot_aktive_zuerst_nach_created_at(self):
        reg = JobRegistry(preview_max_chars=10)
        for rid, now in (("a", 1.0), ("b", 2.0), ("c", 3.0)):
            reg.register(rid, source="s", model="m", full_prompt="p", now=now)
        reg.finish("c", JobStatus.DONE, now=4.0)
        self.assertEqual([d["rid"] for d in reg.snapshot()], ["a", "b", "c"])
        reg.finish("a", JobStatus.ABORTED, now=5.0)
        # aktive zuerst (b), danach abgeschlossene nach created_at (a vor c)
        self.assertEqual([d["rid"] for d in reg.snapshot()], ["b", "a", "c"])

    def test_get_unbekannte_rid(self):
        reg = JobRegistry(preview_max_chars=5)
        self.assertIsNone(reg.get("gibt-es-nicht"))

    def test_finish_ist_first_wins(self):
        # R-B: der erste Abschluss gewinnt; Cancel-Route und Relay-close
        # duerfen sich nicht gegenseitig ueberschreiben.
        reg = JobRegistry(preview_max_chars=5)
        reg.register("h8", source="s", model="m", full_prompt="p", now=10.0)
        reg.finish("h8", JobStatus.ABORTED, now=11.0)
        reg.finish("h8", JobStatus.DONE, completion_tokens=9, now=12.0)
        d = job_to_dict(reg.get("h8"))
        self.assertEqual(d["status"], "aborted")
        self.assertEqual(d["duration_ms"], 1000.0)     # erstes finish zaehlt
        self.assertIsNone(d["completion_tokens"])      # spaeteres finish ignoriert
        self.assertIsNone(d["error"])

    def test_finish_nach_error_bleibt_error(self):
        reg = JobRegistry(preview_max_chars=5)
        reg.register("i9", source="s", model="m", full_prompt="p", now=10.0)
        reg.finish("i9", JobStatus.ERROR, error="upstream weg", now=11.0)
        reg.finish("i9", JobStatus.DONE, now=12.0)
        d = job_to_dict(reg.get("i9"))
        self.assertEqual((d["status"], d["error"]), ("error", "upstream weg"))
        self.assertEqual(d["duration_ms"], 1000.0)

    def test_register_prunt_alte_beendete_jobs(self):
        # Mehr als MAX_BEENDETE_JOBS abgeschlossene Jobs: nur die neuesten
        # bleiben; der alte aktive Job wird nie entfernt.
        self.assertEqual(MAX_BEENDETE_JOBS, 200)
        reg = JobRegistry(preview_max_chars=5)
        reg.register("alt-aktiv", source="s", model="m", full_prompt="p",
                     now=0.0)                           # aeltester Eintrag
        for n in range(1, 206):
            reg.register(f"job-{n}", source="s", model="m", full_prompt="p",
                         now=float(n))
            reg.finish(f"job-{n}", JobStatus.DONE, now=float(n) + 0.5)
        # Noch ein Job: die Prune laeuft in register() und zieht das Limit
        # strikt ein (nach dem letzten finish lag der Zaehler bei 201).
        reg.register("aktiv-2", source="s", model="m", full_prompt="p",
                     now=1000.0)

        beendete = [d for d in reg.snapshot()
                    if d["status"] not in ("waiting", "running")]
        self.assertEqual(len(beendete), MAX_BEENDETE_JOBS)
        self.assertIsNone(reg.get("job-1"))             # aelteste sind weg
        self.assertIsNone(reg.get("job-5"))
        self.assertIsNotNone(reg.get("job-6"))
        self.assertIsNotNone(reg.get("job-205"))
        self.assertIsNotNone(reg.get("alt-aktiv"))      # aktiv bleibt
        self.assertIs(reg.get("alt-aktiv").status, JobStatus.WAITING)

    def test_aktive_jobs_zaehlen_nicht_gegen_das_limit(self):
        reg = JobRegistry(preview_max_chars=5)
        for n in range(250):                            # viele aktive Jobs
            reg.register(f"aktiv-{n}", source="s", model="m",
                         full_prompt="p", now=float(n))
        self.assertEqual(len(reg.snapshot()), 250)      # nichts geprunnt


if __name__ == "__main__":
    unittest.main()
