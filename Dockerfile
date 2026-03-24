FROM python:3.13-slim

WORKDIR /app

# git: Pantry package installs clone from GitHub
# libsqlcipher-dev: sqlcipher3 encrypted SQLite (fallback if no wheel)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git build-essential libsqlcipher-dev \
    && rm -rf /var/lib/apt/lists/*

# Install base dependencies (no audio libs)
COPY requirements-base.txt .
RUN pip install --no-cache-dir -r requirements-base.txt

# Install SDK from monorepo sibling
# In docker-compose: provided via additional_contexts
# Standalone: copy ../jarvis-command-sdk into build context first
COPY --from=jarvis-command-sdk . /tmp/jarvis-command-sdk/
RUN pip install --no-cache-dir /tmp/jarvis-command-sdk && rm -rf /tmp/jarvis-command-sdk

# Copy all application code
# Using .dockerignore to exclude .venv, __pycache__, tests, etc.
COPY . .

# Ensure all top-level packages are importable (bare imports like "from services.xxx")
ENV PYTHONPATH=/app

# Skip provisioning check in Docker
ENV JARVIS_SKIP_PROVISIONING_CHECK=true
ENV CONFIG_PATH=/config/config.json
ENV JARVIS_NODE_PORT=7771

CMD ["python", "scripts/text_mode.py"]
