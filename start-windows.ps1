# Запуск ассистента на Windows одной командой:
#   powershell -ExecutionPolicy Bypass -File .\start-windows.ps1
# Обновляет код из ветки main, ставит зависимости, проверяет настройки
# и запускает бота. Остановить — Ctrl+C или закрыть окно.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Host "Нет файла .env в $PSScriptRoot — скопируйте .env.example в .env и заполните." -ForegroundColor Red
    exit 1
}

Write-Host "== Обновляю код (ветка main)" -ForegroundColor Cyan
git fetch origin
git checkout main
git pull origin main

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "== Создаю окружение Python" -ForegroundColor Cyan
    python -m venv .venv
}

Write-Host "== Ставлю зависимости" -ForegroundColor Cyan
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt

Write-Host "== Самопроверка" -ForegroundColor Cyan
.venv\Scripts\python.exe -m app.selfcheck
if ($LASTEXITCODE -eq 2) {
    Write-Host "Запуск невозможен — почините пункты выше и запустите скрипт снова." -ForegroundColor Red
    exit 2
}

Write-Host "== Запускаю. Пишите боту в Telegram: /check, /auth. Остановить — Ctrl+C" -ForegroundColor Green
.venv\Scripts\python.exe run.py
