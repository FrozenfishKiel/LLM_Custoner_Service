@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_customer_service_production.ps1" %*
exit /b %errorlevel%
