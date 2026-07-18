@echo off
setlocal
echo Stopping service on port 8012...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8012" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>nul
)
echo Stopping MySQL and Neo4j containers...
docker stop llm-cs-mysql llm-cs-neo4j >nul 2>nul
echo Done.
pause
