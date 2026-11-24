# Build context: the solution root. One image; the model is chosen by MODEL_VERSION.
FROM python:3.12-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PATH=/opt/venv/bin:$PATH MODELS_DIR=/srv/models
WORKDIR /srv
COPY services/scorer/pyproject.toml services/scorer/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY services/scorer/app ./app
# Stand-in for a model registry (MLflow): versioned artifacts baked into the image.
COPY models ./models
RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
USER root
RUN uv sync --frozen --no-install-project
COPY services/scorer/tests ./tests
USER app
CMD ["pytest", "-q"]
