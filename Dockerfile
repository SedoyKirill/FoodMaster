# syntax=docker/dockerfile:1.9
ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22
ARG UV_VERSION=0.11.32
ARG PG_MAJOR=16

# --------------------------------------------------------------------------
# stage: web-build (M6)
# Nothing in `runtime` references this stage yet, so BuildKit skips it
# entirely — it is parsed but never executed while web/ does not exist.
# In M6: uncomment the COPY --from=web-build line near the bottom.
# --------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web/ ./
RUN npm run build

# --------------------------------------------------------------------------
# stage: python-base
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS python-base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# --------------------------------------------------------------------------
# stage: builder — dependencies only, so editing app/ never invalidates it
# --------------------------------------------------------------------------
FROM python-base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
ARG APP_EXTRAS=""
WORKDIR /src
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --frozen --no-dev ${APP_EXTRAS}

# --------------------------------------------------------------------------
# stage: runtime
# --------------------------------------------------------------------------
FROM python-base AS runtime
ARG PG_MAJOR

# pg_dump for the nightly backup job. Debian bookworm's main archive only
# ships postgresql-client-15, which refuses to dump a 16 server; the trixie
# one is 17, whose custom-format archives pg_restore 16 cannot read. So the
# major is pinned to match the db service exactly.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-${PG_MAJOR}; \
    apt-get purge -y gnupg; apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

# Directories are created AND chowned here on purpose: when Docker mounts an
# empty named volume over a path that exists in the image, it copies that
# path's ownership into the volume. Without this the volumes come up root-owned
# and a non-root process cannot write dumps or the HF cache.
RUN groupadd -g 10001 app \
    && useradd -u 10001 -g app -m -d /home/app -s /usr/sbin/nologin app \
    && mkdir -p /app /backups /cache/hf /backup-nas \
    && chown -R app:app /app /backups /cache/hf /backup-nas

COPY --from=builder --chown=app:app /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic/ ./alembic/
COPY --chown=app:app scripts/docker/ ./scripts/docker/
COPY --chown=app:app app/ ./app/

# CRLF insurance. .gitattributes already pins these to LF, but a clone on a
# machine without it would otherwise produce an image whose entrypoint fails
# with the near-ungoogleable "no such file or directory".
RUN sed -i 's/\r$//' /app/scripts/docker/*.sh && chmod +x /app/scripts/docker/*.sh

# --- M6: uncomment to ship the built SPA -----------------------------------
# COPY --from=web-build --chown=app:app /web/dist /app/web/dist
# ---------------------------------------------------------------------------

USER app
ENV PYTHONPATH=/app \
    HF_HOME=/cache/hf
EXPOSE 8000
ENTRYPOINT ["/bin/sh", "/app/scripts/docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
