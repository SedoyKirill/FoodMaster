FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./

RUN pip install --no-cache-dir "setuptools>=77" asyncpg==0.31.0 \
    && apt-get update \
    && apt-get install --yes --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY README.md ./
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/ration-entrypoint

RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
    && chmod +x /usr/local/bin/ration-entrypoint

RUN useradd --create-home --uid 10001 ration \
    && mkdir -p /app/data \
    && chown -R ration:ration /app

ENTRYPOINT ["ration-entrypoint"]
CMD ["python", "-m", "app.store.lenta.cli"]
