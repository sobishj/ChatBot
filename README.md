# Website Assistant

An AI chat assistant that any organisation (malls, hotels, hospitals, universities, airports,
restaurants, dealerships, real estate, government sites) can add to its website with **one
script line**. It answers visitors' questions from the client's **website** and **documents**,
in the visitor's language, and logs every question for analytics.

Everything is managed in a web admin UI. The terminal is needed once, to run `install.sh`.

```
 visitor's browser                         your server (Docker)
┌───────────────────┐   widget.js    ┌──────────────────────────────────────────────┐
│ client website    │ ─────────────▶ │ app  :8000  public chat API + widget.js      │
│  <script …>       │ ◀───────────── │      :8001  admin UI (React) + admin API     │
└───────────────────┘   /api/chat    │ worker      crawls, indexing, scheduler      │
                                     │ db          PostgreSQL 16 + pgvector         │
                                     └──────────────┬───────────────────────────────┘
                                                    │ LiteLLM
                                   any LLM: OpenAI, Claude, Gemini, Kimi, DeepSeek,
                                   Mistral, Groq, OpenRouter, Bionic/Ollama/vLLM/LM Studio
```

**How an answer is made:** the question is masked (phone numbers and emails removed). Then
hybrid search runs: pgvector similarity plus Postgres full-text search, merged with reciprocal
rank fusion and always filtered by client. The model gets the top 6 passages plus the last 4
messages of the conversation. The answer comes back with its sources and is logged. Answers
with low confidence are marked *unanswered* so they show up in Stats.

---

## Contents

