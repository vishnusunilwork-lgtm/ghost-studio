@echo off
title Ghost Studio
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ghost.ps1" %*
