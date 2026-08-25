<p align="right"><b>🇩🇪 Deutsch</b> | <a href="DESIGN-server-default-penalty.md">🇬🇧 English</a></p>

# Design: Overthinking-Penalty als Server-Default (Flag-gesteuert)

Stand: 2026-08-24. Read-only-Analyse des installierten Escha-SGLang-Forks
(`.venv/lib/python3.12/site-packages/sglang/`). Ziel: Server-Flags, die den
Overthinking-Penalty (arXiv 2606.00206) für alle Requests ohne explizite Felder
aktivieren; explizite Request-Felder behalten Vorrang.

## Inhaltsverzeichnis

1. [Ziel & Annahmen](#1-ziel--annahmen)
2. [Verifizierte Code-Anker](#2-verifizierte-code-anker)
3. [Komplikationen (bereits geprüft)](#3-komplikationen-bereits-geprüft)
4. [Patchpunkte (~150 Zeilen gesamt)](#4-patchpunkte-150-zeilen-gesamt) — [A: `server_args.py`](#a-srtserver_argspy-12-zeilen) · [B: `tokenizer_manager.py`](#b-srtmanagerstokenizer_managerpy-25-zeilen) · [C: neues Modul](#c-neues-modul-srtsamplingoverthinking_penalty_defaultpy-120-zeilen) · [D: `serve.sh`](#d-runtimesglangservesh-10-zeilen)
5. [Edge-Cases](#5-edge-cases)
6. [Verifikation](#6-verifikation)
7. [Rollback](#7-rollback)

---

## 1. Ziel & Annahmen

- Drei neue Server-Flags aktivieren den Penalty als Server-Standard:
  - `--default-overthinking-penalty`
  - `--penalty-lambda` (Default 2.0)
  - `--penalty-think-only` / `--penalty-always`
- Jeder Request ohne `custom_logit_processor` erhält automatisch den dill-Payload
  (mit eingebackenem `penalty_lambda`/`think_only`).
- Explizite Request-Felder (`custom_logit_processor`, `custom_params`) behalten
  immer Vorrang.
- Der Payload wird einmal beim Server-Start aus dem Tokenizer gebaut (gleicher
  Prozess, keine CLI-/Datei-Transport-Problematik).

## 2. Verifizierte Code-Anker

| Punkt | Ort | Fakt |
|---|---|---|
| Injektionspunkt | `srt/managers/tokenizer_manager.py:_validate_one_request` (Zeile 787) | Wird pro Request-Element aufgerufen (auch Batch, Zeile 1038); deckt chat/completions, completions und natives /generate ab |
| Request-Feld | `srt/managers/io_struct.py:178` | `GenerateReqInput.custom_logit_processor` ist First-Class-Feld; `_normalize_custom_logit_processor` (Zeile 538) macht aus einem String eine List bei parallel_sample>1 |
| sampling_params | `io_struct.py:142` | rohes `Dict` (oder List of Dicts) im Request; `custom_params` liegt **in** `sampling_params` |
| Chat-Pfad | `entrypoints/openai/protocol.py:777` | ChatRequest merged `custom_params` in das `sampling_params`-Dict |
| `__req__`-Injektion | `srt/managers/schedule_batch.py:570-573` | nur wenn `sampling_params.custom_params` ein Dict ist (sonst kein `__req__` → `think_only` degradiert zu always-on) |
| CLP-Gate | `tokenizer_manager.py:861-862` | `custom_logit_processor` wird abgelehnt, wenn `enable_custom_logit_processor` aus ist |
| Server-Flag | `srt/server_args.py:641` | `enable_custom_logit_processor: bool = False` — neue Flags daneben |

## 3. Komplikationen (bereits geprüft)

1. **`sampling_params` kann None sein** (Default-Request) → Injektion muss ein Dict erstellen.
2. **Parallel-Sampling**: `sampling_params` kann eine List of Dicts sein → `custom_params`
   in jedem Element setzen.
3. **ENABLE_CLP-Gate**: OTP=1 mit ENABLE_CLP=0 würde jeden Request als Fehler abweisen →
   serve.sh muss ENABLE_CLP=1 erzwingen; Payload nur bauen, wenn beide Flags gesetzt sind.
4. **Embedding-Requests** laufen durch dieselbe `_validate_one_request` → nur für
   `GenerateReqInput` injizieren (isinstance-Guard).
5. **`_normalize_custom_logit_processor`**: String→List bei parallel_sample>1 — ein
   injiziertes String-Payload ist kompatibel.
6. **Priorität**: Injektion nur bei `obj.custom_logit_processor is None`; `custom_params`
   nie überschreiben, nur bei None/fehlend ergänzen (`setdefault`).

## 4. Patchpunkte (~150 Zeilen gesamt)

Alle Pfade relativ zu `.venv/lib/python3.12/site-packages/sglang/`.

### A. `srt/server_args.py` (~12 Zeilen)

Nach Zeile 641 im Dataclass:
```python
default_overthinking_penalty: bool = False
penalty_lambda: float = 2.0
penalty_think_only: bool = True
```
In `add_cli_args`, neben dem `--enable-custom-logit-processor`-Block:
`--default-overthinking-penalty` (store_true), `--penalty-lambda` (float, 2.0),
`--penalty-think-only` (store_true, default True) + `--penalty-always`
(store_false, exklusiv).

### B. `srt/managers/tokenizer_manager.py` (~25 Zeilen)

`__init__` (nach Tokenizer-Load):
```python
self._default_penalty_payload: Optional[str] = None
if (self.server_args.default_overthinking_penalty
        and self.server_args.enable_custom_logit_processor
        and self.server_args.penalty_lambda != 0.0):
    try:
        self._default_penalty_payload = build_overthinking_penalty_payload(
            self.tokenizer, self.server_args.penalty_lambda,
            self.server_args.penalty_think_only)
        logger.info("default overthinking penalty active (lambda=%s, think_only=%s)", ...)
    except Exception as e:
        logger.warning("default overthinking penalty disabled: %s", e)
```

`_validate_one_request`, vor Zeile 861 (CLP-Validierung):
```python
if isinstance(obj, GenerateReqInput) and self._default_penalty_payload:
    if obj.custom_logit_processor is None:
        obj.custom_logit_processor = self._default_penalty_payload
    sp = obj.sampling_params
    if sp is None:
        obj.sampling_params = {"custom_params": {}}
    elif isinstance(sp, dict):
        sp.setdefault("custom_params", {})
    elif isinstance(sp, list):
        for p in sp:
            p.setdefault("custom_params", {})
```

### C. Neues Modul `srt/sampling/overthinking_penalty_default.py` (~120 Zeilen)

- `MARKERS`: 49 Marker, wortgleich mit
  `runtime/sglang/overthinking_penalty.py:85-96`
- `build_overthinking_penalty_payload(tokenizer, default_lambda, think_only) -> str`:
  - Token-Auflösung via `tokenizer.encode` + `decode`-Roundtrip; Abbruch unter 80 % resolved
  - Think-IDs via `convert_tokens_to_ids` (248068/248069), Newline via `encode("\n")` (198)
  - lokale Subklasse `OverthinkingPenaltyLogitProcessor(CustomLogitProcessor)` mit
    `DEFAULT_PENALTY_LAMBDA`/`DEFAULT_THINK_ONLY` und demselben `__call__` wie im Repo-Skript
  - Rückgabe `processor_cls.to_str()`
- Abbruch bei fehlenden Think-Tokens (kein Thinking-Modell → Fehler statt Stille)

### D. `runtime/sglang/serve.sh` (~10 Zeilen)

Neue Env-Vars neben `ENABLE_CLP`:
```bash
OTP=${OTP:-0}
PENALTY_LAMBDA=${PENALTY_LAMBDA:-2.0}
PENALTY_ALWAYS=${PENALTY_ALWAYS:-0}
OTP_ARGS=()
if [[ "$OTP" == "1" ]]; then
  [[ "$ENABLE_CLP" != "1" ]] && { echo "[serve] ERROR: OTP=1 requires ENABLE_CLP=1" >&2; exit 1; }
  OTP_ARGS+=(--default-overthinking-penalty --penalty-lambda "$PENALTY_LAMBDA")
  [[ "$PENALTY_ALWAYS" == "1" ]] && OTP_ARGS+=(--penalty-always)
fi
```
`"${OTP_ARGS[@]}"` in die exec-Zeile nach `"${CLP_ARGS[@]}"`.

## 5. Edge-Cases

- **λ=0**: Der Payload wird beim Start gar nicht gebaut (Guard `penalty_lambda != 0.0`)
  → Verhalten bit-identisch zu OTP=0. Per-Request `custom_params={"penalty_lambda": 0}`
  auf einem OTP-Server: Processor skippt bei `lam == 0.0` → Penalty aus, Budget bleibt aktiv.
- **Fehlendes `__req__`**: Injektion B erzwingt `custom_params={}` für Default-Requests →
  `schedule_batch.py` injiziert `__req__` → `think_only` funktioniert korrekt.
- **Beide Request-Felder gesetzt**: keine Injektion, `custom_params` unberührt →
  Request-Vorrang vollständig erhalten (Kette: request-`custom_params` > eingebackene
  Defaults im Payload).
- **Payload-Build-Fehlschlag** (z. B. Modell ohne Think-Tokens): try/except im `__init__`
  → Warnung, `_default_penalty_payload=None`, Server startet normal ohne Penalty.

## 6. Verifikation

1. `py_compile` der 3 gepatchten Dateien + des neuen Moduls
2. Server mit `OTP=1` starten → Log: "default overthinking penalty active"
3. Request ohne Felder → Penalty aktiv (finish_reason/Token-Zahlen vs. OTP=0-Vergleich)
4. Request mit `custom_params={"penalty_lambda": 0}` → Override greift, Penalty aus
5. Batch-/Parallel-Request (n>1) mit OTP → kein Fehler (List-Pfad)

## 7. Rollback

- Patch als `.patch`-Datei in `runtime/sglang/patches/` archivieren
- `.venv` ist eine Kopie → `git apply -R` oder Wheel-Neuinstallation stellt den Status quo wieder her
- Default `OTP=0` verändert nichts gegenüber heute
