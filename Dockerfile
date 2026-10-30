# Build locally: the operator builds the LGPL native host; no image is published.
FROM rust:1.99.0-bookworm AS native
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates clang libclang-dev git curl pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY tools/build-host.sh /build-host.sh
RUN /build-host.sh /build > /library-path \
    && install -Dm755 "$(cat /library-path)" /native/libmornye_desktop.so \
    && mkdir -p /native/source-notices \
    && cp /build/source/rust_backend/NOTICE /native/source-notices/ \
    && cp /build/source/rust_backend/Cargo.lock /native/source-notices/ \
    && for name in LICENSE COPYING NOTICE; do \
         if [ ! -f "/build/source/$name" ]; then continue; fi; \
         cp "/build/source/$name" /native/source-notices/; \
       done \
    && echo c90ec6478ad3e808ef22ba9464f8ad05054dde7b > /native/source-notices/source-commit

FROM python:3.12-slim-bookworm AS python-build
COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --no-cache

FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg tini libgcc-s1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid bridge --no-create-home --home-dir /state bridge \
    && install -d -m 0700 -o bridge -g bridge /state
COPY --from=native /native /opt/mornye
COPY --from=python-build /app/.venv /app/.venv
COPY packaging/docker.toml /etc/mornye-bridge/config.toml
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/state
USER 10001:10001
WORKDIR /state
VOLUME ["/state"]
EXPOSE 4759 4760
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "from pathlib import Path; from mornye_bridge.config import Config; import urllib.request; c=Config.load(Path('/etc/mornye-bridge/config.toml')); urllib.request.urlopen('http://127.0.0.1:'+str(c.management_port if c.management_host else c.port)+'/login', timeout=3).close()"]
ENTRYPOINT ["/usr/bin/tini", "--", "mornye-bridge", "--config", "/etc/mornye-bridge/config.toml"]
CMD ["serve"]
