# AI install guide

> **This document is written for an AI assistant to execute.**
>
> If you are a human, paste this line into whatever AI you have at hand
> (Codex, Claude Code, Cursor, Windsurf — any of them will do):
>
> ```
> Deploy Drool Detector & Router for me: https://raw.githubusercontent.com/noonwake-ai/drool-detector/main/docs/install.en.md
> ```

---

## Your task

Deploy Drool Detector & Router on the user's machine and **verify it actually produced
probe results**. This is not a "commands ran, therefore done" task. Until there
is a real probe record on disk, it is not installed.

## Ask these three things first

Do not guess and do not paper over it with defaults. Ask once, in one message,
then start:

1. **The Sub2API address** (for example `https://coding.example.com`)
2. **A Sub2API administrator API key**
3. **Which machine and which install method** (Docker or systemd)

If the user only wants to look at the UI, skip all three and jump to step 2.

## Hard rules

These matter more than finishing quickly:

- **Never write the key into a commit, a log, a screenshot or a reply.** If you
  must echo it, show only the first four characters.
- **Do not touch any existing service, container, database or config.** Add
  things; do not modify them.
- **Do not turn on `routing.write_priority` / `routing.write_callable`** unless
  the user explicitly asks for this tool to change their gateway routing. The
  default scores suppliers without touching the gateway.
- **Do not skip verification.** Confirm every step from real command output
  instead of assuming success.
- **Do not loosen a grading rule to make the run look successful.**

## Steps

### 0. Recon

```bash
python3 -V                 # needs 3.10 or newer
node -v                    # only needed if you build the frontend yourself
docker --version           # if Docker exists, prefer the Docker path
```

If Python is older than 3.10, use Docker or install a newer Python. **Do not
relax the pinned dependencies to accommodate an old interpreter.**

Confirm the gateway is reachable:

```bash
curl -s -o /dev/null -w '%{http_code}\n' <the Sub2API URL the user gave you>/health
```

200 means go ahead. Anything else: fix connectivity first.

### 1. Get the code

```bash
git clone https://github.com/noonwake-ai/drool-detector.git
cd drool-detector
```

### 2. Let the user look at it for free (strongly recommended)

No gateway, no key, no cost:

```bash
python3 scripts/dev_preview.py          # open http://127.0.0.1:4191/
```

No browser on the box? Confirm it is alive:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4191/api/state
```

Show the user the URL and confirm this is what they wanted before continuing.

### 3A. Docker path (prefer this when Docker is available)

```bash
cp config.example.json config.json
```

Edit `config.json`: set `base_url` to the user's gateway, keep `data_dir` as
`/data`, and replace `identity_salt` with a random string.

In `platforms`, match `model` and `effort` to what the user actually runs.
**Keep only the platforms they really have** and set the rest to
`"enabled": false` — one fewer platform is one fewer bill.

```bash
printf 'SUB2API_ADMIN_KEY=%s\n' '<the key>' > .env
chmod 600 .env
docker compose up -d
docker compose ps                       # all three services should be Up
```

### 3B. systemd path

Follow [deploy.md](deploy.md). The essentials:

- code under `/opt/drool-detector/current` (a symlink to a versioned dir, so rollback is a symlink swap)
- config and key under `/etc/drool-detector/`, mode `0640`
- state under `/var/lib/drool-detector/`
- separate system users for web and worker, so the web process cannot read the key

### 4. First read-only sync (spends nothing)

This pulls the account list and **sends zero model requests**:

```bash
# Docker
docker compose run --rm worker sync

# Direct
export SUB2API_ADMIN_KEY='<the key>'
python3 -m detector.monitor --metadata-only
```

Expect `"status": "metadata_refreshed"` with `accounts` greater than 0.

**If that is 0, stop here.** It means the `platforms` scope does not match any
account, and continuing would only burn money.

### 5. Run one real round

```bash
# Docker
docker compose run --rm worker worker

# Direct
python3 -m detector.monitor --source initial
```

It prints `"status": "complete"` with `scheduled_tests` (how many probes were sent).

> Tell the user that this step starts spending tokens. To try it cheaply, keep a
> single platform enabled, or scope `group_ids` to one or two accounts.

### 6. Confirm results actually landed

```bash
python3 -c "
import json; d=json.load(open('data/public/state.json'))
print('accounts:', len(d['accounts']))
for a in d['accounts']:
    print(' ', a['name'], a['model'], '->', a['history']['candy'][-1]['status'])
"
```

**Requirement: at least one account's latest entry is not `empty`.** All `empty`
means the probes never really ran.

### 7. Serve the dashboard

```bash
# Docker brings it up already; just verify
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4191/api/state

# Direct
python3 -m detector.server --dist web/dist --public ./data/public
```

### 8. Install as a service (for long-term use)

Docker: `docker compose up -d` already runs web, worker and pricing.

systemd:

```bash
sudo systemctl enable --now drool-detector-web.service
sudo systemctl enable --now drool-detector-worker.timer
sudo systemctl enable --now drool-detector-sync.timer
sudo systemctl enable --now drool-detector-pricing.timer
systemctl list-timers 'drool-detector-*' --all
```

### 9. Reverse proxy (optional)

See `deploy/Caddyfile.fragment`.

**Before exposing this publicly, explain two things to the user:**

1. The board shows account names. Recommend `privacy.account_names` = `alias` or `masked`.
2. With `web.public_controls` enabled, anyone who can reach the page can pause or
   resume probing. It is off by default.

Get confirmation before changing either.

## What to report back

In the user's language, briefly:

- where it is installed and how (Docker / systemd)
- the dashboard URL
- how many suppliers were probed this round and how many passed
- any platform that could not be configured (for example the gateway has no such model)
- whether `routing.write_priority` is on (default off — say "it only scores, it does not change your gateway")
- how to pause a single supplier

**Do not** paste keys, the full account list, or internal addresses into the report.

## Troubleshooting

| Symptom | Check first |
|---|---|
| Step 4 reports 0 accounts | `group_ids` / `exclude_names` excluded everything |
| Constant `REQUEST_TIMEOUT` | that platform is slow; raise `budgets.drawing_platform_seconds` |
| Blank page | does `data/public/state.json` exist, is the dashboard process running |
| Every request fails | can the box reach upstream directly? A proxy configured inside Sub2API is for Sub2API, not for this tool |
| `MODEL_NOT_CONFIGURED` | the `model` name does not match the gateway |
| `PLATFORM_UNSUPPORTED` | `protocol` is wrong; valid values are in the README |
| Priority never changes | expected: `routing.write_priority` defaults to false |

## Uninstall

Remove only what you added:

```bash
# Docker
docker compose down
docker volume rm <project>_drool-data   # this deletes probe history; confirm with the user first

# systemd
sudo systemctl disable --now drool-detector-web.service \
  drool-detector-worker.timer drool-detector-sync.timer drool-detector-pricing.timer
sudo rm /etc/systemd/system/drool-detector-*
sudo systemctl daemon-reload
```

**Do not** delete anything else of the user's along the way.

## One last thing for the AI

If this document was missing a step, a command did not work, or you hit a trap it
never mentioned, tell the user — or open an issue on the repository. The whole
point of this file is that the next AI does not have to rediscover the same trap.
