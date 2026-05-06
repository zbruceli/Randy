FROM python:3.11-slim

WORKDIR /app

# Install package first so dependency layer caches across code changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .

# Tests are not needed at runtime; copy only what's needed to run.
COPY scripts ./scripts

# SQLite DB lives at /app/randy.sqlite by default; mount a volume here to persist.
VOLUME ["/app/data"]
ENV DB_PATH=/app/data/randy.sqlite

CMD ["python", "-m", "randy"]
