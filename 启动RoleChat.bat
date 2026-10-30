@echo off
chcp 65001 >nul
title RoleChat 一键启动
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_rolechat.ps1"
