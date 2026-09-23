.PHONY: install run dev test build-frontend docker import

PYTHON ?= python3

install:
	@$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e '.[dev]'
	npm --prefix frontend ci

run:
	.venv/bin/uvicorn app.main:app --app-dir backend --reload

dev:
	@.venv/bin/uvicorn app.main:app --app-dir backend --reload & backend_pid=$$!; \
		npm --prefix frontend run dev & frontend_pid=$$!; \
		trap 'kill $$backend_pid $$frontend_pid 2>/dev/null || true' INT TERM EXIT; \
		wait

test:
	.venv/bin/pytest
	npm --prefix frontend test

build-frontend:
	npm --prefix frontend run build

docker:
	docker compose up --build

import:
	@test -n "$(DIR)" || (echo "Usage: make import DIR=/path/to/extra-data" && exit 2)
	@TOKEN=$$(curl --silent --fail-with-body -X POST \
		-H 'Content-Type: application/json' \
		-d '{"role":"hr"}' \
		http://127.0.0.1:8000/auth/demo-login | \
		.venv/bin/python -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'); \
	 curl --fail-with-body -X POST \
		-H "Authorization: Bearer $$TOKEN" \
		$(foreach file,$(wildcard $(DIR)/skills.json $(DIR)/employees.json $(DIR)/events.json $(DIR)/activity_history.csv),-F "files=@$(file)") \
		http://127.0.0.1:8000/import
