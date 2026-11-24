# syntax=docker/dockerfile:1.7

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY requirements.lock requirements-build.lock ./
RUN pip install --require-hashes --prefix=/install -r requirements.lock \
    && pip install --require-hashes -r requirements-build.lock
COPY pyproject.toml README.md ./
COPY pipip ./pipip
RUN pip install --no-build-isolation --no-deps --prefix=/install .

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIPIP_CONFIG=/config/config.yaml \
    PIPIP_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/*

ARG PIPIP_UID=1000
ARG PIPIP_GID=1000
RUN groupadd --system --gid "${PIPIP_GID}" pipip \
    && useradd --system --uid "${PIPIP_UID}" --gid pipip \
       --home-dir /home/pipip --create-home pipip \
    && mkdir -p /data /config /app \
    && chown -R pipip:pipip /data /config /app

COPY --from=builder /install /usr/local

WORKDIR /app
USER pipip

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; port=os.environ.get('PIPIP_HTTP_PORT','8080'); raise SystemExit(urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4).status != 204)" 2>/dev/null

ENTRYPOINT ["python", "-m", "pipip"]
