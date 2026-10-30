# RoleChat 打包脚本：在工作区内创建 venv 并用 PyInstaller 生成单文件 exe。
# 所有的 pip 缓存 / 临时目录 / 构建产物都重定向到项目目录内，不写工作区之外。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\make_exe.ps1 [-PythonExe python]
param(
    [string]$PythonExe = "python"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BuildCache = Join-Path $Root ".build-cache"
$Venv = Join-Path $Root ".venv"

New-Item -ItemType Directory -Force -Path (Join-Path $BuildCache "tmp") | Out-Null

# 限制写入范围：pip 缓存与临时目录都放进工作区
$env:PIP_CACHE_DIR = Join-Path $BuildCache "pip"
$env:TEMP = Join-Path $BuildCache "tmp"
$env:TMP = $env:TEMP

if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Write-Host "[1/3] 创建虚拟环境 (.venv，位于工作区内)..."
    & $PythonExe -m venv $Venv
}
$Py = Join-Path $Venv "Scripts\python.exe"

Write-Host "[2/3] 安装 PyInstaller（仅装进工作区 venv）..."
& $Py -m pip install --quiet pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller 失败" }

Write-Host "[3/3] 打包 RoleChat.exe ..."
Set-Location $Root
$StaticSrc = Join-Path $Root "app\static"
& $Py -m PyInstaller --noconfirm --onefile --name RoleChat `
    --distpath (Join-Path $Root "dist") `
    --workpath (Join-Path $Root "build") `
    --specpath $Root `
    --add-data "$StaticSrc;static" `
    (Join-Path $Root "run.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

Write-Host ""
Write-Host "完成: dist\RoleChat.exe"
Write-Host "提示: 请把 exe 保留在项目目录内运行；角色/会话/记忆保存在 exe 旁边的 data\ 目录。"
