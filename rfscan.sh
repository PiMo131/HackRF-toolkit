#!/bin/sh
# wrapper: run rfscan with the project venv
D="$(cd "$(dirname "$0")" && pwd)"
PYTHONUNBUFFERED=1 exec "$D/.venv/bin/python" -m rfscan "$@"
