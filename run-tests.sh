#!/usr/bin/env bash
# Run Wojtek Stock's tests in a throwaway Python 3.12 container: nothing to install, and your settings, data and store
# are never used. The tests talk to a pretend WooCommerce store (tests/mock_woo.py) with a temporary database.
# About 3 minutes; the last line says how many passed, and names any that failed.
set -euo pipefail
cd "$(dirname "$0")"
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
exec $DOCKER run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD":/app:ro -w /app python:3.12-slim python tests/test_stock.py "$@"
