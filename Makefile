.PHONY: install lint test migrate up down logs sync

install:
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -e ".[dev]"

lint:
	.venv/bin/ruff check .

test:
	.venv/bin/pytest

migrate:
	.venv/bin/python -m trading_news.migrate

up:
	docker compose -p news up -d --build

down:
	docker compose -p news down

logs:
	docker compose -p news logs -f --tail=100

sync:
	.venv/bin/python -m trading_news.reference.sync
