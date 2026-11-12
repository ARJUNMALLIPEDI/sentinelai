# Host port overrides (e.g. API_PORT) come from .env when present.
-include .env
API_PORT ?= 8000

.PHONY: up down logs logs-worker test test-unit lint migrate migration fe-check fault evaluate

up:            ## Build and start the full stack
	docker compose up --build -d

down:          ## Stop the stack (keeps data volumes)
	docker compose down

logs:          ## Tail API logs
	docker compose logs -f api

logs-worker:   ## Tail worker logs
	docker compose logs -f worker

test:          ## Run all backend tests inside the api container
	docker compose exec api pytest -q

test-unit:     ## Run only unit tests (no Postgres/Redis needed)
	docker compose exec api pytest -q -m "not integration"

lint:          ## Lint, format-check and type-check the backend
	docker compose exec api sh -c "ruff check . && ruff format --check . && mypy app"

migrate:       ## Apply migrations
	docker compose run --rm migrate

migration:     ## Create a migration: make migration m="add incidents"
	docker compose exec api alembic revision --autogenerate -m "$(m)"

fe-check:      ## Type-check and build the frontend
	docker compose exec frontend npm run build

fault:         ## Inject a fault: make fault svc=orders-service type=db_connection_leak
	curl -s -X POST http://localhost:$(API_PORT)/api/v1/simulation/faults \
	  -H 'content-type: application/json' \
	  -d '{"service":"$(svc)","type":"$(type)"}'; echo

evaluate:      ## Detection quality report (false positives, time-to-detect) over 3 seeds
	docker compose exec api python -m app.detection.evaluate --seeds 1 2 3
