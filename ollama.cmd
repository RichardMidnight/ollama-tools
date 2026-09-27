@echo off
rem Wrapper so "ollama.cmd" works inline in PowerShell/cmd without the
rem flash-and-close console that the .py file association causes.
rem OLLAMA_INLINE tells the script we are inline (no pause needed).
set "OLLAMA_INLINE=1"
where py >nul 2>nul
if %errorlevel%==0 (
  py "%~dp0ollama.py" %*
) else (
  python "%~dp0ollama.py" %*
)