1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Setup wizard](#3-setup-wizard)
4. [Adding a client](#4-adding-a-client)
5. [Website crawling](#5-website-crawling)
6. [Documents: upload and watched folders](#6-documents-upload-and-watched-folders)
7. [AI models](#7-ai-models)
8. [Embedding the widget](#8-embedding-the-widget)
9. [Users and roles](#9-users-and-roles)
10. [Cloud vs on-premise](#10-cloud-vs-on-premise)
11. [HTTPS](#11-https)
12. [Backups and restore](#12-backups-and-restore)
13. [CLI (developers and recovery)](#13-cli-developers-and-recovery)
14. [Development and tests](#14-development-and-tests)
15. [Configuration reference](#15-configuration-reference)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. Requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Any Linux with Docker (Ubuntu 22.04+/Debian 12) | Ubuntu 24.04 LTS |
| CPU | 4 cores | 8 cores |
| RAM | **8 GB** | 16 GB (more if a local LLM runs on the same machine) |
| Disk | 20 GB | 50 GB+ |

The embedding model (BAAI/bge-m3, about 2.3 GB) is loaded by both the app and the worker, so
each process needs about 2.5 GB of RAM. A local LLM such as Qwen needs its own memory, or a GPU.

## 2. Install

### Linux server

```bash
git clone https://github.com/sobishj/ChatBot.git website-assistant
cd website-assistant
./install.sh
```

`install.sh` does the following:
- checks for Docker and offers to install it with Docker's official script
- creates `.env` with a random `SECRET_KEY` and database password
- builds and starts the containers
- waits until the app is healthy, then prints the setup URL

```
Open http://<server>:8001/setup to finish setup
```

Running it again is safe: it keeps the existing `.env`.

Optional: choose other ports with `APP_PORT=9000 ADMIN_PORT=9001 ./install.sh`.

### Docker Desktop (Windows / macOS)

Run `./install.sh` from Git Bash or WSL. If you prefer to do it by hand:

```bash
cp .env.example .env     # set POSTGRES_PASSWORD, DATABASE_URL (same password) and a long SECRET_KEY
docker compose up -d --build
```

> **Never change or lose `SECRET_KEY`** after setup. It encrypts the saved AI provider API
> keys. Keep a copy of `.env` somewhere safe.

### GPU acceleration (optional)

Indexing time is almost all embedding. On a CPU, bge-m3 needs roughly 1–2 seconds per PDF
page (a 1,000-page PDF takes about 15 minutes on an 8-core server); an NVIDIA GPU is
typically 20–50× faster. By default the image is built for CPU only. To use a GPU:

1. Install the NVIDIA driver and the
   [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
   on the host. `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` must work.
2. In `.env` set:
   ```
   TORCH_VARIANT=cu124
   COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
   ```
   (On Windows, separate the two files with `;` instead of `:`.)
3. `docker compose up -d --build` (the CUDA image is a few GB larger).
4. In **Settings → Compute device** choose **GPU**. The page shows the GPU the worker detected
   and what is in use. No restart or re-index is needed, and switching back to CPU works the same way.

If GPU is selected but no GPU is available, the app keeps working on the CPU and the page shows a warning.

### Embedding models: local or cloud

**Settings → Embeddings** lists the available models with their languages, the time for
1,000 pages on this server (measured once something has been indexed) and, for cloud models,
the cost:

| Model | Where | Notes |
|---|---|---|
| `BAAI/bge-m3` (default) | Local | Best local quality, 100+ languages; slow without a GPU |
| `intfloat/multilingual-e5-small` | Local | ~10× faster on a CPU, ~100 languages, somewhat weaker search |
| `BAAI/bge-small-en-v1.5` | Local | Fast, English only |
| OpenAI `text-embedding-3-small` | Cloud | Thousands of pages in a minute or two, ~$0.02 per 1M tokens |
| OpenAI `text-embedding-3-large` | Cloud | Most accurate OpenAI model |
| Google `gemini-embedding-001` | Cloud | Strong multilingual quality, free tier with rate limits |
| Mistral `mistral-embed` | Cloud | Inexpensive, best for European languages |

Cloud models need an API key (leave it blank to reuse the key of a saved AI model from the same
provider) and send document text and visitor questions to the provider. Switching models
re-indexes all content once.

When an upload would take a local model more than about 10 minutes, it isn't indexed right
away: the Documents tab shows the estimate next to each cloud model's time and cost, and the
admin chooses to index locally anyway or switch to a cloud model. Files from watched folders
are always indexed with the current model.

## 3. Setup wizard

Open `http://<server>:8001/setup`. The wizard is available only until setup is completed.

1. **Super admin account**: name, email and password (at least 10 characters).
2. **Mode**: *Cloud* (many clients) or *On-premise* (one client). See [section 10](#10-cloud-vs-on-premise).
3. **Public domain**: the address websites load the widget from, e.g. `chat.sprintgames.online`
   (no `http://`). For local testing you can use `localhost:8000`.
4. **First AI model**: pick a provider, then fill in the URL, model name and API key.
   **Test connection** must succeed before you can continue.
5. **Embedding model**: `BAAI/bge-m3` by default (multilingual, including Malayalam). It
   downloads once, with a progress bar, and you can continue while it downloads.
6. **Client** (on-premise only): the single organisation this server serves.
7. **Done**: opens the dashboard.

## 4. Adding a client

Go to **Clients → Add client** and fill in:
- **Name**
- **Client ID**: lowercase letters, digits and hyphens, e.g. `lulu-kochi`. It appears in the
  embed code and can't be changed later.
- **Website URL**
- **Allowed domains**: the websites allowed to show the widget. Defaults to the website's
  domain.

The client detail page has these tabs:

| Tab | What it does |
|---|---|
| Overview | Counts, running jobs, activate/deactivate, delete |
| Website | **Crawl now** with live progress, crawl settings, crawled pages (view the exact text learned, remove a page) |
| Documents | Drag-and-drop upload, watched folder, re-index |
| AI model | Which saved model answers this client (default = system default) |
| Branding | Colour, logo, bot name, greeting, language, position, "Powered by", **live preview** |
| Allowed domains | Websites allowed to use this bot (super admin only) |
| Test chat | Chat as a visitor, with sources, confidence score, model used, response time and the retrieved passages |
| Embed code | The one-line script with a Copy button, plus guides for WordPress, Wix, Shopify, Squarespace, GTM and custom HTML |
| Stats | Questions over 7/30/90 days, top 20 questions, unanswered questions, per-day chart, languages, tokens and cost |
| Conversations | Searchable question/answer log (personal data masked), CSV export |

## 5. Website crawling

**Crawl now** (Website tab) queues a job for the worker:

- Reads `robots.txt`, respecting `Disallow`, `Crawl-delay` and its `Sitemap:` entries.
- Tries sitemaps first (`/sitemap.xml`, `/sitemap_index.xml`, `/wp-sitemap.xml`, including
  index files and `.gz`). If there are none, it follows internal links from the start page.
- Stays on the same site (`www.` and the bare domain count as the same site), waits a delay
  between requests and stops at the page limit.
- **Text extraction:**
  - *Full page content* (default) removes the header, menus, footer, scripts and forms and
    keeps everything else, including small facts such as "Located on: Second Floor" or
    opening hours.
  - *Main article only* (trafilatura) suits blogs and news sites.
- Unchanged pages (same content hash) are skipped. Changed pages are re-indexed. Pages that
  disappeared (404/410, or no longer found after a complete crawl) are removed along with
  their passages.
- **Include/exclude patterns** are wildcards matched against the URL path, e.g. `/shop/*`
  or `*?print=*`.

Automatic re-crawls run daily or weekly at a time you choose (Settings → Crawl schedule).
They re-crawl every active client and re-check their documents.

## 6. Documents: upload and watched folders

Supported formats: **PDF, DOCX, XLSX, TXT, MD**, up to 1 GB each. PDF pages without a text
layer (scans) are read with Tesseract OCR, several pages in parallel (one per CPU core by default).
Set the OCR languages with `OCR_LANGS` (installed: `eng`, `mal`, `hin`, `ara`, e.g. `eng+mal`);
each extra language slows OCR down. Pages that already contain text skip OCR entirely.

**Upload** (default): drag files into the Documents tab. They're stored on the server in
`DATA_DIR/clients/<client_id>/documents/` and indexed automatically.

**Watched folder** (mainly for on-premise): the worker scans a server folder every N minutes,
and on **Scan now**. It indexes new or changed files and removes passages of deleted files.
Each file shows its source (*Uploaded* or *Folder*). Both sources can be used together.

The containers can only see folders mounted into them. The host folder `WATCHED_HOST_DIR`
(default `./watched`) is mounted **read-only** at `/mnt/watched`.

```bash
# On the host: put files (or mount a network share) under the watched folder
mkdir -p watched/hr-policies
cp ~/policies/*.pdf watched/hr-policies/

# Windows/SMB share (mount on the host, then restart so the containers see the new mount)
sudo mount -t cifs //fileserver/policies ./watched/hr-policies -o ro,username=svc_reader
docker compose restart app worker
```

Then set the folder in **Documents → Watched folder**, e.g. `/mnt/watched/hr-policies`. The
path is checked: it must exist, be readable and lie inside `/mnt/watched`.

To use a different host location, set `WATCHED_HOST_DIR=/srv/shared-docs` in `.env` and run
`docker compose up -d`.

## 7. AI models

**Settings → AI models → Add model.** All calls go through LiteLLM, so you can add or switch
models without restarting anything.

| Provider | Base URL (pre-filled) | Example model |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Anthropic (Claude) | `https://api.anthropic.com` | `claude-haiku-5-5` (fast) or `claude-sonnet-5-5` |
| Google (Gemini) | `https://generativelanguage.googleapis.com` | `gemini-3.5-flash` |
| Moonshot (Kimi) | `https://api.moonshot.ai/v1` | `kimi-k2-0905-preview` |
| DeepSeek / Mistral / Groq / OpenRouter | pre-filled | see the provider's docs |
| **Bionic**, Ollama, vLLM, LM Studio, other OpenAI-compatible | `http://host.docker.internal:<port>/v1` | e.g. `qwen2.5-7b-instruct` |

- **Test connection** sends a small prompt and shows the reply and the response time, or the
  error.
- **API keys** are encrypted at rest with `SECRET_KEY` and are never shown again; only the
  last 4 characters are displayed.
- **Default model**: used by every client that hasn't picked its own. Per-client choice is on
  client → AI model.
- **Fallback model**: used automatically when the main model fails or times out. The
  timeout is set per model under advanced options.
- **Costs**: optionally enter USD per 1M input/output tokens. Otherwise LiteLLM's built-in
  price list is used. Local models cost 0. Usage and cost appear in Stats and on the
  Dashboard.

**Local models and Docker:** inside a container, `localhost` is the container itself. Use
`host.docker.internal` to reach a model server running on the host. This works on Docker
Desktop and, thanks to `extra_hosts`, on Linux too. Make sure the model server listens on an
address Docker can reach (for example `0.0.0.0`, or the Docker bridge IP).

In on-premise mode, choosing a cloud provider shows a warning: *visitor questions and content
will be sent outside this server.*

## 8. Embedding the widget

Copy the line from client → **Embed code**:

```html
<script src="https://chat.example.com/widget.js" data-client="lulu-kochi" defer></script>
```

- It renders in a Shadow DOM, so the host page's CSS can't break it and it can't restyle the
  page.
- It shows a floating bubble that opens a chat window, with a typing indicator. It is
  keyboard accessible (Enter sends, Escape closes) and full-screen on phones.
- Branding (colour, logo, greeting, position, language, "Powered by") is loaded from
  `/api/client/<id>/config`. That response contains branding only, no secrets.
- Answers show their sources as links (web pages) or names (documents). The notice "Chats are
  recorded to improve service" is shown and editable in Settings → Privacy.
- The widget only works on the client's **allowed domains**. Requests from other origins, or
  with no origin at all, are rejected.

**Try it locally:** add `localhost:5500` to the client's allowed domains, then:

```bash
cd demo && python -m http.server 5500
# open http://localhost:5500/?client=lulu-kochi&api=http://localhost:8000
```

## 9. Users and roles

**Users** (super admin only):

| Role | Can do |
|---|---|
| `super_admin` | Everything: clients, AI models, settings, users, system health, backups |
| `client_admin` | Only assigned clients: crawl, documents, branding, test chat, embed code, stats, conversations. Cannot change AI models, allowed domains or system settings. |

Disabling a user or changing their password signs them out everywhere. There is always at
least one active super admin.

## 10. Cloud vs on-premise

The code is the same in both modes. You choose the mode in the setup wizard and can change it
later in **Settings → Clients mode**. Switching keeps all content, settings, users and statistics.
Switching to on-premise needs at most one client (delete the others first, nothing is deleted
automatically); that client becomes the assistant.

| | Cloud | On-premise |
|---|---|---|
| Clients | many | exactly one (the Clients list is skipped) |
| Typical model | hosted API (GPT, Claude, …) | local (Qwen via Bionic, Ollama, vLLM) |
| External calls | to the chosen AI provider | none needed once the embedding model is downloaded |
| Admin UI | usually behind HTTPS on its own domain | keep it internal: `ADMIN_BIND_IP=127.0.0.1` |

**Fully offline on-premise:** download `BAAI/bge-m3` once (the setup wizard does it), use a
local model, and the server needs no internet access. LiteLLM's price list is bundled, and
Hugging Face telemetry is disabled.

## 11. HTTPS

Websites served over HTTPS can only load the widget over HTTPS. Caddy is included as an
**optional** compose profile, off by default. It obtains Let's Encrypt certificates
automatically.

1. Point DNS at the server, e.g. `chat.sprintgames.online`, and optionally
   `admin.sprintgames.online`.
2. In `.env`:
   ```
   HTTPS_PUBLIC_DOMAIN=chat.sprintgames.online
   HTTPS_ADMIN_DOMAIN=admin.sprintgames.online   # optional; leave empty to keep the admin internal
   APP_BIND_IP=127.0.0.1                          # only Caddy talks to the app directly
   ADMIN_BIND_IP=127.0.0.1
   FORWARDED_ALLOW_IPS=*                          # trust Caddy's X-Forwarded-* headers
   ```
3. `docker compose --profile https up -d`
4. Set **Settings → Public domain** to `chat.sprintgames.online`.

Ports 80 and 443 must be open. Session cookies are marked `Secure` automatically when the
admin UI is reached over HTTPS.

## 12. Backups and restore

**System health → Download backup** streams a PostgreSQL dump of everything: settings,
users, clients, pages, passages, questions and jobs.

```bash
# restore into a fresh install (same SECRET_KEY in .env!)
docker compose exec -T db pg_restore -U chatbot -d chatbot --clean --if-exists < website-assistant-YYYYMMDD.dump
```

- API keys in the dump only work with the **same `SECRET_KEY`**. Back up `.env` too.
- Uploaded files, logos and the model cache live in the `appdata` volume. Back them up with
  `docker run --rm -v website-assistant_appdata:/data -v "$PWD":/out alpine tar czf /out/appdata.tgz -C /data .`

## 13. CLI (developers and recovery)

Everything is available in the UI. The CLI calls the same service functions.

```bash
docker compose exec app python cli.py crawl lulu-kochi
docker compose exec app python cli.py index-docs lulu-kochi [--force]
docker compose exec app python cli.py ask lulu-kochi "Where is the ASICS store?"
docker compose exec app python cli.py stats lulu-kochi --days 30
docker compose exec app python cli.py reset-admin-password admin@example.com
```

## 14. Development and tests

```
app/
  admin/        admin API routes, auth dependencies, CSRF middleware, SPA hosting
  api/          public API (chat, config, widget.js, logo) and /health
  analytics/    stats, conversations, CSV export, retention
  chat/         PII masking, language detection, prompt, chat service (fallback, logging)
  crawler/      robots/sitemaps/link crawler, text extraction, crawl service
  db/           SQLAlchemy models, Alembic migrations (run automatically at startup)
  documents/    PDF/DOCX/XLSX/TXT/MD readers, uploads, watched folders
  embeddings/   local models (download with progress, loading), cloud embedding APIs, model catalog
  indexer/      chunking (~500 tokens, 50 overlap), embedding, vector column management
  llm/          LiteLLM wrapper and provider presets
  search/       hybrid search (pgvector + full-text, RRF)
  security/     Argon2 passwords, Fernet API-key encryption, signed sessions, CSRF
  services/     settings, clients, users, AI models, jobs, rate limits
  worker/       job runner, handlers, scheduler
  widget/       widget.js (vanilla JS, Shadow DOM)
frontend/       React + TypeScript admin UI (built into the image by Docker)
cli.py, install.sh, docker-compose.yml, Dockerfile, deploy/Caddyfile, demo/
tests/
```

```bash
echo "INSTALL_DEV=true" >> .env && docker compose up -d --build   # include pytest + ruff
docker compose exec app pytest              # unit + integration tests (uses a separate <db>_test database)
docker compose exec app ruff check app tests cli.py

cd frontend && npm install && npm run dev   # UI dev server on :5173, proxies /api to :8001
```

Tests use a fake embedder and a fake LLM, so they need neither the 2 GB model nor a model
server.

## 15. Configuration reference

`.env` holds bootstrap settings only. Everything else lives in the database and is edited in
the UI.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | generated | SQLAlchemy URL of the Postgres database |
| `POSTGRES_USER` / `POSTGRES_DB` / `POSTGRES_PASSWORD` | `chatbot` / `chatbot` / generated | Database container credentials |
| `SECRET_KEY` | generated | Encrypts API keys, signs sessions. **Never change after setup.** |
| `APP_PORT` | `8000` | Public chat API and widget.js |
| `ADMIN_PORT` | `8001` | Admin UI |
| `APP_BIND_IP` / `ADMIN_BIND_IP` | `0.0.0.0` | Host interface to publish on (`127.0.0.1` = internal only) |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted |
| `WATCHED_HOST_DIR` | `./watched` | Host folder mounted read-only at `/mnt/watched` |
| `LOG_LEVEL` | `INFO` | |
| `OCR_LANGS` | `eng` | Tesseract languages for scanned PDFs, joined with `+` |
| `OCR_WORKERS` | `0` | Pages OCR'd in parallel (0 = one per CPU core) |
| `OCR_DPI` | `200` | Resolution scans are read at; 300 for very small print (about 40% slower) |
| `INSTALL_DEV` | `false` | Include test tools in the image |
| `TORCH_VARIANT` | `cpu` | PyTorch build: `cpu` or `cu124` for NVIDIA GPUs (see [GPU acceleration](#gpu-acceleration-optional)) |
| `COMPOSE_FILE` | unset | Set to `docker-compose.yml:docker-compose.gpu.yml` to give the containers the GPU |
| `HTTPS_PUBLIC_DOMAIN` / `HTTPS_ADMIN_DOMAIN` | empty | Domains for the optional Caddy `https` profile |

## 16. Troubleshooting

| Symptom | Fix |
|---|---|
| Widget doesn't appear | Browser console shows `[Website Assistant] not loaded: config 403`: add the site's domain under **Allowed domains**. HTTPS sites need the widget served over HTTPS. |
| "Could not connect" when testing a local model | Use `host.docker.internal`, not `localhost`, and make sure the model server listens on a reachable interface. |
| Crawl finds few pages | Check robots.txt, the include/exclude patterns and the page limit. Sites that render their content with JavaScript only aren't supported yet. |
| Answers marked unanswered although correct | Lower **Settings → Answers → confidence threshold**. Test chat shows each answer's score. |
| Worker "Problem" on System health | `docker compose logs worker` |
| Forgot admin password | `docker compose exec app python cli.py reset-admin-password you@example.com` |
| API key shows "Unreadable" | `SECRET_KEY` changed. Restore the old key or re-enter the API keys. |
