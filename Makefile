.PHONY: install run test

install:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e '.[dev]'

run:
	.venv/bin/uvicorn app.main:app --app-dir backend --reload

test:
	.venv/bin/pytest

