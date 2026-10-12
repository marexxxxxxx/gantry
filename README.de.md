<p align="right"><b>🇩🇪 Deutsch</b> | <a href="README.md">🇬🇧 English</a></p>

# smal_qwen — Escha-Runtime für Qwen3.8-27B-Escha-W2

Dieses Repository betreibt das 2-Bit-quantisierte Reasoning-Modell
[`EschaLabs/Qwen3.8-27B-Escha-W2`](https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2)
(~10 GB Gewichte) auf einer einzelnen Consumer-GPU via dem `escha`-SGLang-Fork —
und ergänzt eine **training-freie Overthinking-Penalty**, die quantisierte
Reasoning-Modelle daran hindert, sich aus bereits gefundenen Antworten
hinauszureden.

**Schnellster Weg zu einem laufenden Server mit aktivierter Penalty:**

```bash
# 0) einmalig: venv anlegen & installieren — vollständige Anleitung: runtime/sglang/INSTALL.de.md
python3.12 -m venv .venv && source .venv/bin/activate
pip install "torch==2.9.*" --index-url https://download.pytorch.org/whl/cu128
pip install ./runtime/sglang/escha-*.whl

# 1) Penalty-Payload einmalig bauen
python runtime/sglang/overthinking_penalty.py --model ./Qwen3.8-27B-Escha-W2 > otp.json

# 2) Server mit Penalty als Serverdefault starten
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 bash runtime/sglang/serve.sh
```

Details, GPU-Profile und Troubleshooting: [`runtime/sglang/INSTALL.de.md`](runtime/sglang/INSTALL.de.md).

## Inhaltsverzeichnis

1. [Repository-Aufbau](#1-repository-aufbau)
2. [Dokumentationskarte](#2-dokumentationskarte)
3. [Die Overthinking-Penalty in einem Absatz](#3-die-overthinking-penalty-in-einem-absatz)
4. [Credits](#4-credits)

---

## 1. Repository-Aufbau

| Pfad | Inhalt |
|---|---|
| [`runtime/sglang/`](runtime/sglang/) | **Die eigentliche Laufzeitumgebung**: escha-Wheel + SGLang-Fork, `serve.sh`-Launcher, Payload-Builder (`overthinking_penalty.py`, `thinking_budget.py`), Benchmark-Wrapper, Server-Default-Patch, sämtliche Dokumentation |
| [`Qwen3.8-27B-Escha-W2/`](Qwen3.8-27B-Escha-W2/) | Heruntergeladene Modellgewichte (separates HF-Repo, nicht Teil dieser Codebasis) |
| [`docs/`](docs/) | Technische Vertiefungen: FP8-KV-Cache-Quantisierung |
| [`slotmon/`](slotmon/) | **GPU-Slot-Monitor**: liest das Server-Log passiv mit und zeigt, welcher Auftrag den Slot hält (`monitor.sh start` → `http://0.0.0.0:8090`) |
| `ctxmon.sh` | Live-Monitor für den Server: tok/s, aktuelle Kontextbelegung, Kontext-Peak |

## 2. Dokumentationskarte

Alle Dokumentation existiert auf Deutsch und Englisch; jede Seite hat oben einen
Flaggen-Umschalter.

| Thema | Deutsch | English |
|---|---|---|
| Runtime-Überblick & Funktionsweise | [runtime/sglang/README.de.md](runtime/sglang/README.de.md) | [runtime/sglang/README.md](runtime/sglang/README.md) |
| Installation, GPU-Profile, Troubleshooting | [runtime/sglang/INSTALL.de.md](runtime/sglang/INSTALL.de.md) | [runtime/sglang/INSTALL.md](runtime/sglang/INSTALL.md) |
| Penalty-Bedienungsanleitung (beide Aktivierungswege, Rezepte, Benchmarks) | [runtime/sglang/USAGE-penalty.de.md](runtime/sglang/USAGE-penalty.de.md) | [runtime/sglang/USAGE-penalty.md](runtime/sglang/USAGE-penalty.md) |
| Design-Doku: Server-Default-Patch (`OTP=1`) | [runtime/sglang/DESIGN-server-default-penalty.de.md](runtime/sglang/DESIGN-server-default-penalty.de.md) | [runtime/sglang/DESIGN-server-default-penalty.md](runtime/sglang/DESIGN-server-default-penalty.md) |
| FP8-KV-Cache: 2× Kontext, Qualitäts-A/B, HiCache-Urteil | [docs/kv-cache-quantisierung.md](docs/kv-cache-quantisierung.md) | [docs/kv-cache-quantization.md](docs/kv-cache-quantization.md) |

## 3. Die Overthinking-Penalty in einem Absatz

Post-Training-Quantisierung beschädigt nicht primär die Antwortqualität, sondern das
*Terminierungsverhalten* der Gedankenkette: quantisierte Reasoning-Modelle sampeln
Zögerungsmarker („Wait“, „But“, „Alternatively“) noch lange nach der bereits gefundenen
richtigen Antwort, öffnen neue Denkzweige und verbrennen ihr Token-Budget
([arXiv:2606.00206](https://arxiv.org/abs/2606.00206), Meta FAIR). Dieses Repo setzt den
Fix des Papers als Custom-Logit-Processor um: an jedem Dekodierschritt wird von den Logits
der 49 Markertokens λ abgezogen — kein Training, kein Extra-Forward-Pass. Aktivierung
serverseitig für alle Requests mit `OTP=1` oder pro Request via
`custom_logit_processor`-Payload; Request-Overrides gewinnen immer.

## 4. Credits

- Modell, Quantisierungsmethode, Wheel und Original-Launcher:
  [EschaLabs](https://huggingface.co/EschaLabs).
- Overthinking-Penalty-Methode: Lotfi, Kirichenko, Li, Liu (Meta FAIR),
  *"Quantized Reasoning Models Think They Need to Think Longer, but They Do Not"*
  ([arXiv:2606.00206](https://arxiv.org/abs/2606.00206)).
- Die Adaptionen in diesem Repo (Payload-Builder, Server-Default-Patch, Doku) sind
  lokale Arbeit; Fehler gehen auf uns.
