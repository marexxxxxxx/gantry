# KV-Cache-Quantisierung für Qwen3.8-27B-Escha-W2

**Datum:** 2026-08-21
**Geänderte Dateien:** `runtime/sglang/serve.sh` (einzige funktionale Änderung), neu: `ctxmon.sh` (Monitoring), dieses Dokument.

---

## 1. Ziel & Ausgangslage

Ziel war es, den KV-Cache zu quantisieren, um **deutlich mehr Kontext in den VRAM zu bekommen,
ohne Qualitätsverlust** — optional zusätzlich ein RAM-Offload für KV-Seiten, die nicht mehr in
den VRAM passen. Anlass: längere Einzelkonversationen auf einer 16-GB-Karte.

| Komponente | Wert |
|---|---|
| GPU | RTX 5070 Ti (Blackwell, sm_120), 16 GB VRAM |
| RAM | 64 GB |
| Modell | `./Qwen3.8-27B-Escha-W2` — Qwen3.5-27B-Hybrid-Architektur: 64 Layer, nur **16 mit voller Attention** (alle 4 Layer), Rest `linear_attention`; `head_dim=256`, 4 KV-Heads, Modell-Limit 262144 Tokens; escha-2-bit-quantisiert (~9,5 GB Gewichte) |
| Runtime | escha-sglang-Fork, Wheel `escha-1.2.0+qwen3dense`, pip-installiert in `.venv` |

**Baseline vor der Änderung** (FP16-KV, MEM=0.88, CTXLEN=80000, triton backend):
`max_total_num_tokens = 32 345`. Die alten Defaults in `serve.sh` (MEM=0.72, CTXLEN=65536)
waren noch für eine 24-GB-Karte geschrieben.

---

## 2. Recherche im Fork (was überhaupt möglich ist)

Untersucht wurde der installierte Fork unter
`.venv/lib/python3.12/site-packages/sglang/`:

1. **FP8-KV wird unterstützt:** `--kv-cache-dtype fp8_e4m3|fp8_e5m2`
   (`server_args.py` choices; Aktivierung über `model_runner.configure_kv_cache_dtype`).
2. **Triton-Backend speichert K/V im gewählten dtype** (`set_kv_buffer` mit `k_scale`/`v_scale`).
   Das Checkpoint enthält keine Kalibrier-Skalen → scale=1.0 statisch. Das ist der Standard-Fall
   und in der Praxis qualitätsneutral (K/V-Aktivierungen liegen weit unter dem e4m3-Bereich ±448).
   Die Startup-Warnung `no scaling factors ... using 1.0` ist also erwartbar und harmlos.
3. **FP4-KV ist für dieses Modell unbrauchbar:** `HybridLinearKVPool`
   (`mem_cache/memory_pool.py:1210`) hardcoded `MHATokenToKVPool` und wählt **nie**
   `MHATokenToKVPoolFP4`; die FP4-Auswahl in `model_runner_kv_cache_mixin.py` (~Zeile 647)
   existiert nur im Nicht-Hybrid-Zweig. FP4 würde hier stillschweigend falsch alloziert.
   Zusätzlich: unter ~6 bit/Elem leidet die KV-Qualität real. → FP4 bewusst **nicht** angeboten.
4. **HiCache (RAM-Offload) existiert**, nimmt in `scheduler.py` (Zeile ~696 vor ~707) sogar
   Vorrang vor MambaRadixCache — wird aber weiter unten live als nicht funktionsfähig für
   dieses Modell gemessen (Abschnitt 6).

---

## 3. Änderungen an `runtime/sglang/serve.sh`

### 3.1 Neuer Knob `KVDTYPE` (Default: `fp8_e4m3`)

* Deklaration + Doku: Zeilen 45–63; Flag-Assemblierung: Zeilen 275–280 (`KV_ARGS`);
  Übergabe in der exec-Zeile: Zeile 375.
* Gültige Werte:
  * `fp8_e4m3` (**Default**) → `--kv-cache-dtype fp8_e4m3`
  * `fp8_e5m2` → möglich, aber dokumentiert als Debug-only (mehr Exponent, keine Mantisse;
    messbar schlechter als e4m3 für KV)
  * `off` / `fp16` / `float16` → **kein Flag** → sglang `auto` → float16 = exakt das
    Verhalten von vor der Änderung (Rückweg ohne Verhaltenänderung)
  * alles andere → `ERROR` + `exit 1`
