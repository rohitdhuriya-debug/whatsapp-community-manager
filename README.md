# WhatsApp Community & Channel Manager

A personal, single-user, fully local engagement manager for the WhatsApp communities and
channels you admin. Pick chats, write one line about what you want, and it produces a
message, a poll, a PDF resource or an Excel sheet — then sends it now or on a schedule.

**Runs entirely on your Mac. No cloud, no hosting bill.**

## Autopilot — it decides what to post

Set a community up once, and it runs itself: each firing it looks at what it has already
sent, picks a fresh topic and a format, writes it, and delivers.

**Setup is one box.** Describe the community — who they are, what they already know, what
they want. That text is the whole brief; everything else is picked per run.

**Different every time, enforced twice.** A prompt asking a model not to repeat itself is
not a guarantee, so:

1. The planner is shown every topic already sent and told to avoid them.
2. The topic it returns is scored against that history **in code** — Jaccard overlap of
   significant words, with plurals folded. Above 0.5 the plan is rejected and re-planned
   with explicit exclusions.

Measured on real rephrasings: *"Top 10 AI tools for founders"* vs *"10 best AI tools
founders should use"* scores **1.00** (blocked); *"SIP checklist for beginners"* vs
*"a beginner checklist for starting SIPs"* scores **0.75** (blocked); while *"AI tools for
video editing"* vs *"AI tools for accounting"* scores **0.20** — same category, genuinely
different topic, allowed through.

**Format rotates too.** With "let it decide", the planner proposes a format and the code
refuses one used in the last two runs while alternatives exist. Pin it instead if you always
want a PDF.

| Button | What it does |
|---|---|
| **What would it post next?** | Thinks and shows the topic, angle and format — writes nothing, sends nothing |
| **Run now** | Thinks and drafts, then waits for approval |
| **Set schedule** | Time + every day / alternate days / Mon–Fri / weekly |
| **Clear memory** | Forgets past topics, so it may revisit them |

Approval is on by default. Turn it off and it posts unattended.

## Two ways to work

| | **Compose** | **Personas** |
|---|---|---|
| Shape | One brief → many chats, right now | One community → its own voice, on repeat |
| Use it for | Broadcasts, resources, announcements | Hands-off daily/weekly content |
| Where | `/compose` | Each community's page |

Both share the same send gate, approval queue, scheduler and log.

## Two engines, switchable per send

| | **OpenRouter (free)** | **Claude Code (Max plan)** |
|---|---|---|
| Cost | Free | Included in your subscription |
| Speed | 10–40s | 40s–2min |
| Limits | ~50/day, provider rate limits | None in practice |
| Best at | Short messages, polls | PDFs, spreadsheets, long structure |

Claude Code runs the local CLI headlessly, sandboxed in an empty directory with all tools
disabled — a pure generator that cannot read your files or run commands.

## Multiple devices

Link more than one WhatsApp account on the Devices tab. Each is a separate WAHA session in
the same free container, with its own QR, its own groups and channels, and its own chats to
pick from. A chat can only ever be sent from the account it belongs to.

---

## What it costs

| Piece | Service | Cost |
|---|---|---|
| WhatsApp bridge | WAHA Core (Docker, local) | free |
| Research | DuckDuckGo via `ddgs` + Google News RSS | free, keyless, unlimited |
| Drafting | OpenRouter models ending `:free` | free (~50 calls/day) |
| Database | SQLite (`app.db`) | free |
| Dashboard | FastAPI on localhost | free |

Research uses no API keys at all, so **each message costs exactly one free model call.**

---

## One-time setup

### 1. Prerequisites

- **Docker Desktop** — https://www.docker.com/products/docker-desktop (launch it once after installing)
- **Python 3.12** — `brew install python@3.12`

### 2. Configure

```bash
cp .env.example .env
```

Then edit `.env`:

| Variable | What to put |
|---|---|
| `WAHA_API_KEY` | Any long random string you invent |
| `WAHA_DASHBOARD_PASSWORD` | Any password — this is your QR-dashboard login |
| `OPENROUTER_API_KEY` | Optional — you can add this in the dashboard instead |

`.env` is git-ignored and holds live credentials. Never commit it.

**The OpenRouter key is easier to set in the app:** Settings → *Drafting model* → paste the
key → **Test key** to confirm it works → **Save key**. It is stored in `app.db` (also
git-ignored) and takes precedence over `.env`, so you never have to touch a file. The
Settings page only ever shows a masked version, and the API never returns the raw key.

### 3. Install and start

```bash
./start_all.command
```

That launches Docker if needed, starts WAHA, creates the virtualenv on first run, and opens
the dashboard at http://localhost:8080.

