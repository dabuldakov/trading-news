#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.test.yml"
cleanup() {
  $COMPOSE down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

$COMPOSE up -d --wait
export DATABASE_URL="postgresql://news:news@127.0.0.1:55432/news_test"
export SERVICE_KEY="test-key"
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

exec .venv/bin/python -m pytest --cov=trading_news --cov-report=term "$@"
