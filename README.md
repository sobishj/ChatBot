# Website Assistant

A multi-client AI website assistant: a chat widget that any business adds to its
website with one script line. It answers from the client's website and documents
and logs every question for analytics. Everything is managed from a web admin UI.

> Work in progress. The full README (setup wizard, clients, AI models, embedding,
> cloud vs on-premise) is written in the final build step.

## Quick start (Linux server)

```bash
./install.sh
```

Then open `http://<server>:8001/setup` (admin UI). The public chat API and widget are on port `8000`.

## Quick start (Docker Desktop, Windows/macOS)

Run `./install.sh` from Git Bash / WSL, or by hand:

```bash
cp .env.example .env      # then set POSTGRES_PASSWORD, DATABASE_URL and SECRET_KEY
docker compose up -d --build
```

## Health check

```bash
curl http://localhost:8001/health   # admin port
curl http://localhost:8000/health   # public port
```

## Tests

```bash
# once: build the image with test tools
echo "INSTALL_DEV=true" >> .env && docker compose up -d --build
docker compose exec app pytest
```

## HTTPS (optional)

Set `HTTPS_PUBLIC_DOMAIN` (and optionally `HTTPS_ADMIN_DOMAIN`) in `.env`, point DNS
at the server, then:

```bash
docker compose --profile https up -d
```