### 4. Pair WhatsApp

Open http://localhost:3000/dashboard and log in with `WAHA_DASHBOARD_USERNAME` /
`WAHA_DASHBOARD_PASSWORD` from `.env`.

Start a session named **exactly `default`** and scan the QR from your phone
(WhatsApp → Settings → Linked Devices → Link a Device).

> WAHA Core's free tier allows exactly one session and it must be called `default`.
> **Turn any VPN off while pairing** — it is the most common cause of QR failures.

The Settings page badge turns green and reads `WORKING` once you are paired.

---

### 5. Optional — enable the Claude Code engine

```bash
claude
```

Run `/login` once with your Max plan. The app pins an explicit model (`sonnet` by default)
rather than inheriting `~/.claude/settings.json`, so a local override there — an Ollama
model, say — cannot break generation. Your settings file is never modified.

---

## Daily use

Everything happens on **Compose**, top to bottom:

1. **Send to** — pick the account, tick groups and/or channels (announcement groups first,
   with member counts). *Reload list* re-fetches from WhatsApp.
2. **What do you want to send?** — describe it in your own words: topic, angle, what to
   include, what to avoid. Then pick *Deliver as* (message / poll / PDF / Excel), the
   language, and the engine.
3. **Generate** → the final draft appears **exactly as it will arrive**, with a separate
   card for the group version and the channel version, since those differ. Approve, edit
   either version, regenerate, or reject — all inline.
4. **When** — pick a date and a time, then how it repeats: *just once, every day, alternate
   days, every week, Mon–Fri,* or a custom interval. The chosen time is used for every
   mode.

Ticking a chat registers it as a target automatically — that's what makes it sendable.

**Overview** shows the stats, everything scheduled, your saved targets, and recent
activity. There is no separate approvals or scheduled tab; both live in the flow above.

### Groups vs channels — how files are delivered

Channels do not render document attachments usably, so the app delivers the same campaign
two different ways and you do not have to think about it:

| | Group (`@g.us`) | Channel (`@newsletter`) |
|---|---|---|
| Message / poll | ✅ direct | ✅ direct |
| PDF / Excel | ✅ **file attached** | ✅ **preview image + Drive link** |

For a channel, page one of the PDF is posted as a picture and the caption carries a Google
Drive link to the full file. That is the format that actually works in a channel feed.

Without Drive connected the image and caption still post — just with no download link, and
the reason is shown on the preview.

### Google Drive — two ways to connect

Settings → **Google Drive** lets you pick:

| | **Claude connector (MCP)** | **Own Google client (API)** |
|---|---|---|
| Setup | none — uses your claude.ai Drive connector | one-time OAuth client |
| File size | **~16 KB max** | any size |
| Speed | ~30s per upload | instant |
| Cost | Max-plan usage | free |

**The MCP path cannot carry real documents.** The Drive MCP tool takes file content as a
base64 *argument*, so the model has to write the entire encoded file out as tool-call
output. Measured here: a 560 B file uploads fine in 31s (~187 output tokens); a 66 KB PDF
needs ~22,500 output tokens and never completes. Output limits, not context limits, are the
binding constraint.

So MCP is fine for tiny text files and useless for the PDFs this app generates. If MCP is
selected and the file is too big, it falls back to the API path automatically when that is
connected, and otherwise says exactly why.

### Google Drive API setup (one time, free)

Settings → **Google Drive** walks you through it:

1. [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials),
   create or pick a project
2. Enable the **Google Drive API**
3. **Create credentials → OAuth client ID → Web application**
4. Add this exact authorised redirect URI:
   `http://localhost:8080/api/drive/oauth/callback`
5. Download the JSON, paste it into Settings, click **Save credentials**
6. Click **Connect Google Drive** and approve

The app requests only the `drive.file` scope, so it can see files it created and nothing
else in your Drive. Uploads land in a `Upsurge WhatsApp Manager` folder, shared link-readable so
channel subscribers can open them. **Test** verifies the connection any time.

### Files tab

Every generated PDF and spreadsheet is listed under **Files**, newest first. PDFs render as
page images and spreadsheets as a table, so you can check the output without downloading it.
Page images are rendered server-side rather than embedded as a PDF, because embedding
depends on a browser plugin that often shows a blank panel.

### Assets

PDFs and spreadsheets are generated from the model's structured output and rendered by the
app, so they carry consistent typography rather than looking machine-made. Money is written
as `Rs.` rather than `₹` — neither Arial nor Arial Unicode MS contains the rupee sign (it
postdates both), so it would render as an empty box. Any glyph the embedded font cannot draw
is substituted or dropped before it reaches the page.

### Adding a community or channel

