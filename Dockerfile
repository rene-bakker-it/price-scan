FROM python:3.14-slim-bookworm

# uv package manager (matches the tooling used on the host).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    RESOURCE_DIR=/data \
    IGNAV_API_KEY=default_key \
    UV_FROZEN=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# System packages: cron for the scheduler and dos2unix for line-ending normalization.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron ca-certificates dos2unix \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Install the Playwright browsers used by the scrapers (frecce.py -> chromium,
# italo.py -> firefox) plus their system dependencies.
RUN uv run playwright install --with-deps chromium firefox
RUN apt clean

# Copy the rest of the application.
COPY ./app /app
COPY ./resources/scan.sh /usr/bin/scan.sh
COPY ./resources/startup.sh /app/startup.sh

# Normalize line endings in shell scripts to avoid CRLF issues when built from
# Windows-hosted worktrees, then ensure executable permissions.
RUN dos2unix /usr/bin/scan.sh /app/startup.sh \
    && find /app/resources -type f ! -name "*.db" -exec dos2unix {} + \
    && chmod +x /usr/bin/scan.sh /app/startup.sh \
    && find /app/resources -type f -name "*.sh" -exec chmod +x {} +


VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["/app/startup.sh"]
# ENTRYPOINT ["top", "-b"]