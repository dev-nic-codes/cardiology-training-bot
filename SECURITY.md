# Security policy

## Reporting a vulnerability

Please use GitHub's **Security → Report a vulnerability** flow for this
repository. Do not open a public issue for a suspected vulnerability.

Include the affected component, a minimal reproduction, the impact, and any
suggested mitigation. Use synthetic data and obvious placeholders only.

## Operational security

- Keep the Telegram bot token, owner ID, topic IDs, and AI provider key private.
- Never publish `.env`, SQLite databases, downloaded articles, logs, or backups.
- Restrict management commands with `OWNER_USER_ID`.
- Keep the Telegram group private when quizzes or answer history are private.
- Rotate any credential immediately if it is committed, logged, or shared.

Only the current `main` branch receives security updates.
