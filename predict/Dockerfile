# Schlankes Image nur für Prognosen (pdb-predict). Baut aus dem Wurzelordner:
#   docker build -f predict/Dockerfile -t predictiondb-predict .
# Installiert nur pdb-predict und seine Abhängigkeiten aus dem uv-Workspace, ohne
# Datenbank-Migrationen, Grafana oder Trainingswerkzeuge.
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY packages packages
COPY services services
COPY predict predict
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package pdb-predict

FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS runtime
# LightGBM braucht OpenMP
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /data /models && chown app:app /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# Endpunkte und Gitterausschnitt für GFS (nur Wetter-Varianten) und GFS Europa (nachbarwetter)
COPY config/sources/gfs_0p25.yaml config/sources/gfs_0p25.yaml
COPY config/sources/gfs_0p5.yaml config/sources/gfs_0p5.yaml
COPY LICENSE predict/README.md ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    PDB_PREDICT_DATA_DIR=/data PDB_PREDICT_MODEL=/models
USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=5m --timeout=10s --start-period=30m \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5)"
CMD ["pdb-predict", "serve"]
