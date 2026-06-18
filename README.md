# 📄 DanSun - Article Downloader Telegram Bot

A Telegram bot that downloads research papers from **18+ sources** using a single DOI. It chains through legal open-access repositories first, then falls back to broader sources, maximizing the chance of finding any paper.

> **⚠️ Educational & Personal Use Only**
> This bot is provided **for educational and research purposes only**. Users are responsible for complying with applicable copyright laws and the terms of service of each source. The authors do not host, store, or distribute any copyrighted content — the bot merely fetches publicly available resources on behalf of the user. Use at your own risk.

## ✨ Features

- **Single DOI input** — send a DOI, get a PDF (or metadata if no PDF is found)
- **18-source fallback chain** — tries every known source automatically
- **NIH PoW solver** — solves SHA-256 hash puzzles to access PubMed Central papers
- **Tor proxy support** — route downloads through SOCKS5
- **Admin panel** — `/stats`, `/ban`, `/unban`, `/broadcast`
- **Channel-gated access** — restrict usage to channel members
- **Rate limiting** — configurable per-user limits
- **Health server** — built-in HTTP endpoint for Render / UptimeRobot monitoring

## 📚 Supported Sources

| # | Source | Type | Requires |
|---|--------|------|----------|
| 1 | [Unpaywall](https://unpaywall.org/) | Legal OA | Email (env var) |
| 2 | [OpenAlex](https://openalex.org/) | Legal OA | None |
| 3 | [CORE](https://core.ac.uk/) | Legal OA | API key (env var) |
| 4 | [Semantic Scholar](https://www.semanticscholar.org/) | Legal OA | None |
| 5 | [Zenodo](https://zenodo.org/) | Legal OA | None |
| 6 | [Internet Archive Scholar](https://scholar.archive.org/) | Legal OA | None |
| 7 | [BASE](https://www.base-search.net/) | Legal OA | None |
| 8 | [PubMed Central](https://www.ncbi.nlm.nih.gov/pmc/) | Legal OA | None |
| 9 | [Europe PMC](https://europepmc.org/) | Legal OA | None |
| 10 | [bioRxiv](https://www.biorxiv.org/) | Preprint | None |
| 11 | [medRxiv](https://www.medrxiv.org/) | Preprint | None |
| 12 | [arXiv](https://arxiv.org/) | Preprint | None |
| 13 | [ResearchGate](https://www.researchgate.net/) | Academic network | None |
| 14 | [Sci-Hub](https://sci-hub.se/) | Shadow library | Optional `scihub` package |
| 15 | [Library Genesis](https://libgen.is/) | Shadow library | Optional `libgen-api` package |
| 16 | [Z-Library](https://z-lib.io/) | Shadow library | Optional `zlibrary-sync` package |
| 17 | [STC/Nexus](https://github.com/anisoptera/geck-stc) | Distributed search | Optional `geck-stc` package |

Shadow-library sources are **optional** (skip gracefully if not installed) and tried only after all legal sources fail.

## 🚀 Quick Start

### Prerequisites

- Python **3.10+**
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Installation

```bash
# 1. Clone the repo
git clone https://github.com/sunsorady/Dansun_paper.git
cd "DanSun - Article Downloader Telegram Bot"

# 2. Install core dependencies
pip install python-telegram-bot requests

# 3. (Optional) Install paper-source packages
pip install scihub           # Sci-Hub
pip install libgen-api       # Library Genesis
pip install zlibrary-sync    # Z-Library
pip install geck-stc         # STC/Nexus

# 4. Set environment variables
set TELEGRAM_BOT_TOKEN=your_token_here
set UNPAYWALL_EMAIL=your@email.com
set CORE_API_KEY=your_core_api_key   # Optional (get from https://core.ac.uk/)

# 5. Run
python bot.py
```

### Using `run.bat.example`

Copy `run.bat.example` to `run.bat`, fill in your values, then double-click.

```batch
@echo off
set TELEGRAM_BOT_TOKEN=your_token_here
set UNPAYWALL_EMAIL=your@email.com
set CORE_API_KEY=your_core_api_key
python bot.py
pause
```

## 🔧 Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | **Yes** | — | Bot token from @BotFather |
| `UNPAYWALL_EMAIL` | No | `sunsorady32@gmail.com` | Email for Unpaywall API |
| `CORE_API_KEY` | No | `""` | API key for CORE (skips CORE if empty) |
| `TOR_PROXY` | No | `None` | SOCKS5 proxy, e.g. `socks5://127.0.0.1:9050` |
| `PORT` | No | `8080` | Port for the health server (Render) |

## 🤖 Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message & instructions |
| `/help` | Show help |
| `/about` | Credits & version |
| `/doi <DOI>` | Fetch a paper by DOI |
| Send any DOI in chat | Auto-detects DOIs in any message |
| `/admin <password>` | Authenticate as admin (password: `1509`) |
| `/stats` | Usage statistics (admin only) |
| `/ban <id\|@username>` | Ban a user (admin only) |
| `/unban <id\|@username>` | Unban a user (admin only) |
| `/broadcast <message>` | Message all users (admin only) |

## ☁️ Deploy to Render

The bot includes a health server on `PORT` (default `8080`) that responds `200 OK` to any HTTP request — perfect for [Render](https://render.com/) cron-job uptime monitoring.

1. Create a **Web Service** on Render
2. Set the **Start Command** to `python bot.py`
3. Add your environment variables in the Render dashboard
4. (Optional) Set up [UptimeRobot](https://uptimerobot.com/) to ping `https://your-app.onrender.com` every 5 minutes

## 📁 Data Persistence

User data and banned IDs are stored in `bot_data.json` in the working directory. This file is auto-created on first use.

## 🧠 How It Works

1. User sends a DOI
2. Bot checks channel membership & rate limit
3. Bot iterates through the fallback chain (legal → preprint → shadow library)
4. Each source is shown with a progress bar and a random status message
5. First source to return a PDF wins — file is sent to the user (≤ 50 MB)
6. If PDF > 50 MB, a direct download link is sent instead
7. If no source finds a PDF, Crossref metadata (title, authors, abstract) is returned

## 📄 License

[MIT](LICENSE)