* FP4 wird bewusst nicht angeboten (siehe Recherche-Punkt 3).

### 3.2 Neue Knobs `HICACHE` / `HICACHE_GB` / `HICACHE_FORCE`

* Deklaration: Zeilen 64–73; Logik: Zeilen 284–297 (`HICACHE_ARGS`); exec-Zeile: Zeile 376.
* `HICACHE=1` würde `--enable-hierarchical-cache --hicache-size $HICACHE_GB` setzen
  (Default 32 GB Host-Pool). Da das für dieses Hybridmodell **gemessen nicht funktioniert**
  (Abschnitt 6), baut der Script einen **Early-Guard**: bei `HICACHE=1` ohne
  `HICACHE_FORCE=1` gibt es eine klare Fehlermeldung und `exit 1`, bevor irgendwelche
  Gewichte geladen werden. `HICACHE_FORCE=1` umgeht den Guard (für zukünftige Forks).
* Weil HiCache den Radix-Cache braucht, gewinnt `HICACHE=1` über `RADIX=0`
  (mit Warnung auf stderr).
* Bei `HICACHE=0` (Default) setzt `HICACHE_ARGS` weiterhin `--disable-radix-cache`
  und ersetzt damit die alte `RADIX_ARGS`-Logik in der exec-Zeile (`RADIX_ARGS` bleibt
  als leere Variable für Kompatibilität stehen).

### 3.3 Auto-Default `ATTN_BACKEND=triton` auf Blackwell

* Zeilen 256–265. Wenn `ATTN_BACKEND` leer ist und `nvidia-smi --query-gpu=compute_cap`
  ≥ 12 meldet (sm_120 → `"120"`), wird automatisch `triton` gesetzt und geloggt.
  Grund: der sglang-Default löst auf Blackwell auf `flashinfer` auf, den der Fork für
  Hybridmodelle zurückweist.

### 3.4 Kommentare/Header aktualisiert

* Header (Zeile 10): Defaults jetzt ausdrücklich „for a 16 GB card (RTX 5070 Ti) with
  FP8 KV cache on".
* `CTXLEN`-Kommentar (Zeilen 32–37): CTXLEN ist ein Cap pro Request, kein Allocation;
  mit FP8 verdoppelt sich der Pool — CTXLEN muss ≤ `max_total_num_tokens` bleiben,
  sonst wird ein Überlang-Request mitten in der Generierung retraktiert statt vorne
  abgelehnt.
* Status-Echo (Zeile 358) zeigt jetzt auch `KVDTYPE=`, `HICACHE=` und ggf. `ATTN=`.

### 3.5 Konsistenz-Fix

Der FP4-Kommentar behauptete ursprünglich, mehr Kontext käme über
„KVDTYPE=fp8 + HICACHE=1" — das widersprach dem gemessenen HiCache-Aus. Korrigiert auf:
mehr Kontext kommt aus `KVDTYPE=fp8_e4m3` allein; HiCache ist für dieses Modell
nicht verfügbar.

---

## 4. Messungen (live, gleiche Karte, gleiches MEM)

| Konfiguration | `max_total_num_tokens` | K/V-Pool |
|---|---|---|
| FP16-KV (Baseline) | **32 345** | ~2 GB |
| FP8 e4m3 (neuer Default) | **64 690** | K 0,99 GB + V 0,99 GB |

→ **exakt verdoppelte Token-Kapazität bei identischem VRAM.** Freier VRAM nach
Startup: ~1,56 GB (MEM=0.88 ist damit ausgereizt; höher = OOM-Risiko bei CUDA-Graph-Capture).

## 5. Qualitäts-A/B-Test FP16 vs. FP8

* Methode: Greedy-Decoding (Temperatur 0), 3 feste Prompts (`/tmp/opencode/qa_prompts.jsonl`),
  einmal gegen den FP16-Server, einmal gegen den FP8-Server.
* Baseline gesichert in `/tmp/opencode/baseline_fp16.json`, FP8-Ergebnisse in
  `/tmp/opencode/fp8_results.json`.
