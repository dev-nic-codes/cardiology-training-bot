# Cardiology Training Bot

[![CI](https://github.com/dev-nic-codes/cardiology-training-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/dev-nic-codes/cardiology-training-bot/actions/workflows/ci.yml)
[![CodeQL](https://github.com/dev-nic-codes/cardiology-training-bot/actions/workflows/codeql.yml/badge.svg)](https://github.com/dev-nic-codes/cardiology-training-bot/actions/workflows/codeql.yml)

An owner-operated Telegram bot created by Nic for forum-based cardiology education. It publishes ECG quizzes, literature updates, and podcast updates into separate Telegram topics.

> **Educational use only.** This software does not provide medical advice, diagnosis, or treatment and must not be used for clinical decisions. Verify all source material independently.

## Features

- Fetches and caches ECG Wave-Maven cases in SQLite.
- Posts an ECG image, vignette, and randomized non-anonymous quiz poll.
- Records answers and accuracy, then returns the full explanation.
- Cycles through all available ECG cases before reusing them.
- Publishes new cardiology articles and podcast episodes on separate schedules.
- Restricts management commands to the configured owner.
- Supports optional AI-assisted article summary rewriting.

## Stack

Python 3.12, aiogram 3, SQLAlchemy 2, SQLite, APScheduler, httpx, BeautifulSoup4, and pydantic-settings.

## Configuration

Copy `.env.example` to `.env` and configure:

- `BOT_TOKEN`
- `CHAT_ID`
- `ECG_TOPIC_ID`
- `ARTICLES_TOPIC_ID`
- `PODCAST_TOPIC_ID`
- `OWNER_USER_ID`
- `OPENAI_API_KEY` only if AI-assisted summaries are wanted

The chat must be a Telegram supergroup with forum topics enabled. Topic values are Telegram `message_thread_id` values. If no AI API key is present, extractor-based article summaries are used.

Never commit the real `.env`, database, downloaded files, or logs.

## Run locally

```bash
git clone https://github.com/dev-nic-codes/cardiology-training-bot.git
cd cardiology-training-bot
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
cp .env.example .env       # Windows: copy .env.example .env
python main.py
```

Windows users can also use `start_bot.bat`.

For Docker:

```bash
cp .env.example .env
docker compose up --build -d
```

## Commands

- `/start` shows the overview.
- `/post_now` posts the next ECG case.
- `/post_article_now` posts the next unseen article.
- `/score` shows answer totals and accuracy.
- `/wrong` shows recent incorrect answers and the correct diagnosis.
- `/status` shows content counts, activity, and schedule times.

## Source handling

ECG Wave-Maven requires a cookie bootstrap flow and legacy TLS compatibility. Some cases provide answer choices; others receive distractors from cached diagnoses. The integration uses caching and measured request intervals, but operators remain responsible for third-party terms, attribution, rate limits, and content rights.

## Validation

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
docker build --tag cardiology-training-bot:test .
```

CI runs the Python checks and a clean Docker build on every push and pull request. CodeQL provides a separate static security analysis.

## Security

Read [SECURITY.md](SECURITY.md) before reporting a vulnerability. Never publish tokens, API keys, Telegram identifiers, databases, downloaded documents, or production logs. Use GitHub's private vulnerability reporting flow for security reports.

## License

Copyright © 2026 Nic. All rights reserved. This repository is source-available, not open source. See [LICENSE](LICENSE).
