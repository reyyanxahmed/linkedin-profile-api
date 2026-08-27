# Dockerfile for the LinkedIn Profile API.
# Single-stage python:3.12-slim. Installs build deps, builds curl_cffi, then removes
# the build deps in the same layer so the final image has no compiler.
# Runtime is non-root.

FROM python:3.12-slim

# Install build deps and curl's runtime lib together. We remove the build-only deps
# in the same RUN so they don't end up in a layer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl libcurl4-openssl-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user early so the venv and code are owned by it.
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app

# Install dependencies into the system site-packages (simpler than a venv copy).
COPY pyproject.toml README.md ./
COPY app ./app

# Install the package. pip needs the build deps above for curl_cffi.
RUN pip install --no-cache-dir -e ".[dev]" \
    # Now remove the build-only deps so the final image has no compiler / headers.
    && apt-get purge -y --auto-remove build-essential curl libcurl4-openssl-dev libssl-dev \
    && apt-get autoremove -y \
    # Re-install the runtime libcurl (purge above may have removed it).
    && apt-get update && apt-get install -y --no-install-recommends libcurl4 \
    && rm -rf /var/lib/apt/lists/*

COPY scripts ./scripts
RUN chown -R app:app /app

USER app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Healthcheck hits /v1/health (unauthenticated).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/v1/health', timeout=3).status==200 else 1)"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]