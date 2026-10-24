# Ein Image für alle Python-Dienste; der Dienst ergibt sich aus dem Befehl in compose.yaml.
FROM python:3.14-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages

FROM python:3.14-slim-trixie AS runtime
# LightGBM (ml-train) braucht OpenMP
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --home-dir /app app
# Einhängepunkt für Volumes (ml-data): ein neues, leeres Volume übernimmt Besitzer und Rechte
RUN mkdir -p /data && chown app:app /data
WORKDIR /app
COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
CMD ["python", "-c", "import predictiondb_core; print(predictiondb_core.__version__)"]
