# flaver_bot_resender v2

Clean rewrite on `aiogram` with simpler flow:

- one source: `channel_post` from `TELEGRAM_SOURCE_CHANNEL_ID`
- stable startup drain with retries
- startup catch-up for last `STARTUP_BACKFILL_POSTS` posts
- album handling for live updates (`MEDIA_GROUP_MODE=combined|each|first_only`)
- better error logs without giant tracebacks for network retries
- resilient state store (best-effort persistence)

## Run

```bash
cd v2
python -m pip install -r requirements.txt
python main.py
```

## Docker / Make

```bash
cd v2
make up
make logs
make down
make reset-state
```

If you changed dependencies, always rebuild image:

```bash
cd v2
docker compose down
docker compose up -d --build
```

## Docker note

If you use Docker + volume `/data`, make sure container user can write `STATE_FILE_V2`.
For example:

- `STATE_FILE_V2=/data/state-v2.json`
- volume owner/permissions must allow writes by container user.
- to fully reset dedupe history in Docker, use `make reset-state` (removes compose volume).

## Key behavior changes vs old code

- No heavy channel history probing with `forward/copy` loops.
- Startup catch-up needs `TELEGRAM_CATCHUP_BUFFER_CHAT_ID`.
- If source channel blocks forwarding, set `TELEGRAM_CATCHUP_READ_CHAT_ID` (your user id) for copy+forward fallback.
- If Telegram proxy is unstable during startup drain, bot logs and continues to polling.
