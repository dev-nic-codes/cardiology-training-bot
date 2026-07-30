# Cardiology Training Bot

[![CI](https://github.com/dev-nic-codes/cardiology-training-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/dev-nic-codes/cardiology-training-bot/actions/workflows/ci.yml)
[![CodeQL](https://github.com/dev-nic-codes/cardiology-training-bot/actions/workflows/codeql.yml/badge.svg)](https://github.com/dev-nic-codes/cardiology-training-bot/actions/workflows/codeql.yml)

This project is an owner-operated Telegram bot for a forum-based cardiology education workflow. It posts ECG self-training quizzes, cardiology literature updates, and podcast updates into separate Telegram topics.

> **Educational use only.** This software does not provide medical advice, diagnosis, or treatment and must not be used for clinical decision-making. Verify source material independently.

## What it does

- Fetches ECG quiz cases from ECG Wave-Maven in polite batches
- Caches cases locally in SQLite through SQLAlchemy 2
- Posts one ECG case into a Telegram forum topic every 3 hours
- Sends the ECG image, the vignette, and a non-anonymous quiz poll with randomized options
- Stores poll answers and tracks accuracy
- Sends the full Wave-Maven explanation back into the same topic after the answer
- Posts one new cardiology article into the articles topic every 2 hours
- Posts cardiology podcast updates into a dedicated topic
- Supports `/start`, `/id`, `/post_now`, `/post_article_now`, `/score`, `/wrong`, and `/status`

## Stack

- Python 3.12
- aiogram 3
- SQLAlchemy 2
- SQLite via `aiosqlite`
- APScheduler
- httpx
- BeautifulSoup4
- pydantic-settings
- Optional OpenAI-compatible chat-completions API for article summary rewriting

## Project layout

```text
project/
  api/
    ecg_wave_maven.py
  bot/
    config.py
    main.py
    handlers/
    services/
    database/
  docker-compose.yml
  Dockerfile
  requirements.txt
  README.md
```

## Configuration

Copy `.env.example` to `.env` and set:

- `BOT_TOKEN`
- `CHAT_ID`
- `ECG_TOPIC_ID`
- `ARTICLES_TOPIC_ID`
- `OWNER_USER_ID`
- `OPENAI_API_KEY` if you want AI-rewritten article summaries

Important notes:

- `CHAT_ID` must be the Telegram supergroup id where the forum topic exists.
- `ECG_TOPIC_ID` must be the topic's `message_thread_id` for the `ECG Challenges` topic.
- `ARTICLES_TOPIC_ID` should be the message thread id for the articles topic.
- `PODCAST_TOPIC_ID` should be the message thread id for the podcast topic.
- The bot assumes a private, owner-operated workflow. If `OWNER_USER_ID` is set, management commands are restricted to that user.
- If `OPENAI_API_KEY` is empty, the bot skips AI rewriting and uses extractor-based article summaries only.

## Local run

```bash
git clone https://github.com/dev-nic-codes/cardiology-training-bot.git
cd cardiology-training-bot
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env       # Windows: copy .env.example .env
python main.py
```

## Windows launcher

Use `start_bot.bat` to create `.venv`, install dependencies, and start the bot.

## Docker

```bash
cp .env.example .env
docker compose up --build -d
```

## Commands

- `/start` shows the bot overview
- `/post_now` posts the next cached ECG case immediately
- `/post_article_now` posts the next unseen article immediately
- `/score` shows total answers, correct answers, incorrect answers, and accuracy
- `/wrong` shows recent incorrect answers with the correct diagnosis and source
- `/status` shows ECG/article counts, last activity, and both next scheduled post times

## Notes on ECG Wave-Maven integration

- The site requires an initial cookie/bootstrap flow before case pages become available.
- The site also uses legacy TLS. The client enables OpenSSL's legacy server connect option so Python can still fetch cases.
- Some Wave-Maven cases have built-in answer choices and some do not. When a case has no source choices, the bot generates distractors from other cached diagnoses.
- The parser includes TODO comments where HTML selectors are most likely to require tuning if the source markup changes.

## Data model

Main tables:

- `ecg_cases`
- `posted_articles`
- `posted_quizzes`
- `user_answers`
- `bot_state`

SQLite is the default for MVP, but the app is structured around SQLAlchemy async sessions so moving to PostgreSQL later is straightforward by changing `DATABASE_URL`.

## Validation

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
docker build --tag cardiology-training-bot:test .
```

CI runs the Python checks and a clean Docker build on every push and pull request. CodeQL performs a separate static security analysis.

## Source responsibility

The bot fetches material from third-party publishers and ECG Wave-Maven. Respect each source's terms, rate limits, attribution requirements, and content rights. The configured delays and caching reduce repeated requests but do not replace operator responsibility.

## Security

Read [SECURITY.md](SECURITY.md) before reporting a vulnerability. Never publish a real `.env`, bot token, API key, Telegram identifier, database, downloaded document, or production log.

## License

Copyright © 2026 dev-nic-codes. All rights reserved. This repository is source-available, not open source. See [LICENSE](LICENSE).
