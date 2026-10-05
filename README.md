# tg-x-copilot

A copilot that turns content forwarded from Telegram into **original, useful X posts in
English for the US market**.

You forward posts to the bot: text, photos, captions, albums, or several posts at once.

1. **Jev** ([TypeSafe AI System One](https://docs.typesafe.ai/api)) triages the post with
   typed questions:
   - a value score;
   - the content type;
   - the probability that the post is promotional, risky, unverified, or has useful media.

   Each answer comes with probabilities and a confidence value. Confident low-value posts are
   skipped at once. Uncertain ones are passed on to the main LLM.
2. The **main LLM and vision model** (via a CPA proxy, OpenAI-compatible) evaluate the post
   in depth and rewrite it as a concise, natural X post. The post has an honest hook and
   practical background. Made-up facts, clickbait, and translate-and-repost are blocked by
   deterministic guards.
3. **Images** are kept, enhanced, regenerated as original visuals, or flagged for review.
   Third-party watermarks are never removed, and third-party files are never reposted.
4. You get a **draft** with buttons to approve, regenerate, or reject. You post it to X
   yourself.

**Storage is built for the R2 free tier.** Incoming media stays in memory only. Nothing is
stored for skipped or rejected content. Only final assets and, optionally, review copies are
uploaded. They are compressed and keyed by content hash, so duplicates are stored once.

Stack: Python 3.11+, FastAPI, Telethon, asyncio, httpx, Pydantic v2, MySQL (PyMySQL in
threads), Cloudflare R2 (async SigV4, Standard storage), a CPA proxy, TypeSafe Jev, and
Image2-compatible image models.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the data flow, Jev routing, storage
policy, concurrency, and content rules.

```
Telegram ─▶ Bot ─▶ Collector ─▶ normalize ─▶ MySQL + asyncio.Queue ─▶ Workers
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
  pipeline/              normalizer, collector, workers, triage, processor, guards,
                         image policy, media optimization
  services/              client hub, runtime config, asset store (R2 policy), ops
  prompts/en-US/         LLM prompts (*.md) and Jev question set (jev_triage.json)
  i18n/locales/          locale packs
  web/                   FastAPI admin UI (Jinja2)
sql/                     schema + en-US seed + DB user
deploy/                  systemd unit (not installed automatically)
tests/                   unit tests for the pure logic (triage, guards, image policy, media...)
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

### 3. Cloudflare R2

Create a bucket, for example `tg-x-copilot`, and keep it in **Standard** storage. The free
tier covers 10 GB-month of Standard storage. Create an R2 API token with **Object Read &
Write** on that bucket. The bucket can stay private: the admin UI uses presigned URLs.

### 4. TypeSafe Jev

Get an API key from TypeSafe (early access) and set `JEV__API_KEY`. The defaults point at
`https://api.typesafe.ai/v1` with the model `jev-latest`.

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

The admin UI listens on `127.0.0.1:8080`. Open it through an SSH tunnel:
`ssh -L 8080:127.0.0.1:8080 <vps>`. To run it as a service, see
`deploy/tg-x-copilot.service`.

## Using the bot

- `/start` or `/menu` show these buttons:
  - **🔄 Refresh models** reads CPA `/v1/models` and TypeSafe `/v1/models`.
  - **🩺 Test connections** checks MySQL, R2, R2 budget usage, CPA, Jev, and Telegram.
  - **📋 Recent tasks** lists the latest tasks.
- Forward one or more posts. Forwards that arrive within `PIPELINE__MERGE_WINDOW_SECONDS`
  become one task.
- Each draft arrives as three parts: the final images, a review card (problems, background
  claims to fact-check, image decisions, duplicate warnings), and the plain post text.
- **Approve** is only available when every check passed. Approving deletes the review copies.
  **Reject** deletes all of the task's R2 objects. **Regenerate** deletes them and processes
  the task again from the Telegram originals.

## Storage policy in short

- **Never stored:** incoming media for triage, skipped or unsuitable tasks, failed tasks,
  and temp files (processing happens in memory).
- **Stored, deduplicated by SHA-256:** final draft images, plus compressed review copies
  while a decision is pending.
- **Compression:** screenshots and charts stay lossless PNG (max 4096 px). Photos become
  JPEG at quality 85 (max 2048 px). Clean Telegram JPEGs are kept as-is. Metadata is
  stripped.
- **Budget:** `STORAGE__BUDGET_BYTES` (default 9 GiB). Above it, images are flagged for
  review instead of uploaded. Usage is shown on the dashboard and in the health check.

## Media ownership

Media is treated as **third-party** unless its forward source is listed in
`PIPELINE__OWNED_SOURCE_IDS`, or it is a direct upload and `PIPELINE__DIRECT_UPLOADS_OWNED=true`.

Third-party media is never kept or enhanced. Generic visuals are re-created as original
illustrations. Real-event photos, images of real people, and anything with a watermark go to
review.

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

## Adding a locale (later phases)

1. Add `i18n/locales/xx-YY.json` and `prompts/xx-YY/` (including `jev_triage.json` if the
   questions should change).
2. Seed the `locales`, `x_rules`, and `hooks` rows.
3. Set `DEFAULT_LOCALE` and `MARKET`.

Text in generated images follows the locale's `language_name`.

## Security notes

- Only `TELEGRAM__ALLOWED_USER_IDS` can use the bot. An empty list means nobody can.
- The admin UI uses HTTP Basic auth and should be reached only over localhost or a tunnel.
- Secrets edited in the admin UI are stored **in plaintext** in the `settings` table, and
  they are masked in the UI and in logs. If you prefer, keep secrets only in `.env`
  (`chmod 600`).
- `.env`, Telegram sessions, keys, local media, and caches are excluded by `.gitignore`.

## Roadmap

- An X API publisher adapter behind Approve
- Video support (keyframes and review)
- Per-task locale selection, plus a second market
- Calibrating Jev thresholds against the operator's approve and reject history
- A reconciliation job that lists R2 and deletes orphaned objects
- Prometheus metrics

## License

MIT
