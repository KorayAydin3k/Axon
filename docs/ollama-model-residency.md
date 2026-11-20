# Model residency during local development

The Compose Ollama service requests a 15-minute idle residency window through
`OLLAMA_KEEP_ALIVE`. This reduces repeated model loads when requests are spaced
more than Ollama's default five minutes apart. It also keeps the model's memory
allocated for longer after a request. The interval is finite and configurable.

To override it, add a duration to the repository's `.env` file, for example:

```dotenv
OLLAMA_KEEP_ALIVE=15m
```

Apply the configuration and inspect it:

```bash
docker compose config --quiet
docker compose --profile local-ai up --wait --wait-timeout 120 ollama
docker compose exec -T ollama printenv OLLAMA_KEEP_ALIVE
```

After a planning request completes, inspect the loaded model and its remaining
residency time:

```bash
docker compose exec -T ollama ollama ps
```

The timer starts when a model is used; this setting does not preload it.
The first request after startup, expiry or eviction can still be slow and may
reach the existing planning timeout. Ollama may unload models to make room for
other models. API requests that explicitly set `keep_alive` override the server
setting; Axon's planner currently leaves that parameter unset.

Keep initial-load and already-loaded measurements separate. The setting does
not establish that an observed 180-second timeout was caused solely by model
loading, and a warm result must not replace an earlier failing cold result.
Planning timeouts still use the existing bounded job retries.

To restore Ollama's documented default idle interval, set
`OLLAMA_KEEP_ALIVE=5m` in `.env` and repeat the Compose command above.

Reference: [Ollama FAQ: keeping models loaded](https://docs.ollama.com/faq#how-do-i-keep-a-model-loaded-in-memory-or-make-it-unload-immediately).
