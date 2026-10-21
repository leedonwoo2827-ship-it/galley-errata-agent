@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (echo 먼저 setup.bat 을 실행하세요. & pause & exit /b 1)
set PYTHONUTF8=1
echo TS 정오표 에이전트 콘솔을 엽니다. 이 창은 닫지 마세요.
.venv\Scripts\python -m tse.server
pause