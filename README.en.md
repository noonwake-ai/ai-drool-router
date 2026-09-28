<div align="center">

<h1>💧 AI Drool Detector</h1>

<strong>Is your AI still drooling?</strong>

<p>Scheduled probes · powered by Sub2API · answers straight from upstream</p>

<p>
  <a href="https://noonwake-ai.github.io/ai-drool-detector/"><strong>Live demo</strong></a> ·
  <a href="README.md">简体中文</a> ·
  <a href="#what-it-solves">What it solves</a> ·
  <a href="#five-minute-setup">Five-minute setup</a> ·
  <a href="#deploy">Deploy</a> ·
  <a href="#security-boundaries">Security</a>
</p>

<p>
  <img alt="License" src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-green.svg?style=for-the-badge&logo=python&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/React-19-58c4dc.svg?style=for-the-badge&logo=react&logoColor=white">
  <img alt="Sub2API" src="https://img.shields.io/badge/Sub2API-required-d5b769.svg?style=for-the-badge">
</p>

</div>

![AI Drool Detector dashboard](docs/assets/dashboard-en.png)

> 🚀 **[Open the live demo](https://noonwake-ai.github.io/ai-drool-detector/)** — nothing to install, no key, no cost. The data is a locally generated static snapshot and the controls are disabled there.

---

## What it solves

You pay for model quality, but you do not always get it.

Relay purity is uneven, upstreams silently degrade, and a provider can swap its backend overnight. None of that is visible from the outside. Your client just says "request succeeded" and hands you an answer that looks fine while being measurably worse.

**AI Drool Detector** does something blunt: on a fixed schedule it asks every model configured in your Sub2API the same question, puts the results on one board, and **writes the resulting score back as Sub2API call priority**.

- **Intelligence** — one fixed candy question, needing two fresh independent correct answers to pass
- **Cost** — the supplier rate multiplier you enter, where lower is cheaper
- **Stability** — request success rate over recent rounds
- **Speed** — latency to the first body character plus end-to-end throughput

The four factors combine into a composite score, and the score becomes call priority. Suppliers that degraded sink; suppliers that are genuinely healthy rise.

> This is a **targeted reasoning probe**, not a full model evaluation. It answers one question: is this supplier still delivering the level it should?

## Highlights

| | |
|---|---|
| 🎯 **Fixed questions, comparable results** | The same candy question and the same SVG animation task every round, comparable across time and suppliers |
| 🔁 **Two independent requests** | A pass requires two brand-new requests both answering correctly. A definite wrong first answer ends the round immediately instead of burning a second call |
| 🧠 **Budget cut vs upstream failure** | A stream that keeps delivering data until the client budget runs out is recorded as a budget cut — not an outage, and it never opens a circuit |
| 🚦 **Honest circuits** | Only two consecutive real upstream errors stop a supplier. Local misconfiguration, an interrupted worker and an ungradable answer never misfire |
| ⚡ **Composite scoring** | intelligence 36% / cost 36% / stability 18% / speed 10%, every weight configurable |
| 💰 **Cost ledger** | Per-request usage with the price snapshot in force at the time, summed per platform over 24 hours and 30 days |
| 🌏 **Bilingual UI** | Simplified Chinese by default with a one-click English toggle; `?lang=en` also works as a direct link |
| 🔒 **Credentials never reach the browser** | The dashboard only ever sees a redacted public projection |
| 🧩 **Any model** | Five wire protocols built in: OpenAI Responses, Chat Completions, Anthropic, Gemini, xAI. **Any OpenAI-compatible relay (Moonshot, DeepSeek, Qwen, Volcengine, OpenRouter…) needs config only, no code change** |

## How it works

```
                    ┌──────────────────────────────┐
                    │   Sub2API (your gateway)      │
                    │   accounts · groups · priority │
                    └───────────┬──────────────────┘
                                │ admin API (read + one narrow write)
                                ▼
   ┌────────────────────────────────────────────────────┐
   │  AI Drool Detector worker                           │
   │  1. Pull accounts, pick the models to probe          │
   │  2. Call upstream directly with that account's creds │
   │  3. Run the candy and SVG tasks; record the metrics  │
   │  4. Score, then write call priority (optional)       │
   └───────────┬────────────────────────┬───────────────┘
               │ public projection      │ narrow write
               ▼                        ▼
   ┌────────────────────┐    ┌──────────────────────┐
   │  Public dashboard   │    │  Sub2API call priority│
   │  bilingual · no creds│    │  circuits / recovery  │
   └────────────────────┘    └──────────────────────┘
```

Three processes, none sharing credentials:

| Process | Job | What it can read |
|---|---|---|
| `detector.monitor` | scheduled probes, scoring, optional writes | Sub2API admin key (this process only) |
| `detector.server` | read-only dashboard plus a scoped pause endpoint | the public data directory only — **no private database, no credentials** |
| `detector.pricing_tick` | recompute priority when a rate changes | Sub2API admin key (this process only) |

## Five-minute setup

### 1. What you need

- A running **Sub2API** — the probe reads accounts and routes through it
- A small Linux box with Python 3.10+ and Node 20+ (1 vCPU / 1 GB is plenty)
- A **Sub2API administrator API key**

> Generate the key in your Sub2API admin settings and use it only for this project. Do not reuse a key you use elsewhere.

### 2. Install

```bash
git clone https://github.com/noonwake-ai/ai-drool-detector.git
cd ai-drool-detector

python3 -m pip install -r detector/requirements.txt

cd web && npm ci && npm run build && cd ..
```

### 3. Configure

```bash
cp config.example.json config.json
```

Change at least these three things:

```jsonc
{
  "base_url": "https://your-sub2api-host",  // your gateway
  "data_dir": "./data",                     // where runtime state lives
  "platforms": {
    "openai": {
      "enabled": true,
      "model": "gpt-6-astra",               // what you want probed
      "effort": "medium",                   // reasoning effort
      "group_ids": [],                      // empty = every account on this platform
      "exclude_names": ["生图", "image"]     // name match = skip
    }
  }
}
```

**Group scope**: leave `group_ids` / `group_names` empty to probe every account on that platform. Fill them in to narrow the set. `exclude_names` always wins — list keywords there to keep image-generation accounts out.

**Models**: use the model names that actually exist in your Sub2API. To add another supplier, add another block:

```jsonc
"moonshot": {
  "enabled": true,
  "label": "Kimi",
  "model": "kimi-k3",
  "effort": "max",
  "protocol": "openai_chat"
}
```

**What `protocol` means**: it selects the wire protocol for that supplier. You can omit it — `openai`, `anthropic`, `gemini` and `grok` pick their native protocol automatically, and **any other name is treated as OpenAI-compatible Chat Completions**, so mainstream relays work out of the box.

| protocol | When to use it |
|---|---|
| `openai_chat` | The default. Anything exposing `/v1/chat/completions`: Moonshot, DeepSeek, Qwen, Volcengine, OpenRouter, SiliconFlow… |
| `openai_responses` | OpenAI-compatible services exposing `/v1/responses` |
| `anthropic_messages` | Claude's `/v1/messages` |
| `gemini_generate` | Gemini's `streamGenerateContent` |
| `xai_responses` | xAI's `/v1/responses` |

An unknown protocol name fails at startup rather than degrading silently.

### 4. Look at it for free first

```bash
python3 scripts/dev_preview.py
# open http://127.0.0.1:4191/
```

This preview uses locally generated fake data. It **never contacts your gateway, costs nothing, and needs no credentials**. Confirm the UI looks right before going further.

### How the online demo is built

The static snapshot in [demo/](demo/) **is a build artefact that is committed on purpose** — GitHub Pages publishes it directly.

```bash
python3 scripts/build_demo.py          # regenerate demo/
python3 scripts/build_demo.py --check  # what CI runs: fail if it drifts from source
```

What it does:

- builds the frontend with `DROOL_BASE=./` so every asset and endpoint path is relative and the bundle works from a subdirectory such as `/<repo>/`
- writes the synthetic `dev_preview` dataset to `demo/api/state`, `demo/api/runs/<id>` and `demo/artifacts/<id>.html`
- injects a `drool-demo` marker so the client shows the demo banner and disables every control
- anchors all timestamps to a fixed instant, so the output is byte-reproducible and `--check` can actually detect drift

**The demo contains no real data, no credentials and no gateway address.** CI re-runs the secret scan and the public-projection check before publishing.

### 5. Run it for real

```bash
export SUB2API_ADMIN_KEY='your-admin-key'

# Sync the account list only, with zero model requests (recommended first run)
python3 -m detector.monitor --metadata-only

# Run one real round
python3 -m detector.monitor --source initial
```

Then serve the board with real data:

```bash
python3 -m detector.server --dist web/dist --public ./data/public
```

> A real round spends tokens. To validate cheaply, keep one platform enabled or scope `group_ids` to one or two accounts.

## Deploy

### Option 1: Docker (simplest)

```bash
cp config.example.json config.json      # set base_url and platforms
printf 'SUB2API_ADMIN_KEY=your-key\n' > .env

docker compose up -d
# open http://127.0.0.1:4191/
```

To look at the UI only — no gateway, no spend:

```bash
docker compose run --rm --service-ports web preview
```

The image runs three services: `web` (read-only board), `worker` (scheduled probes) and `pricing` (recomputes priority when a rate changes). **Only worker and pricing receive the key; web never does.** State lives in the `drool-data` volume.

### Option 2: systemd

Ready-made systemd units live in `deploy/`.

```bash
sudo install -d -m 0755 /opt/ai-drool-detector
sudo install -d -m 0750 /var/lib/ai-drool-detector
sudo install -d -m 0750 /etc/ai-drool-detector

# Code under /opt/ai-drool-detector/current (symlink to a versioned dir for rollback)
# Config at /etc/ai-drool-detector/config.json

sudo install -m 0600 deploy/drool-detector.env.example /etc/ai-drool-detector/drool-detector.env
sudoedit /etc/ai-drool-detector/drool-detector.env   # put the real key here

sudo install -m 0644 deploy/systemd/*.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/*.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now drool-detector-web.service
sudo systemctl enable --now drool-detector-worker.timer drool-detector-sync.timer drool-detector-pricing.timer
```

| Unit | Cadence | Purpose |
|---|---|---|
| `drool-detector-worker.timer` | every 15 min | the worker decides whether a scheduled slot is due |
| `drool-detector-sync.timer` | daily at 04:00 | refresh the account list, **no model requests** |
| `drool-detector-pricing.timer` | every minute | recompute priority when a rate changes |
| `drool-detector-web.service` | always on | the read-only dashboard |

Put a reverse proxy in front (Caddy / Nginx); see `deploy/Caddyfile.fragment`.

**Rollback**: `current` is a symlink. Point it at the previous release directory and restart `drool-detector-web`. No data or control file changes are needed.

## Configuration reference

### Cadence

```jsonc
"schedule": {
  "timezone": "Asia/Shanghai",
  "regular_minutes": 45,
  "quiet_start": "04:00",
  "quiet_end": "08:00",
  "quiet_minutes": 90,
  "history_hours": 24
}
```

### Timeout budgets

```jsonc
"budgets": {
  "default_seconds": 900,
  "idle_seconds": 120,
  "gemini_high_idle_seconds": 300,
  "drawing_platform_seconds": {"grok": 1500},
  "max_attempts": 3
}
```

High-effort models can spend well over ten minutes reasoning before emitting the first body character on the drawing task. **Do not lower `default_seconds`** — the budget running out is recorded as a budget cut and that round does not pass. Widen `drawing_platform_seconds` for slow platforms instead.

### Scoring

```jsonc
"routing": {
  "weights": {"intelligence": 0.36, "cost": 0.36, "stability": 0.18, "speed": 0.10},
  "rounds": 3,
  "write_priority": false,   // default: score only, never touch your gateway
  "write_callable": false    // default: no circuit or recovery writes
}
```

> **The default is observe-only, and this switch is authoritative**: neither a CLI flag nor a systemd unit can override it. A write happens only when config sets the switch to `true` **and** the matching `--enable-*` flag is passed. Turn both on once the scores match your expectations.

### Privacy

```jsonc
"privacy": {
  "account_names": "full"   // full | alias | masked
}
```

The board shows account names, which often reveal the real upstream. Before a public deployment, switch to `alias` (stable pseudonyms, history still lines up) or `masked` (first and last characters only).

## Security boundaries

| Boundary | How |
|---|---|
| **No keys in the browser** | The browser only receives a redacted projection: account name, platform, model, state, scores. No tokens, no base URLs, no emails |
| **Auditable projection** | `scripts/check_public_projection.py` checks the projection by field name and value shape, catching keys, JWTs, emails, private IPs and unknown hosts. CI runs it on every push |
| **No keys in Git** | `config.json`, `.env`, `credentials/` and `data/` are all ignored |
| **No keys in logs** | Every upstream error is redacted before storage — keys, tokens, emails and upstream URLs are replaced |
| **Process isolation** | The web process cannot read the private database or the credential directory, and can only write control files |
| **Model output is untrusted** | Generated HTML runs in a `sandbox allow-scripts` iframe with network, forms and framing blocked |
| **Narrow writes** | Only two Sub2API mutations exist: priority and callability. Both read back and fail loudly on mismatch |

**The anonymous pause endpoint is off by default.** With `web.public_controls` enabled, anyone who can reach the page can pause or resume an account's probing. That is fine on an internal network; for a public deployment, think it through — or keep it read-only.

## FAQ

**Is Sub2API required?**
Yes. The detector does not maintain its own account pool: accounts come from Sub2API, credentials come from Sub2API, and the routing result is written back to Sub2API. Without the gateway, the chain from "where do accounts come from" to "who provides the reserve" is broken.

**Will this get my accounts banned?**
It uses each account's own credentials and makes ordinary model requests, just on a schedule you control. The real consideration is volume — by default each platform and account gets two tasks per round. Raise `regular_minutes` or scope the probe to a few accounts if that is too much.

**Why does the first wrong answer end the round?**
A definite wrong answer is already a result; asking again only burns another call. Only a **request error** deserves a retry, because that means you never got an answer.

**What does "budget cut" mean?**
It means upstream kept streaming data (first token arrived early) but the generation did not complete inside your budget. That is your budget, not an outage, so it neither opens a circuit nor grades as a wrong answer.

**The page is empty.**
Make sure a round has run — `--metadata-only` or a real probe — so `data/public/state.json` exists. The dashboard reads that file.

**Do I need to restart after editing config?**
The worker re-reads `config.json` at every start, so the next round picks it up. Web-side changes such as port or paths need a service restart.

## Contributing

Issues and PRs are welcome. Before submitting, run:

```bash
python3 -m unittest discover -s detector/tests -t . -p 'test_*.py'
cd web && node --test src/*.test.js && npm run build
```

Add tests with any new behaviour. For changes touching Sub2API writes, describe your read-back verification strategy.

## License

[MIT](LICENSE) © NoonWake.AI
