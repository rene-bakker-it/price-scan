#!/usr/bin/env bash
set -euo pipefail

RESOURCE_DIR="${RESOURCE_DIR:-/data}"
export RESOURCE_DIR

# Make sure the persistent database directory exists.
mkdir -p "${RESOURCE_DIR}"

# Install the crontab shipped in resources/, injecting the environment variables
# the scheduled scrapers need so they write to the same persistent database as
# the API. cron jobs do not inherit the container ENV, hence the prepend.
{
    echo "RESOURCE_DIR=${RESOURCE_DIR}"
    echo "PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
    echo "IGNAV_API_KEY=${IGNAV_API_KEY:-}"
    echo "UV_FROZEN=1"
    cat "/app/resources/crontab"
} | crontab -

# Start the cron daemon in the background.
cron

# Run the FastAPI application in the foreground as PID 1.
cd /app
exec uv run uvicorn main:app --host 0.0.0.0 --port 8000
