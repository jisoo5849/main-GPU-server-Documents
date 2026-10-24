@echo off
rem 노트북용 시험 허브 실행. ComfyUI Desktop 과 Ollama 를 먼저 켜 둔다.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo [devsim] 가상환경 만드는 중...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
set CUSTOM_NODES_DIR=%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI\custom_nodes
.venv\Scripts\python sim.py
