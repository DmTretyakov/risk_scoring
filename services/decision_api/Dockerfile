# Build context: the solution root (see docker-compose.yml).
FROM python:3.12-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PATH=/opt/venv/bin:$PATH
WORKDIR /srv
COPY services/decision_api/pyproject.toml services/decision_api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY services/decision_api/app ./app
RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Same image plus test tooling and fixtures; never deployed.
FROM runtime AS test
USER root
RUN uv sync --frozen --no-install-project
COPY services/decision_api/tests ./tests
COPY infra/db ./db
COPY data ./data
COPY reference ./reference
COPY scripts ./scripts
USER app
CMD ["pytest", "-q"]
