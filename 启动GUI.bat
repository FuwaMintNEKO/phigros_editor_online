@echo off
rem Double-click this file to start Phigros cloud save editor (no console window)
rem Forces C:\Python314 (full-permission install, can write to this folder)
cd /d "%~dp0"
start "" "C:\Python314\pythonw.exe" phigros_cloud_gui.py
