# Setup and running

> Русская версия: [setup.ru.md](setup.ru.md)

## Prerequisites

Only **Docker Desktop** with the WSL2 backend. Python and Node are not needed on
the host — they are only for development and tests (see
[development.md](development.md)).

Verified on: Windows 10 Pro 22H2 (build 19045), Docker 29.1.3, Docker Compose
2.40.3, NVIDIA RTX 3070 8 GB, driver 591.74.

> The README says Windows 11. Windows 10 22H2 is officially supported by Docker
> Desktop, but Microsoft's consumer support ended on 2025-10-14, and Docker only
> supports Windows within Microsoft's servicing window. Practical consequence:
> pin your Docker Desktop version and disable auto-update, or plan a move to
> Windows 11.

## First run

1. Install Docker Desktop, enable the WSL2 backend and GPU support
   (Settings → Resources → WSL integration; keep the NVIDIA driver current).
2. Copy the configuration file:
   ```powershell
   copy .env.example .env
   ```
   The defaults are fine for a local run. Save it as UTF-8 — it contains Russian
   comments.
3. Start everything:
   ```powershell
   docker compose up -d
   ```
4. Open <http://localhost:8000>.

The first `up` creates the database, applies migrations and begins downloading
the `qwen3:8b` model (~5.2 GB). **The download blocks nothing**: it runs in a
separate one-shot `ollama-init` container, and `up -d` returns within seconds.
Watch it with:

```powershell
docker compose logs -f ollama-init
```

Seeing `ollama-init  Exited (0)` in `docker compose ps` is **normal** — it is a
one-shot task that finished successfully. Model readiness is reported by
`GET /health` in the `ollama_model` field.

## Daily use

```powershell
docker compose up -d      # start
docker compose down       # stop
```

Data lives in named Docker volumes and survives a stop. `docker compose down -v`
**deletes the volumes**, including the database and the downloaded model.

## Verifying the installation

```powershell
docker compose ps                              # every service healthy
curl http://127.0.0.1:8000/health               # {"status":"ok","db":"ok",...}
docker compose exec ollama nvidia-smi           # the RTX 3070 is visible
docker compose exec db psql -U ration -d ration -c "\dx"   # vector, pg_trgm
```

## Access from a phone on the home network

By default the port is published on `127.0.0.1` only, so the app is reachable
from this PC alone. To open it to the home network, set in `.env`:

```
WEB_BIND_IP=0.0.0.0
```

then `docker compose up -d`.

> **Deviation from the spec.** The spec calls this knob `WEB_EXPOSE=0|1`. Docker
> Compose has no conditional expressions in `ports`, and the obvious workarounds
> fail **open** (an empty `host_ip` means `0.0.0.0`) — unacceptable for an app
> with no authentication. So the bind address itself is the knob.

That alone is not enough: Windows Defender Firewall must also allow inbound TCP
8000 for `com.docker.backend.exe`, and the home Wi-Fi must be classified as a
**Private** network.

**Never publish the app to the internet** — it has no authentication.

## Auto-start after a reboot

Every service uses `restart: unless-stopped`, so they come back when Docker
restarts. But Docker Desktop is a desktop application: it starts **at interactive
sign-in, not at boot**. Enable Settings → General → "Start Docker Desktop when
you sign in". Fully unattended start after a reboot without a login is not
possible.

## Disk space

Docker Desktop keeps images and volumes in `ext4.vhdx` on drive C:. The project
needs 25-30 GB (app image ~0.4 GB, Ollama image ~3 GB, model 5.2 GB, Postgres,
up to 14 GB of dumps). If C: is tight, relocate the store:
Settings → Resources → Advanced → Disk image location.

## WSL2 memory

WSL2 defaults to roughly half the host RAM for containers. That is enough for
M1, but M5's model benchmark (`qwen3:14b`, `gpt-oss:20b`) runs in hybrid GPU+CPU
mode and will hit the limit. Create `%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
memory=16GB
processors=8
swap=8GB
```

Changes require `wsl --shutdown`, which stops all containers — do it before
`docker compose up`.

## Host security

Check that Docker Desktop's "Expose daemon on tcp://localhost:2375 without TLS"
is **off**. It exposes an unauthenticated Docker API, which is equivalent to
root on the machine. This project does not need it.
