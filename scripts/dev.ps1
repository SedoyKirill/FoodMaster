<#
.SYNOPSIS
    Development mode: infrastructure in Docker, the app natively with hot reload.

.DESCRIPTION
    Runs Postgres (and optionally Ollama) in containers while uvicorn runs on
    Windows with --reload.

    Why not run the app in a container with a bind mount: uvicorn's reloader
    relies on native filesystem events, and those are not delivered reliably
    across the Windows -> WSL2 boundary, so containerised hot reload degrades to
    CPU-hungry polling or silently misses edits. Running natively also means the
    production compose file needs no source bind mount at all, which removes the
    whole Cyrillic-path risk class.

    .env is not modified: it holds the Docker-side truth (@db:5432) and this
    script injects the host-side values as process environment, which
    pydantic-settings ranks above the .env file.

.EXAMPLE
    .\scripts\dev.ps1
    .\scripts\dev.ps1 -NoOllama -Port 8080
    .\scripts\dev.ps1 -ResetDb
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$NoOllama,
    [switch]$ResetDb
)

$ErrorActionPreference = 'Stop'
# PowerShell 5.1 garbles Cyrillic output without this.
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }

function Invoke-Native {
    <#
      Run a native command and fail only on a non-zero exit code.

      PowerShell 5.1 wraps anything a native program writes to stderr in an
      ErrorRecord, and `docker compose` reports normal progress there; under
      $ErrorActionPreference = 'Stop' that would abort the script even though
      docker exited 0. The command is taken as a script block because a
      ValueFromRemainingArguments parameter silently swallows tokens that look
      like parameter names (`-d ration` would arrive as a bare `ration`).
    #>
    param([Parameter(Mandatory = $true, Position = 0)][scriptblock]$Command)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) {
        throw "Команда `"$Command`" завершилась с кодом $LASTEXITCODE"
    }
}

# --- Docker -----------------------------------------------------------------
Write-Step 'Проверяю Docker'
try { Invoke-Native { docker info 2>$null } | Out-Null } catch {
    Write-Host 'Docker Desktop не запущен. Запустите его и повторите.' -ForegroundColor Red
    exit 1
}

if (-not (Test-Path '.env')) {
    Write-Step 'Создаю .env из .env.example'
    Copy-Item '.env.example' '.env'
}

# The container would otherwise already own port 8000 and uvicorn would die
# with WinError 10048.
Write-Step 'Останавливаю контейнеры api и scheduler (порт 8000 нужен локально)'
Invoke-Native { docker compose stop api scheduler 2>$null }

$services = @('db')
if (-not $NoOllama) { $services += @('ollama', 'ollama-init') }
Write-Step "Поднимаю инфраструктуру: $($services -join ', ')"
Invoke-Native { docker compose up -d @services 2>$null }

Write-Step 'Жду готовности базы'
$deadline = (Get-Date).AddMinutes(3)
while ((docker inspect --format '{{.State.Health.Status}}' ration-db-1 2>$null) -ne 'healthy') {
    if ((Get-Date) -gt $deadline) { Write-Host 'База не поднялась за 3 минуты.' -ForegroundColor Red; exit 1 }
    Start-Sleep -Seconds 2
}

# --- Python environment -----------------------------------------------------
Write-Step 'Синхронизирую окружение Python'
$uv = (Get-Command uv -ErrorAction SilentlyContinue)
if ($uv) { uv sync } else { python -m uv sync }
function Invoke-Uv { if ($uv) { uv @args } else { python -m uv @args } }

# --- Host-side configuration ------------------------------------------------
$dbPort = if ($env:DB_PORT) { $env:DB_PORT } else { '5432' }
$env:DATABASE_URL = "postgresql+asyncpg://ration:ration@127.0.0.1:$dbPort/ration"
$env:OLLAMA_URL   = 'http://127.0.0.1:11434'
$env:APP_ENV      = 'dev'
$env:APP_ROLE     = 'api'
$env:LOG_FORMAT   = 'console'
$env:HF_HOME      = Join-Path $repoRoot '.cache\hf'

if ($ResetDb) {
    # Never `docker compose down -v`: that would also destroy the 5+ GB Ollama
    # model volume.
    Write-Step 'Пересоздаю базу ration'
    Invoke-Native {
        docker compose exec -T db psql -U ration -d postgres -v ON_ERROR_STOP=1 `
            -c 'DROP DATABASE IF EXISTS ration WITH (FORCE);' `
            -c 'CREATE DATABASE ration OWNER ration;'
    }
}

Write-Step 'Применяю миграции'
Invoke-Uv run alembic upgrade head

Write-Step "Запускаю uvicorn с автоперезагрузкой на http://127.0.0.1:$Port"
Invoke-Uv run uvicorn app.main:app --reload --reload-dir app --host 127.0.0.1 --port $Port