Click **+ Add community/channel**. The picker pulls your groups and channels live from the
paired WhatsApp account; click one and its `chat_id` and type fill in automatically.

Then set the persona. This is what makes each target sound different:

| Field | What it does |
|---|---|
| **Persona prompt** | The system prompt. Who is writing, for whom, with what point of view. |
| **Research instructions** | Used verbatim as the search query base. Be specific. |
| **Tone** | Short adjectives: "punchy, practical, no hype" |
| **Language** | Hinglish (Roman-script Hindi-English mix) or English |
| **Example messages** | Your best past messages. Used as few-shot examples so drafts sound like *you*. This has more effect on quality than anything else. |
| **Banned topics** | Hard exclusions |
| **CTA link** | The one link allowed in a message |
| **Model override** | Use a different `:free` model for this target only |
| **Investment disclaimer** | `Auto` infers from the niche, or force it `Always` / `Never` |

Adding target #20 needs zero code changes.

### Schedules

Presets are Daily 9:00 AM and Weekly Monday 10:00 AM, or write any 5-field cron. The editor
shows a plain-English preview and the next fire time before you save. **All schedules run in
Asia/Kolkata.** Schedule changes apply immediately — no restart.

If several targets fire in the same window, sends are staggered by a random 30–120 seconds so
your account does not emit a burst.

---

## Safety properties

These are enforced in code, not just intended:

- **Only saved targets can be messaged.** Every send goes through one gate that re-checks the
  `chat_id` against the `targets` table. An unsaved id is refused. (`app/services/sender.py`)
- **Only free models are ever called.** Any model id not ending in `:free` is rejected before
  the request leaves your machine — a typo cannot bill you.
- **WAHA is bound to `127.0.0.1` only.** It holds your live WhatsApp session and is never
  exposed to your network.
- **Every outbound attempt is logged**, success or failure, in `send_log`.
- **Approval is on by default** for every new target.
- **Finance niches** automatically get `Educational purpose only, not investment advice.`
  appended, and price targets / guaranteed returns are prompted against.

### Global kill switch

Settings → **Sending enabled**. Off means drafts still generate but nothing leaves the machine.

---

## Free-tier limits

OpenRouter allows roughly **50 requests/day** on a ₹0 account. The Settings page shows today's
count with a progress bar. Research is keyless and unlimited, so one message = one call.

Two daily targets = 2 calls/day. You have plenty of headroom; you would need ~50 scheduled
sends a day to hit the ceiling. If you do hit it, drafting fails with a clear 429 message and
retries the next day.

Free model availability changes over time. The Settings dropdown is fetched live from
OpenRouter and only ever lists `:free` ids, so it is always accurate.

**Automatic model fallback.** Free models get rate-limited at the provider constantly — this
is separate from your own quota and can hit at any moment. If your chosen model is
unavailable, the app automatically tries the next known-good free model rather than losing
the message. Several `:free` models are reasoning-tuned and return an empty message with only
internal reasoning; those are excluded from the fallback list, and their chain-of-thought is
never published.

| Symptom | What it actually means |
|---|---|
| "`X` is rate-limited at the provider" | That model is busy. Transient; fallback handles it |
| "daily free-tier limit reached" | Your own ~50/day quota. Wait for reset |

---

## Access from anywhere

```bash
./start_public.command
```

That starts Docker, WAHA, the dashboard, and a Cloudflare tunnel, then prints and opens a
public HTTPS link. Use it from your phone or any browser.

It exports `PUBLIC_URL` for you, so absolute links — the Google Drive OAuth redirect above
all — point at the tunnel instead of a `localhost` that only exists on the Mac.

**Only port 8080 is tunnelled.** Port 3000 is WAHA, which holds your live WhatsApp session;
it stays bound to loopback and has no route through the tunnel. Never expose it.

Things worth knowing:

- **The link has no login.** Anyone who has it can post to your communities, and delete
  targets. Treat it like a password: don't paste it into group chats, screenshots or
  issues. To lock it down later, set `APP_BEARER_TOKEN` in `.env` (protects `/api/*`), or
  put Cloudflare Access in front of the tunnel.
- **The URL changes every run.** TryCloudflare hands out a random hostname each time. For a
  stable address you need a Cloudflare account and a named tunnel on your own domain.
- **The Mac must stay awake and online.** Close the terminal window, sleep the Mac, or lose
  wifi and the link dies. `caffeinate -s ./start_public.command` keeps it from sleeping.
- **Drive OAuth through the tunnel:** add the tunnel's callback URL to your Google OAuth
  client's authorised redirect URIs, or connect Drive while on `localhost` instead.

For local-only use, `./start_all.command` is unchanged.

## Working on it

