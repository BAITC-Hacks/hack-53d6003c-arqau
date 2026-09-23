.PHONY: install run test import

install:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e '.[dev]'

run:
	.venv/bin/uvicorn app.main:app --app-dir backend --reload

test:
	.venv/bin/pytest

import:
	@test -n "$(DIR)" || (echo "Usage: make import DIR=/path/to/extra-data" && exit 2)
	curl --fail-with-body -X POST \
		$(foreach file,$(wildcard $(DIR)/skills.json $(DIR)/employees.json $(DIR)/events.json $(DIR)/activity_history.csv),-F "files=@$(file)") \
		http://127.0.0.1:8000/import
