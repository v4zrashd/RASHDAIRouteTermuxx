# V4Z AI Route

A small **local AI gateway** for Termux. It exposes one OpenAI-compatible endpoint on your phone and routes each chat request to **your own** configured AI providers — if one provider fails, the next one is tried automatically.

Built by **V4Z RASHD** — Telegram: [@rashdteem](https://t.me/rashdteem)

> Honest scope: this tool is a routing/fallback layer, not a source of free AI. You bring your own provider accounts, base URLs and API keys.

## Install (Termux)

Extract the package, then from inside its folder:

```bash
bash install.sh
```

## Setup

Edit the config (created automatically on first run):

```bash
nano ~/.v4zroute/config.json
```

```json
{
  "host": "127.0.0.1",
  "port": 8787,
  "timeout": 60,
  "gateway_key": "",
  "providers": [
    {
      "name": "myprovider",
      "base_url": "https://api.example.com/v1",
      "api_key": "YOUR_API_KEY_HERE",
      "models": ["model-name-here"],
      "enabled": true
    }
  ],
  "aliases": {}
}
```

- Add one entry per provider you have an account with. Entries still containing `example.com` or the placeholder key are skipped automatically.
- `gateway_key` (optional): if set, clients must send `Authorization: Bearer <gateway_key>` to the gateway itself.
- Keep this file on your phone only — it holds your keys. Never upload it.

## Commands

| Command | What it does |
|---|---|
| `v4zroute serve` | Run the gateway (`--host` / `--port` to override config) |
| `v4zroute providers` | Show configured providers (API keys masked) |
| `v4zroute test` | Validate config and ping each provider's `/models` |

## Using the gateway

```bash
v4zroute serve
```

Then point any OpenAI-compatible app/tool at:

```
http://127.0.0.1:8787/v1
```

and use one of these model names:

| Model name | Meaning |
|---|---|
| `auto` | First working provider, with automatic fallback |
| `<provider>/<model>` | A specific provider, e.g. `myprovider/model-name-here` |
| `<model>` | Whichever provider lists that model |
| any alias from config `aliases` | Your own shortcut, e.g. `"fast": "myprovider/model-name-here"` |

Fallback: if the chosen provider times out, refuses the connection, or answers with an HTTP error, the request is retried on the next provider in the chain. If every provider fails, the gateway returns a `502` with the per-provider reasons (provider names only — keys are never printed).

Also available: `GET /health` and `GET /v1/models`.

Streaming (`"stream": true`) is relayed through as-is.

## Limitations

- The gateway connects to providers **directly**; proxy environment variables of your shell are ignored.
- Fallback works per request. If a provider dies in the middle of a streamed reply, that stream simply ends — the next request will use the next provider.
- Non-root Android can kill background apps; keep the Termux session alive while serving (a wake lock helps).
- No built-in providers or keys are bundled. No account, no AI — the gateway only routes.

## Requirements

- Termux with Python 3 (installer handles it)
- Pure Python standard library — no pip installs

---

© V4Z RASHD — [@rashdteem](https://t.me/rashdteem)