The repo lives at
[rohitdhuriya-debug/whatsapp-community-manager](https://github.com/rohitdhuriya-debug/whatsapp-community-manager).

The public link serves the app **running on this Mac**, so any code change is live as soon
as the app restarts — there is no build or deploy step. The loop is:

1. Ask for a change in Claude Code
2. It edits the files here and pushes to GitHub
3. Restart the app (`Ctrl+C`, then `./start_public.command`) and the link serves it

`.env`, `app.db`, `waha-sessions/` and `assets/` are git-ignored, so no keys, no WhatsApp
pairing and no generated content ever reach the public repo.

---

## Commands

```bash
./start_all.command
```

```bash
./stop_all.command
```

Stopping does not unpair WhatsApp — the session lives in `./waha-sessions`.

Other useful ones:

```bash
docker compose logs -f waha
```

```bash
.venv/bin/python scripts/verify_waha_api.py
```

That last one re-checks every WAHA call this app makes against the running container's own
OpenAPI spec. Run it after upgrading the WAHA image.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Badge says `UNREACHABLE` | Docker isn't running. `./start_all.command` |
| Badge says `NOT_CREATED` | Session not made yet. Open the WAHA dashboard and start one named `default` |
| Badge says `SCAN_QR_CODE` | Scan the QR. Turn VPN off first |
| Badge says `FAILED` | Delete the session in the WAHA dashboard and re-pair |
| WAHA rejects the API key | `.env` changed after the container started: `docker compose up -d --force-recreate` |
| Drafting says "key missing" | `OPENROUTER_API_KEY` still has the placeholder value in `.env` |
| Drafting returns 429 | Daily free ceiling hit. Wait, or pick another `:free` model |
| Picker shows no groups | You must be paired, and the account must actually be in groups |
| Empty research results | Loosen the research instructions — very narrow queries return nothing |
| Claude Code says "not logged in" | Run `claude` in a terminal, then `/login` |
| Claude Code rejects the model | Pick haiku / sonnet / opus in Compose |
| Phone says "couldn't link device" | The QR expired. WAHA issues ~2 minutes of codes then force-stops the session (`QR refs attempts ended`). The dashboard now restarts it automatically and shows a fresh code — just keep the QR window open |
| A device shows `FAILED` | Same cause. Reopen its QR; it self-heals |
| Leftover sessions in WAHA | Devices tab lists any session with no device record and offers to remove it |
| Asset shows `Rs.` not `₹` | Deliberate — no available font has the rupee glyph |
| "Channels cannot receive file attachments" | Correct — WhatsApp limitation. Send files to groups |
| Generation seems stuck | The bar keeps creeping; free models sometimes take 2+ minutes. A model swap is shown under the bar |

---

## Layout

```
app/
  main.py              FastAPI app, lifespan, router wiring
  config.py            .env loading, free-model validation
  models.py            SQLModel tables
  db.py                engine, session helpers, settings k/v
  security.py          optional bearer token (off by default)
  web.py               Jinja setup + shared page context
  routers/
    pages.py           HTML pages
    targets_api.py     targets CRUD, compose-now, test send
    drafts_api.py      queue, edit, approve (sends), reject
    schedules_api.py   schedule CRUD + cron preview
    settings_api.py    settings + live free-model list
    waha_api.py        session status, group/channel picker
    logs_api.py        send log
  services/
    planner.py         decides what to post; novelty + format rotation enforced
    autopilot.py       plan -> write -> deliver, unattended
    composer.py        the Compose flow: brief -> content -> many chats
    pipeline.py        persona flow: research -> draft -> queue/send, jitter, retry
    engines.py         one interface over both engines, JSON extraction
    claude_engine.py   headless Claude Code CLI, sandboxed and toolless
    assets.py          model JSON -> branded PDF / formatted Excel
    research.py        ddgs + Google News RSS, merge and dedupe
    llm.py             OpenRouter transport, free-only guard, model fallback
    sender.py          the single outbound gate (HC-7), text/poll/file
    drive.py           Drive provider dispatch + OAuth API path (drive.file scope)
    drive_mcp.py       Drive via the claude.ai MCP connector (small files only)
    waha.py            WAHA REST client, multi-session
    scheduler.py       APScheduler, Asia/Kolkata, cron + one-off, hot reload
  templates/           Jinja pages (white theme only)
  static/              vanilla JS, no build step
scripts/
  verify_waha_api.py   checks our calls against the live WAHA spec
docker-compose.yml     WAHA, bound to 127.0.0.1 only
```

---

## Non-goals

No multi-user auth, no cloud deploy, no paid model fallback, no bulk DM or outreach to
individuals, no dark mode. This tool posts to communities and channels **you admin**.