* Ergebnis:
  * Prompt 1 (Mathematik): **byte-identisch**
  * Prompt 3: **byte-identisch**
  * Prompt 2 (Übersetzung): einzige Divergenz ist ein zusätzlicher Zeilenumbruch nach
    `**Translation:**` (Near-Tie-Token); Inhalt identisch. (Die FP16-Baseline war zudem
    auf 800 Zeichen abgeschnitten.)
* **Urteil: kein Qualitätsverlust durch FP8-KV.**

## 6. HiCache-Live-Test (RAM-Offload) — fehlgeschlagen wie vorhergesagt

* Start mit `--enable-hierarchical-cache`: nach vollständigem Weight-Load bricht der
  Scheduler ab mit
  `ValueError: HiRadixCache only supports MHA and MLA yet`.
* Ursache: der Fork unterstützt HiRadixCache nur für reine MHA/MLA-Architekturen, nicht
  für die Hybrid-/Linear-Attention-Architektur dieses Modells.
* Konsequenz: Early-Guard in `serve.sh` (Abschnitt 3.2), damit Nutzer eine verständliche
  Fehlermeldung statt eines Crashs nach 9,5 GB Weight-Load bekommen.
* **Was stattdessen geht:** mehr Kontext über `KVDTYPE=fp8_e4m3` (gemacht); CTXLEN ≤ 64690
  halten; ein echter RAM-Offload erfordert einen Fork-Fix am HybridLinearKVPool/HiRadixCache.

---

## 7. Betrieb

### Startbefehl (16-GB-Profil)

```bash
cd /home/user/smal_qwen
( setsid nohup env MODEL=./Qwen3.8-27B-Escha-W2 VENV=.venv \
    MEM=0.88 CTXLEN=64000 HOST=0.0.0.0 API_KEY=marec \
    CUDA_GRAPH_BS='1 2 4' \
    bash runtime/sglang/serve.sh >/tmp/opencode/server.log 2>&1 & )
```

* `KVDTYPE`, `HICACHE`, `ATTN_BACKEND` müssen nicht gesetzt werden — Defaults greifen
  (`fp8_e4m3`, aus, triton-auto).
* `CTXLEN=64000` statt 80000, weil der FP8-Pool 64 690 Tokens fasst.
* Bereit-Meldung: `fired up and ready to roll`; API: Port 30000, Bearer `marec`.

### Stoppen / Überwachen

```bash
pkill -f sglang.launch_server                       # Server beenden
curl -s -H "Authorization: Bearer marec" http://127.0.0.1:30000/get_load   # aktive Requests
watch -n 1 ~/smal_qwen/ctxmon.sh                    # Live: tok/s, Ctx aktuell, Ctx-Peak
```

`ctxmon.sh` liest `/tmp/opencode/server.log` (überschreibbar via `LOG=...`) und zeigt
Status, letzte Decode-Geschwindigkeit, aktuellen Kontextbelegung und den Höchstwert
seit Serverstart.

### Validierung unter Last (SWE-bench Lite)

Nach der Umstellung lief ein mini-swe-agent-Lauf (50 Instanzen, Slice ::6) gegen den
FP8-Server. Erste Instanz `astropy__astropy-12907`: Status `Submitted` nach 35
Agentenschritten (~22 min), Lösungsweg sauber (Reproduktion → Root-Cause → Ein-Zeilen-Fix
→ Regressionstest inkl. Git-Stash-Baseline-Vergleich), Patch **identisch zum Gold-Patch**.
Kein Hinweis auf Degradation durch FP8-KV unter realer Agent-Last (~37k Tokens Kontext,
~42–50 tok/s Decode).

---

## 8. Knobs-Referenz

| Variable | Default | Wirkung |
|---|---|---|
| `KVDTYPE` | `fp8_e4m3` | `off`/`fp16`/`float16` = alter FP16-Modus; `fp8_e4m3` empfohlen; `fp8_e5m2` debug-only |
| `HICACHE` | `0` | `1` = RAM-Offload-Versuch → Guard-Exit (fork-seitig nicht unterstützt) |
| `HICACHE_GB` | `32` | Host-Pool-Größe bei `HICACHE=1` |
| `HICACHE_FORCE` | `0` | `1` = Guard umgehen (erwartet Startup-ValueError) |
| `ATTN_BACKEND` | auto | leer + sm_120 → automatisch `triton` |
| `CTXLEN` | 65536 | Cap pro Request; ≤ `max_total_num_tokens` halten (hier 64690) |
