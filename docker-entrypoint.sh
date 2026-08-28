#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/data/lenta
    chown ration:ration /app/data /app/data/lenta
    if [ "${1#-}" != "$1" ]; then
        set -- python -m app.store.lenta.cli "$@"
    fi
    exec gosu ration "$@"
fi

if [ "${1#-}" != "$1" ]; then
    set -- python -m app.store.lenta.cli "$@"
fi
exec "$@"
