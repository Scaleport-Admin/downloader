# Multi-stage build for smaller image size
# Stage 1: Build dependencies
FROM python:3.11-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy and install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Stage 2: Production image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Europe/Prague

# Install runtime dependencies including gosu for privilege dropping
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    gosu \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

# Create non-root user for security (with fixed UID/GID for volume permissions)
RUN groupadd -r -g 1000 appgroup && useradd -r -g appgroup -u 1000 appuser

# Create directories with proper ownership
RUN mkdir -p /app /data /downloads \
    && chown -R appuser:appgroup /app /data /downloads

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=appuser:appgroup app/ ./app/

# Copy entrypoint script and ensure it's executable
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default environment variables
ENV PORT=8000 \
    LOG_LEVEL=INFO \
    DATABASE_PATH=/data/downloads.db \
    MAX_CONCURRENT_DOWNLOADS=1 \
    AUTO_DECOMPRESS=true \
    HISTORY_RETENTION_DAYS=30 \
    PUID=1000 \
    PGID=1000

# Use entrypoint for permission handling
ENTRYPOINT ["/entrypoint.sh"]

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
