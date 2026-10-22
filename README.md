# tg-x-copilot

A copilot that turns content forwarded from Telegram into **original, useful X posts in
English or Simplified Chinese for the configured market**.

You forward posts to the bot — **or use the web app from any browser, phone included, without
Telegram** (see [Web app](#web-app-no-telegram-needed)): paste text, add links or upload/paste
screenshots, and get the draft, images and all review tools on the page.

0. **Clean the content**: a small deterministic prefilter removes obvious promotion and tracking; an LLM extracts each message's factual core, then combines the results. Forward attribution is audit/rights metadata and never publishing content.
1. **Jev** ([TypeSafe AI System One](https://docs.typesafe.ai/api)) triages the post with
   typed questions:
   - a value score;
   - the content type;
   - the probability that the post is promotional, risky, unverified, or has useful media.

   Each answer comes with probabilities and a confidence value. Confident low-value posts are
   skipped at once. Uncertain ones are passed on to the main LLM. Jev is **optional**: if it
   is disabled, unconfigured, slow, or failing, the main LLM decides instead and the task
   continues.
2. The **main LLM and vision model** (via a CPA proxy, OpenAI-compatible) evaluate the post
   in depth and rewrite it as a concise, natural X post. The post has an honest hook and
   practical background. Made-up facts, clickbait, and translate-and-repost are blocked by
   deterministic guards.
3. **Images** are kept, enhanced, localized, recreated as original visuals, or flagged for review.
   Source/copyright marks and author/photographer credits are protected. Explicitly requested
   channel-promotion cleanup requires confirmed editing rights and carefully bounded regions;
   third-party files are never reposted.
   Every output from the image model must pass a **visual QC check** before it is used. QC
   compares language, text, numbers, dates, names, brands/logos, people, watermarks, and facts. A failed check sends
   the image to review.
4. You get **images with the X draft attached as a caption**, followed by a related review card
   with buttons to approve, regenerate, or reject. You post it to X
   yourself.

**Storage is built for the R2 free tier.** Incoming media stays in memory only. Nothing is
stored for skipped or rejected content. Only final assets for pending drafts are uploaded.
Review copies are optional and off by default. Assets are freed on approval unless retention
is enabled. Uploads are optimized and keyed by content hash, so duplicates are stored once. Minimal
promotion cleanup uses lossless source-sized PNGs to preserve all pixels outside the approved regions.

**Media is never fatal.** If Telegram downloads, the image model, QC, or R2 fail, the
affected image is marked REVIEW with the reason. The text draft is still delivered.

Stack: Python 3.11+, FastAPI, Telethon, asyncio, httpx, Pydantic v2, MySQL (PyMySQL in
threads), Cloudflare R2 (async SigV4, Standard storage), a CPA proxy, TypeSafe Jev, and
Image2-compatible image models.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the data flow, Jev routing, storage
policy, concurrency, and content rules. See [image delivery diagnostics](docs/IMAGE_DELIVERY.md)
for scoped promotion cleanup and attachment failure stages.

```
Telegram ─▶ Bot (message / album / opted-in burst) ─▶ normalize ─▶ MySQL + Queue ─▶ Workers (atomic claim)
                                                                        │
  Jev /v1/systemone (text-only triage) ──skip──▶ done (nothing stored) ◀┤
                                                                        ▼
  download media to memory ─▶ vision ─▶ LLM evaluate ─▶ rewrite + guards ─▶ image policy
                                                                        │
  R2 assets/<sha256> (final + review copies, deduplicated) ◀── persist ─┘
                                                                        ▼
                                  Draft ─▶ Telegram (approve / regenerate / reject)
```

## Project layout

```
src/tg_x_copilot/
  __main__.py            entry point (uvicorn + lifespan)
  app_context.py         composition root, start/stop order, intake, sweeper
  config.py              pydantic-settings + runtime-editable whitelist
  logging_setup.py       JSON logs, task_id context, secret masking
  models.py              domain models (envelope, triage, vision/eval/rewrite outputs)
  bot/telegram.py        Telethon handlers, draft delivery, ops buttons
  clients/jev.py         TypeSafe System One client (typed questions, /v1/models)
  clients/openai_compat.py  CPA client (chat, vision, images)
  clients/r2.py          R2 SigV4 client (put/get/head/delete, presign)
  db/                    PyMySQL pool (to_thread + semaphore), repository (all SQL)
  pipeline/              normalizer, workers, triage, processor, guards, image policy,
                         image QC, media optimization
  services/              client hub, runtime config, asset store (R2 policy), ops
  prompts/<locale>/      LLM prompts, Jev questions, localized rules and hook defaults
  i18n/locales/          locale packs
  web/                   FastAPI admin UI (Jinja2)
sql/                     schema + en-US seed + DB user
deploy/                  systemd unit (not installed automatically)
tests/                   unit + reliability tests (no network/DB/Telegram needed)
```

## Setup

### 1. Python environment (choose one)

```bash
# Conda
conda env create -f environment.yml
conda activate tg-x-copilot

# or plain pip / venv
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install --no-deps -e .
```

Run `pytest` to execute the unit tests. They need no network and no database; install the
dev extras first with `pip install -e ".[dev]"`.

### 2. MySQL

```bash
mysql -u root -p < sql/001_schema.sql
mysql -u root -p < sql/002_seed_en_us.sql
# edit the password inside first:
mysql -u root -p < sql/000_create_user.sql
```

If your database was created from the first schema version (before atomic task claims), also
run `mysql -u root -p < sql/003_reliability.sql`. It adds the claim columns and deletes any
API keys stored in `settings`.

### 3. Cloudflare R2

Create a bucket, for example `tg-x-copilot`, and keep it in **Standard** storage. The free
tier covers 10 GB-month of Standard storage. Create an R2 API token with **Object Read &
Write** on that bucket. The bucket can stay private: the admin UI uses presigned URLs.

### 4. TypeSafe Jev

Get an API key from TypeSafe (early access) and set `JEV__API_KEY` in `.env`. The defaults
point at `https://api.typesafe.ai/v1` with the model `jev-latest`.

Jev is optional. Set `JEV__ENABLED=false`, or leave the key empty, to run without it. If a
call fails, or exceeds `JEV__BUDGET_SECONDS` (30s by default, including retries), a warning is
logged and the main LLM's evaluation decides.

Routing thresholds can be tuned at runtime: `JEV__CONFIDENCE_FLOOR`, `JEV__MIN_VALUE`,
`JEV__PROMO_SKIP`, `JEV__RISK_REVIEW`, and `JEV__MEDIA_USEFUL_MIN`. To change the triage
questions themselves, edit `src/tg_x_copilot/prompts/en-US/jev_triage.json`.

### 5. Configure

```bash
cp .env.example .env && chmod 600 .env
$EDITOR .env
```

At a minimum, set these:
- the Telegram `API_ID`, `API_HASH`, and `BOT_TOKEN`;
- `ALLOWED_USER_IDS` (your own Telegram user ID);
- the CPA URL and key;
- the Jev key;
- the R2 credentials;
- the DB password;
- `ADMIN__PASSWORD`.

The image model must be compatible with OpenAI Images (`gpt-image-2`). If your CPA relay
rejects `/images/edits`, set `MODELS__IMAGE_EDIT_MODE=generations_with_refs`.

### 6. Run

```bash
python -m tg_x_copilot        # or: tg-x-copilot
```

The web app and admin UI listen on `127.0.0.1:8080`. Open them through an SSH tunnel
(`ssh -L 8080:127.0.0.1:8080 <vps>`) or, for phones, behind HTTPS (see
[Using it from your phone](#using-it-from-your-phone)). For web-only mode set
`TELEGRAM__ENABLED=false`. To run it as a service, see `deploy/tg-x-copilot.service`.

## Web app (no Telegram needed)

The same server that hosts the admin UI is a complete, mobile-friendly client. It works with
`TELEGRAM__ENABLED=false` (web-only mode) or next to the bot.

**Create** — tap **＋ New** (`/new`) and pick a mode:
- **⚡ Quick create**: always returns one ready post **and** one generated image, whatever you
  enter (see [Quick create](#quick-create-always-text--image)). Optional *Directions* steer the
  tone and the picture.
- **Standard**: the full editorial pipeline (scoring, fact guards, rights-aware images, review).

Fields:
- **Text**: paste a post, article excerpt or your own notes.
- **Links**: one or more URLs. With *Read the linked pages* the worker reads each page in the
  background, falling back to other methods for pages that block bots (see
  [Reading links](#reading-links)), and uses it as source material, like a separate
  forwarded post. **Test links** shows right away which method can read each URL and an
  excerpt, without creating a task.
- **Images**: choose files, drop them, or **paste screenshots** straight from the clipboard;
  on phones the picker offers camera and gallery. Up to `WEB__MAX_FILES` (10) images, each
  validated as a real JPEG/PNG/WebP/GIF.
- *This is my own original content* behaves like a direct Telegram message (processed without
  source-review flags). Leave it unchecked for content copied from elsewhere.
- *I hold the rights to edit these images* is the explicit editing-rights declaration (never
  inferred). *Image settings* choose the action (automatic, keep, enhance, localize, recreate,
  clean recreate, information card, generate, text only), image language, density and
  options. Your last choices are remembered as the web profile.

**Follow and finish** — the task page updates itself while the task is queued/processing, then
shows the draft with **Copy text** and the character count, the final images (open / save),
review items, and every action: approve, mark published / not published, regenerate, reject,
**re-run images** with another action or options (text stays unchanged), and
**confirm editing rights** for a single blocked image.

**Where uploads live** — uploaded originals are staged on local disk in `WEB__INBOX_DIR`
(`data/web_inbox/<task_id>/`), not in R2 or MySQL. They are re-used for regenerate and image
re-runs, deleted when you publish / mark not published / reject, and purged after
`WEB__INBOX_TTL_DAYS` (14) otherwise. Web drafts keep their final images in R2 until the task
is published, not published or rejected (approval alone doesn't delete them, because the page
is the only delivery copy).

### Using it from your phone

The UI binds to `127.0.0.1` by default. To reach it from a phone, put it behind **HTTPS** —
for example a Cloudflare Tunnel (`cloudflared tunnel --url http://127.0.0.1:8080`) or a
reverse proxy such as Caddy (`copilot.example.com { reverse_proxy 127.0.0.1:8080 }`). Then:

- keep a strong `ADMIN__PASSWORD` (HTTP Basic auth protects every page and API);
- if the proxy rewrites the `Host` header, add its public origin to
  `ADMIN__TRUSTED_ORIGINS=["https://copilot.example.com"]` (CSRF same-origin check);
- HTTPS also enables the one-tap clipboard copy (plain-HTTP pages fall back to a legacy copy).
- *Add to Home Screen* gives an app-like shortcut on iOS/Android.

## Quick create (always text + image)

Quick create uses the **same pipeline** as standard mode: link reading, cleaning, Jev,
image analysis, evaluation, configured writing rules and hooks, verified fact packets,
image settings and image QC. It adds the requirement to generate both a post and an image;
it does not use separate creative prompts or bypass factual checks.

| | Telegram | Web |
|---|---|---|
| Switch on | `/quick` (send again to switch off), or **⚡ Quick create** in Image Tools | **⚡ Quick create** on `/new` (remembered) |
| One task | `/quick <text>`, or a photo/album captioned `/quick …` | select the mode before submitting |
| New picture, same post | **Redo images** | image tools on the task page |

Both modes expose the same image actions, flags, target language and density controls.
In quick mode, automatic/keep/omit/text-only actions default to a verified information card;
a source-dependent action without source images also uses an information card. Other actions
are honored, with a standard information-card fallback if they produce no usable generated image.
The same configured image limit and R2 budgets apply. Missing facts, failed generation or
failed QC cannot be bypassed to satisfy the image requirement: the draft stays `needs_review`
and cannot be approved until it has a verified generated image. Image-only retries retain
the post and rebuild images from source facts, including drafts created by older quick versions.

## Reading links

Linked pages are read by the worker for **both channels**: web submissions when *Read the
linked pages* is ticked, and Telegram messages per `WEB__FETCH_TELEGRAM_LINKS` (`short` =
only when the post is little more than the link, `always`, `off`). At most
`WEB__FETCH_MAX_LINKS` (3) per task; `t.me` links and tracking parameters are dropped.

Strategies are tried in `WEB__FETCH_STRATEGIES` order until one returns the real article.
Anti-bot or verification pages (WeChat's **环境异常** / "完成验证后即可继续访问", Cloudflare
"Just a moment…", captchas) and near-empty JS shells count as failures:

1. **direct**: plain HTTP with desktop-browser headers, then again as a phone / WeChat in-app
   browser. Honours `<meta charset>` (GBK/Big5 sites).
2. **reader**: a reader service (`WEB__FETCH_READER_URL`, default `https://r.jina.ai/`) renders
   the page from its own network. **This sends the URL to that service**; set the URL to empty
   to keep everything on your server. Many VPS networks are refused anonymous use. A free key
   (`WEB__FETCH_READER_API_KEY`, env-only) fixes that.
3. **browser**: headless Chromium via Playwright for JavaScript-rendered pages. Optional:
   `pip install -e .[browser]` (or `pip install playwright`). It uses Playwright's own
   Chromium (`playwright install chromium`) or an installed Chrome/Chromium on `PATH` /
   `WEB__FETCH_BROWSER_PATH`. Images, fonts and media are not loaded, and **every request the
   page makes** passes the same public-host check.
4. **llm**: a model with web access behind your CPA proxy (`WEB__FETCH_LLM_MODEL`, e.g. a
   search/browsing model; empty = off). It is told to return `ok:false` rather than guess.
   Its text is marked *AI-retrieved*: a warning on every draft, plus a review item on
   standard (non-direct) tasks, because it can't be checked against the page. On the web
   page it can be switched off per submission.

Every attempt is logged on the task timeline (`links` step), for example
`direct: blocked (环境异常); reader: ok, 5210 chars`. Pages that still can't be read leave a
warning on the draft. Test a URL from Telegram with **`/fetch <url>`**, or with **Test
links** on `/new`.

## Using the bot

- `/start` or `/menu` show these buttons:
  - **🔄 Refresh models** reads CPA `/v1/models` and TypeSafe `/v1/models`.
  - **🩺 Test connections** checks MySQL, R2, R2 budget usage, CPA, Jev, and Telegram.
  - **📋 Recent tasks** lists the latest tasks.
- In manual mode, each regular forward or Telegram album becomes its own task.
- Enable **Automatic publishing bundle** in Image Tools for a remembered personal workflow:
  forwards within 3 seconds form one task; source images are recreated as original visuals,
  and text-only input receives an original information card. No source-edit permission dialog
  is needed, because this mode does not edit/repost the original files.
- Final images carry the X draft as a caption. The original status reply becomes the review card, showing ⛔ blocking problems,
  🔎 items to review, image decisions and warnings. Single-photo bundles also carry **Redo images**
  and **Image Tools** buttons; albums use the related card. Oversize captions remain intact in a
  related text reply. Image-only reruns keep the draft unchanged.

  If images can't be fetched, the card says so and the text is still sent.
- **`draft_ready`** means every check passed, and you can **Approve**.
- **`needs_review` with only 🔎 items** means a human must check something before posting:
  - background facts the LLM added;
  - numbers that only LLM-extracted facts back up;
  - Jev's risk flags;
  - images that need review.

  After checking, use **Approve (reviewed)**.
- **⛔ problems** mean the draft can't be approved (for example, a fabricated number or
  clickbait). Regenerate or reject it.
- **Approve** frees the task's R2 assets unless `STORAGE__RETAIN_APPROVED_ASSETS=true`. The
  images were already delivered with the draft. **Reject** deletes all of the task's R2
  objects. **Regenerate** deletes them and processes the task again from the Telegram
  originals.

## Storage policy in short

- **Never stored:** incoming media for triage, skipped or unsuitable tasks, failed tasks,
  and temp files (processing happens in memory).
- **Stored, deduplicated by SHA-256:** final draft images while a decision is pending.
  Optionally, compressed review copies are also stored (`STORAGE__PERSIST_REVIEW_MEDIA`,
  off by default).
- **Released:** everything on reject or regenerate. On approve, everything is released unless
  `STORAGE__RETAIN_APPROVED_ASSETS=true`, which keeps the final assets long-term.
- **Compression:** screenshots and charts stay lossless PNG (max 4096 px). Photos become
  JPEG at quality 85 (max 2048 px). Clean Telegram JPEGs are kept as-is. Metadata is
  stripped.
- **Budget:** `STORAGE__BUDGET_BYTES` (default 9 GiB). Above it, images are flagged for
  review instead of uploaded. Usage is shown on the dashboard and in the health check.

## Media ownership

Media is treated as **third-party** unless its forward source is listed in
`PIPELINE__OWNED_SOURCE_IDS`, or it is a direct upload and `PIPELINE__DIRECT_UPLOADS_OWNED=true`.

Third-party media is never kept or enhanced. Informational visuals are localized/recreated
from extracted facts. Channel overlays trigger a new original information card, never a
watermark-removal edit; normal brand/product logos are distinguished from those overlays.
Unlicensed real-person/news photos become clearly non-documentary information cards when
facts are available, otherwise review. Rights are confirmed per media item, including
mixed-origin albums. Every generated/edit output is verified against source facts and the
original image; failed checks never become final assets.

## Telegram progress and publishing replies

Each incoming task immediately gets one receipt replying to the first forwarded message.
Automatic mode groups messages within the existing three-second idle window (bounded by
12 seconds/20 messages); one burst shares one receipt. The receipt message ID is persisted
in `tasks.envelope.status_message_id`. Queue, editorial work, image generation/retry, QC,
R2 storage and delivery update this same message. Final review reasons and action controls
replace its progress text. The publishing draft/photo or album replies directly to the original
forward, with the draft as its caption when it fits Telegram's 1024 UTF-16-unit caption limit.
Longer drafts are delivered in full as an additional source-threaded text reply. Multi-photo
outputs are Telegram albums, rather than a single photo message. Transport failures retain
stage-specific diagnostics on the status card; status edit failures never suppress delivery.

Image Tools remembers the operator's **My forwarded media is authorized for editing**
declaration. It defaults off for new operators and is enabled for an operator only on their
explicit declaration. Every new task snapshots that choice; each downloaded image receives
its own grant in `tasks.envelope.image_edit_authorizations`, bound to task ID, image index,
operator and source SHA-256. This does not change the global owned-source configuration.
Disabling the declaration affects future tasks. Grants never permit removing photographer,
author or copyright marks; scoped promotion cleanup still preserves protected areas and
must pass QC. Documentary scenes and identities cannot be invented even for authorized images.

Publishing drafts open directly with the topic. Necessary source attribution/uncertainty stays
in the body; generic source-preface openers and verification/compatibility/testing footers
trigger rewriting. Image text contains only explicitly supported facts, with no invented
installation instructions or advisory labels. Internal risks and review reasons remain available
on the status card instead of being appended to the publishing draft.

## Image Tools and remembered choices

Open Telegram `/images`, or **Image Tools** from `/menu` or a draft preview. The task preview
keeps its compact approve/regenerate/reject controls and one Image Tools entry. Selections
are automatically remembered **per operator**, including action, option checkboxes, preset,
information density and image language. New tasks capture those choices on intake. Language
settings also mark the current saved selection. Preferences survive restarts; there is no
separate Save step for image choices.

| Primary action | Behavior |
|---|---|
| `KEEP` | Keep a confirmed owned/authorized source unchanged |
| `ENHANCE` | Improve quality while preserving original content and documentary meaning |
| `LOCALIZE` | Translate explanation text; authorized source edits or original informational redraws |
| `RECREATE` | Build an original visual from source-confirmed facts |
| `CLEAN_RECREATE` | Build a new original visual excluding channel/promotion noise; never erase a source watermark |
| `INFO_CARD` | Create an original non-documentary information card for the whole task |
| `GENERATE` | Generate one original publishing visual, including tasks with no source image |
| `OMIT` | Omit images |
| `TEXT_ONLY` | Deliver only the text draft |

Primary actions are mutually exclusive. The first-use **Automatic** choice retains the prior
rights-aware policy. Options compose: preserve brands/identifiers, translate explanation text,
prefer a similar layout or allow redesign, exclude promotional overlays, prioritize factual
accuracy/visual quality, or minimize changes. Conflicting layout preferences replace the prior
choice; safety, source rights and factual fidelity always override preferences. Authorized
informational localization may use a new design when redesign is selected. Documentary photos
are never reconstructed into invented news scenes; original new cards are clearly non-documentary.
Source-dependent actions on a text-only task return review guidance to select Generate/Info card.

Information density defaults to `medium`: `low` uses one core point and minimal text;
`medium` uses a short supported hook/title with about 2–4 valuable points; `high` may add
technical details using short labels/grouped cards. Never invent points to fill a template.
All new images must remain readable on the mobile X feed. `zh-CN` uses Simplified Chinese,
`en-US` uses English; factual brand names, products/models, protocols and identifiers retain
their spelling. Image language defaults to following the task; an explicit remembered override
is shown in the publishing bundle while the existing draft and UI retain their task language.

Generators consume a **verified fact packet** with exact evidence spans from cleaned core
content/source OCR, checked for numeric traceability and semantic entailment. This means
source-supported, not independently proven true. Editorial background and the final post are
never evidence for new image claims. The final hook/angle guides presentation only. Image2.5
uses the existing configurable `models.image_model`/CPA transport. Every edited, recreated or
generated result undergoes Vision verification of facts, numbers, dates, names, brands,
product/model/protocol identifiers, language, density and readability. Informational redesign
checks meaning rather than pixel/layout similarity; documentary edits preserve people/content.
Any failed/missing check is REVIEW, and no failed candidate becomes a final asset.

Choose **Run images only** in a task's tools to reprocess images without rewriting its text.
This queues an atomic, recoverable `images_only` job through the existing worker pool. Its
stored draft stays byte-identical; image errors/timeouts preserve the draft. Existing sources
are re-fetched when necessary, and fact packets are backfilled for older drafts from cleaned
core/source OCR. New assets are prepared/QC-checked before old R2 references are released;
reference counting, compression, budget limits and retention policies remain in place. Telegram
returns the resulting images plus the existing post, and failed visuals remain review items.

Preferences use typed, non-secret `image_preferences:<user_id>` rows in the existing settings
table; runtime config queries exclude that namespace. No database migration is required.
To add an action, extend `ImageAction`, its `ACTIONS` strategy/preset entry and locale data.
For options, extend `ImageOption` and locale guidance/conflict metadata. Telegram builds menus
from these registries and the pipeline executes the shared keep/edit/create/omit strategies.
A fundamentally new execution strategy needs its own executor, not more callback branches.

Implementation references: [Telethon inline buttons](https://docs.telethon.dev/en/stable/modules/custom.html#telethon.tl.custom.button.Button.inline)
and [callback query answer/edit](https://docs.telethon.dev/en/stable/modules/events.html#telethon.events.callbackquery.CallbackQuery).
Callbacks contain short identifiers (at most 64 bytes), acknowledge promptly, use idempotent
option set/unset values and edit submenus in place. Operator/task/chat ownership is checked
before task operations. Natural-writing guidance is informed by
[blader/humanizer](https://github.com/blader/humanizer): concrete details, natural rhythm and
less filler, without fabricated personal experience or mistakes. No detector dependency or
external text submission is added, and passing AI detectors is neither tested nor promised.

## Debugging on a VPS

- `LOG_JSON=false` gives readable logs. Each line during processing carries `task_id=...`.
- The admin **Task** page shows:
  - the full timeline;
  - Jev's typed answers (route, value, confidence, nouls);
  - the evaluation JSON;
  - every rewrite attempt with its guard problems;
  - the stored assets.
- `GET /healthz` needs no auth. `GET /api/tasks/<id>` returns a task as JSON (auth required).
- Secrets are masked in all log output. That covers known values, including the Jev, CPA,
  and R2 keys, plus common token patterns.

## Language settings

Both `en-US` and `zh-CN` are supported. Use **Language settings** from Telegram
`/menu` or `/settings`, or the language selector in the admin header/settings form.
Both controls update the same runtime `default_locale` in MySQL. The selected language
controls messages, buttons, status/review reasons, editorial rules/hooks, drafts and image
instructions, including recreated infographic text. The market remains independently configurable.

Tasks capture their locale on intake so switching during processing cannot mix languages
within a bundle. Existing drafts retain their language; **Regenerate** adopts the current
locale/market. Source text, OCR, names/identifiers and technical logs retain their original
spelling for audit. Brand/product names, technical identifiers, dates and numbers are not
blindly translated. Chinese informational text must use Simplified Chinese.

Locale packs have complete matching keys; prompts and `knowledge.json` provide localized
rules and hook defaults without a database migration. Existing locale/market-specific database
rules and hooks override those defaults. Unsupported locales are rejected by configuration validation.
A separate typed language check verifies generated editorial/vision narrative, with rewrite retries;
invalid-language drafts are never delivered. This adds model calls under the existing text semaphore.

The image transport continues to use `models.image_model` and the existing CPA configuration.
Select the exact Image2.5 model identifier exposed by your provider in the existing model setting;
this change does not rename provider model IDs or alter production configuration.

## Reliability behaviour

| Failure / risk | Behaviour |
|---|---|
| Jev disabled, unconfigured, timeout, or error | Warning logged; the task continues and the main LLM decides |
| Telegram media download fails | Affected images → REVIEW with the reason; text draft continues |
| Image model / QC / R2 upload fails | Affected image → REVIEW with the reason; text draft continues |
| Quick create: image generation fails | Standard image handling and QC; missing generated image blocks approval until retried successfully |
| A linked page blocks bots ("环境异常", captcha, JS shell) | Next strategy (phone UA → reader → browser → AI); every attempt on the timeline |
| R2 fetch fails while sending a draft | The text and review card are still sent, with a note that images are unavailable |
| Image2 output fails visual QC | → REVIEW; can be kept as a review copy if enabled |
| LLM adds background facts | Draft → `needs_review`; a human must check them before approving |
| Two workers or processes pick the same task | Atomic MySQL claim (`UPDATE … WHERE status='received'`): only one runs it |
| Two devices act on one task at once (e.g. Telegram + web) | Operator actions and pipeline results are compare-and-set (`… AND status IN (…)`); the loser gets "task changed, refresh" and a stale run can never resurrect a rejected task |
| Worker cancelled (shutdown) | Its claim is released and the task returns to `received` |
| Process crashes mid-task | Its claim expires after `task_timeout + 120s`; the sweeper requeues the task |
| PyMySQL thread safety | One connection per operation, created, used, and closed in one worker thread |

## Security notes

- **CSRF:** every state-changing admin request must come from the same origin (`Origin`/`Referer` = request host, or `ADMIN__TRUSTED_ORIGINS`). Cross-site form posts are refused with 403 even though the browser would re-send Basic credentials.

- Only `TELEGRAM__ALLOWED_USER_IDS` can use the bot. An empty list means nobody can.
- **Link reading is SSRF-guarded** in every strategy: only public http(s) hosts, redirects
  re-checked, and in the headless browser every sub-request is checked as well.
- The admin UI uses HTTP Basic auth and should be reached only over localhost or a tunnel.
- **API keys are environment-only.** The CPA, Jev, and R2 keys, and every other
  credential, are read from `.env` (`chmod 600`). They can't be edited in the admin UI and
  are never written to MySQL. If such rows already exist in `settings`, they are ignored with
  a warning. The settings page only shows whether each key is set.
- `.env`, Telegram sessions, keys, local media, and caches are excluded by `.gitignore`.

## Roadmap

- An X API publisher adapter behind Approve
- Video support (keyframes and review)
- Per-user locale selection, plus additional markets
- Calibrating Jev thresholds against the operator's approve and reject history
- A reconciliation job that lists R2 and deletes orphaned objects
- Prometheus metrics

## License

MIT
