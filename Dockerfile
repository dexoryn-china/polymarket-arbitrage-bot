# ──── Stage 1: builder ──────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system deps needed for building lxml
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libxml2-dev libxslt-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# Install build deps in a venv so we can copy only what we need
RUN python -m venv /opt/venv \
    && . /opt/venv/bin/activate \
    && pip install --no-cache-dir --upgrade pip setuptools wheel

COPY . .
RUN . /opt/venv/bin/activate \
    && pip install --no-cache-dir ".[prod]"

# ──── Stage 2: runtime ─────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="polymarket-bot" \
      description="Polymarket Research & Trading Bot"

# Install runtime C-libs (lxml, libxml2)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libxml2 libxslt1.1 curl && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -r botuser && useradd -r -g botuser botuser

WORKDIR /app

# Copy only the installed venv from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create writable dirs for data/logs/backups
RUN mkdir -p /app/data /app/logs /app/reports /app/data/backups \
    && chown -R botuser:botuser /app/data /app/logs /app/reports

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:2345/health || exit 1

EXPOSE 2345

USER botuser

ENTRYPOINT ["bot"]
CMD ["dashboard"]
