# Common tasks. Windows users: run from Git Bash or WSL.
PY ?= python
COMPOSE_DEV  = docker compose -f docker-compose.dev.yml
COMPOSE_PROD = docker compose -f docker-compose.prod.yml --env-file $(or $(ENV_FILE),.env.production)
TEST_DB ?= postgresql+asyncpg://ofmp:ofmp_dev_password@127.0.0.1:55432/ofmp_test

.PHONY: help dev-services dev-api dev-frontend migrate test test-db lint frontend-test e2e loadtest build up down logs backup

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

dev-services:    ## start local TimescaleDB + Redis
	$(COMPOSE_DEV) up -d

dev-api:         ## run the API on :8010 with the synthetic feed (reads .env)
	MARKET_DATA_PROVIDER=synthetic COOKIE_SECURE=0 $(PY) -m uvicorn server:app --host 127.0.0.1 --port 8010 --reload

dev-frontend:    ## run the Next.js dev server on :3100
	cd frontend && npm run dev

migrate:         ## apply database migrations (DATABASE_URL)
	$(PY) -m backend.app.cli migrate

lint:            ## ruff + eslint + tsc
	ruff check .
	cd frontend && npm run lint && npm run typecheck

test:            ## backend tests without the database
	$(PY) -m pytest -q -p no:warnings

test-db:         ## backend tests including database integration
	TEST_DATABASE_URL=$(TEST_DB) $(PY) -m pytest -q -p no:warnings

frontend-test:   ## frontend unit tests
	cd frontend && npm test

e2e:             ## Playwright against a running backend (see frontend/README.md)
	cd frontend && npx playwright test

loadtest:        ## load test a running dev backend (see docs/load-testing.md)
	$(PY) tools/loadtest.py --create-users --users 40
	$(PY) tools/loadtest.py --users 40 --conns-per-user 5 --charts 4 --duration 60

build:           ## build production images
	$(COMPOSE_PROD) build --pull

up:              ## start the production stack
	$(COMPOSE_PROD) up -d

down:            ## stop the production stack (volumes kept)
	$(COMPOSE_PROD) down

logs:            ## follow production logs
	$(COMPOSE_PROD) logs -f --tail 100

backup:          ## database backup to ./backups
	COMPOSE="$(COMPOSE_PROD)" sh deploy/scripts/backup-db.sh backups 14
