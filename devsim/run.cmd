@echo off
rem devsim: laptop test hub. Start ComfyUI Desktop and Ollama first.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo [devsim] creating virtualenv ...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
set CUSTOM_NODES_DIR=%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI\custom_nodes
set PYTHONIOENCODING=utf-8
.venv\Scripts\python sim.py
