.PHONY: up down logs test test-unit lint migrate migration fe-check

up:            ## Build and start the full stack
	docker compose up --build -d

down:          ## Stop the stack (keeps data volumes)
	docker compose down

logs:          ## Tail API logs
	docker compose logs -f api

test:          ## Run all backend tests inside the api container
	docker compose exec api pytest -q

test-unit:     ## Run only unit tests (no Postgres/Redis needed)
	docker compose exec api pytest -q -m "not integration"

lint:          ## Lint + type-check the backend
	docker compose exec api sh -c "ruff check . && mypy app"

migrate:       ## Apply migrations
	docker compose run --rm migrate

migration:     ## Create a migration: make migration m="add incidents"
	docker compose exec api alembic revision --autogenerate -m "$(m)"

fe-check:      ## Type-check and build the frontend
	docker compose exec frontend npm run build
