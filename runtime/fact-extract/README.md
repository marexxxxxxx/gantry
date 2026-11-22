# fact-extract (CPU)

Kleines Extraktionsmodell fuer Hindsight: Qwen3-4B-Instruct-2507, Q4_K_M,
`num_gpu = 0` (reine CPU), 32k Kontext.

## Anlegen / Aktualisieren

```bash
ollama pull qwen3:4b-instruct-2507-q4_K_M
ollama create fact-extract-cpu -f /home/user/gantry/runtime/fact-extract/Modelfile
```

Der Provider `providers/fact-extract.toml` zeigt auf Ollama `127.0.0.1:11434`
und den Modellnamen `fact-extract-cpu:latest`. Gantry routet den Client-Namen
`fact-extract` auf dieses Modell.

## Test

```bash
curl -s http://127.0.0.1:11434/api/ps
curl -s http://127.0.0.1:30000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"fact-extract","messages":[{"role":"user","content":"Say OK"}],"max_tokens":8}'
```

`ollama ps` muss `100% CPU` zeigen.
