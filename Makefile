.RECIPEPREFIX := >

up:
>docker compose up -d --build --force-recreate

down:
>docker compose down

restart:
>docker compose down
>docker compose up -d --build

logs:
>docker compose logs -f --tail=200

ps:
>docker compose ps

build:
>docker compose build --no-cache

run-local:
>python main.py

reset-state:
>docker compose down -v
