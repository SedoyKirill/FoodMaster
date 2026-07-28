<#
.SYNOPSIS
    Restore the database from a pg_dump archive.

.DESCRIPTION
    pg_restore runs INSIDE the db container, so its version always matches the
    server exactly and the dumps in the `backups` volume are already reachable
    (mounted read-only at /backups).

    THIS DESTROYS THE CURRENT DATABASE. Everything the family has recorded —
    plans, diary, expenses, price history — is replaced by the dump's contents.

.PARAMETER List
    Show the dumps available inside the container and exit.

.PARAMETER Dump
    Either a file name inside /backups, or a path to a dump on this PC.

.PARAMETER Yes
    Skip the confirmation prompt. Intended for scripted use only.

.EXAMPLE
    .\scripts\restore.ps1 -List
    .\scripts\restore.ps1 -Dump ration-20260728-043000.dump
    .\scripts\restore.ps1 -Dump 'D:\from-nas\ration-20260701-043000.dump'
#>
[CmdletBinding(DefaultParameterSetName = 'Restore')]
param(
    [Parameter(ParameterSetName = 'List')]
    [switch]$List,

    [Parameter(ParameterSetName = 'Restore', Mandatory = $true, Position = 0)]
    [string]$Dump,

    [Parameter(ParameterSetName = 'Restore')]
    [switch]$Yes
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

      Two PowerShell 5.1 traps this closes:

      * PS wraps anything a native program writes to stderr in an ErrorRecord,
        and `docker compose` reports perfectly normal progress there. Under
        $ErrorActionPreference = 'Stop' that aborts the script even though
        docker exited 0.
      * The command must be passed as a script block. A parameter with
        ValueFromRemainingArguments silently swallows tokens that look like
        parameter names, so `-d ration` arrives as a bare `ration`.
    #>
    param([Parameter(Mandatory = $true, Position = 0)][scriptblock]$Command)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) {
        throw "Команда `"$Command`" завершилась с кодом $LASTEXITCODE"
    }
}

$dbUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { 'ration' }
$dbName = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { 'ration' }

try { Invoke-Native { docker info 2>$null } | Out-Null } catch {
    Write-Host 'Docker Desktop не запущен. Запустите его и повторите.' -ForegroundColor Red
    exit 1
}

$ErrorActionPreference = 'Continue'
$dbState = docker inspect --format '{{.State.Status}}' ration-db-1 2>$null
$ErrorActionPreference = 'Stop'
if ($dbState -ne 'running') {
    Write-Host 'Контейнер базы не запущен. Выполните: docker compose up -d db' -ForegroundColor Red
    exit 1
}

if ($List) {
    Write-Step 'Дампы в томе backups'
    # -T is required: PowerShell's stdin is not a TTY.
    Invoke-Native { docker compose exec -T db ls -lht /backups }
    exit 0
}

# --- Locate the dump --------------------------------------------------------
if (Test-Path -LiteralPath $Dump) {
    Write-Step "Копирую $Dump в контейнер"
    Invoke-Native { docker compose cp $Dump db:/tmp/restore.dump }
    $containerPath = '/tmp/restore.dump'
} else {
    $containerPath = "/backups/$Dump"
    try {
        Invoke-Native { docker compose exec -T db test -f $containerPath }
    } catch {
        Write-Host "Файл не найден ни на диске, ни в /backups: $Dump" -ForegroundColor Red
        Write-Host 'Список доступных дампов: .\scripts\restore.ps1 -List' -ForegroundColor Yellow
        exit 1
    }
}

Write-Step 'Проверяю читаемость архива'
try {
    Invoke-Native { docker compose exec -T db pg_restore --list $containerPath } | Out-Null
} catch {
    Write-Host 'Архив повреждён или нечитаем.' -ForegroundColor Red
    exit 1
}

# --- Confirm ----------------------------------------------------------------
if (-not $Yes) {
    Write-Host ''
    Write-Host 'ВНИМАНИЕ: база данных будет удалена и заменена содержимым дампа.' -ForegroundColor Yellow
    Write-Host "Будут потеряны все планы, дневник, траты и история цен в базе '$dbName'." -ForegroundColor Yellow
    $answer = Read-Host "Для подтверждения введите имя базы ($dbName)"
    if ($answer -ne $dbName) { Write-Host 'Отменено.' -ForegroundColor Red; exit 1 }
}

# --- Restore ----------------------------------------------------------------
Write-Step 'Останавливаю api и scheduler'
Invoke-Native { docker compose stop api scheduler 2>$null }

Write-Step "Пересоздаю базу $dbName"
# WITH (FORCE) terminates leftover sessions; without it the DROP hangs behind
# any stray connection.
Invoke-Native {
    docker compose exec -T db psql -U $dbUser -d postgres -v ON_ERROR_STOP=1 `
        -c "DROP DATABASE IF EXISTS $dbName WITH (FORCE);" `
        -c "CREATE DATABASE $dbName OWNER $dbUser;"
}

Write-Step 'Восстанавливаю дамп'
Invoke-Native {
    docker compose exec -T db pg_restore -U $dbUser -d $dbName `
        --no-owner --no-privileges --exit-on-error -j 4 $containerPath
}

Write-Step 'Проверяю результат'
Invoke-Native {
    docker compose exec -T db psql -U $dbUser -d $dbName -c '\dx' `
        -c 'SELECT version_num AS alembic_revision FROM alembic_version;' `
        -c 'SELECT (SELECT count(*) FROM recipes) AS recipes, (SELECT count(*) FROM ingredients) AS ingredients, (SELECT count(*) FROM families) AS families;'
}

Write-Step 'Запускаю api и scheduler'
Invoke-Native { docker compose start api scheduler 2>$null }

Write-Step 'Жду готовности API'
$deadline = (Get-Date).AddMinutes(3)
while ($true) {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 5
        if ($response.StatusCode -eq 200) { break }
    } catch { }
    if ((Get-Date) -gt $deadline) { Write-Host 'API не ответил за 3 минуты — проверьте docker compose logs api.' -ForegroundColor Yellow; break }
    Start-Sleep -Seconds 3
}

Write-Host 'Восстановление завершено.' -ForegroundColor Green
