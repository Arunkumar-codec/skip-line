# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# System deps for mysqlclient, Pillow, cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    default-libmysqlclient-dev \
    pkg-config \
    libffi-dev \
    libssl-dev \
    zlib1g-dev \
    libjpeg-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip wheel --no-cache-dir --wheel-dir /build/wheels -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Non-root user for security
RUN groupadd -r skipline && useradd -r -g skipline -d /app -s /sbin/nologin skipline

WORKDIR /app

# Runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-libmysqlclient-dev \
    libffi8 \
    libssl3 \
    libjpeg62-turbo \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install wheels from builder stage
COPY --from=builder /build/wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels

# Copy application source
COPY --chown=skipline:skipline . .

# Create runtime directories
RUN mkdir -p qr_codes receipts logs \
    && chown -R skipline:skipline qr_codes receipts logs

COPY --chown=skipline:skipline scripts/start.sh /app/start.sh
RUN chmod +x /app/start.sh

USER skipline

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["/app/start.sh"]
