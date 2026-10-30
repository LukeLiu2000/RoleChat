# RoleChat 一键启动：先启动工作区内便携版 Ollama（如未运行），再启动应用 exe。
# 模型文件全部保存在工作区 tools\ollama\models，不写系统目录。
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

# 1) 定位工作区内便携版 ollama.exe
$ollama = Get-ChildItem (Join-Path $root "tools\ollama") -Recurse -Filter "ollama.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $ollama) {
    Write-Host "[错误] 未找到 tools\ollama 下的 ollama.exe，请先完成便携版安装。" -ForegroundColor Red
    exit 1
}

# 2) 模型服务没起来就启动（隐藏窗口，后台常驻）
$alive = $false
try { Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null; $alive = $true } catch {}
if (-not $alive) {
    Write-Host "启动本地模型服务（Ollama 便携版）..."
    $env:OLLAMA_MODELS = Join-Path $root "tools\ollama\models"
    Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden
    $ok = $false
    foreach ($i in 1..40) {
        Start-Sleep -Milliseconds 500
        try { Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null; $ok = $true; break } catch {}
    }
    if (-not $ok) { Write-Host "[警告] 模型服务 20 秒内未就绪，应用仍会启动，但需等模型服务可用后再发消息。" -ForegroundColor Yellow }
    else { Write-Host "模型服务已就绪 (http://127.0.0.1:11434)" -ForegroundColor Green }
} else {
    Write-Host "模型服务已在运行" -ForegroundColor Green
}

# 2.5) 本地语音服务（SenseVoice 识别 / Kokoro 合成，全离线）
$voiceAlive = $false
try { Invoke-RestMethod "http://127.0.0.1:8790/capabilities" -TimeoutSec 2 | Out-Null; $voiceAlive = $true } catch {}
if (-not $voiceAlive) {
    $vpy = Join-Path $root ".venv\Scripts\python.exe"
    $vserver = Join-Path $root "tools\voice_server.py"
    if ((Test-Path $vpy) -and (Test-Path $vserver)) {
        Write-Host "启动本地语音服务（首次加载模型约需 20~40 秒）..."
        Start-Process -FilePath $vpy -ArgumentList ('"' + $vserver + '"') -WorkingDirectory $root -WindowStyle Hidden
        foreach ($i in 1..90) {
            Start-Sleep -Milliseconds 1000
            try { Invoke-RestMethod "http://127.0.0.1:8790/capabilities" -TimeoutSec 2 | Out-Null; $voiceAlive = $true; break } catch {}
        }
        if ($voiceAlive) { Write-Host "语音服务已就绪 (http://127.0.0.1:8790)" -ForegroundColor Green }
        else { Write-Host "[警告] 语音服务 90 秒内未就绪；应用仍会启动，语音功能暂不可用。" -ForegroundColor Yellow }
    } else {
        Write-Host "[提示] 未找到语音组件（.venv / tools\voice_server.py），语音功能不可用。" -ForegroundColor Yellow
    }
} else {
    Write-Host "语音服务已在运行" -ForegroundColor Green
}

# 3) 启动 RoleChat（exe 会自动打开浏览器；数据保存在 exe 旁 data\ 目录）
$exe = Join-Path $root "dist\RoleChat.exe"
if (-not (Test-Path $exe)) { Write-Host "[错误] 未找到 $exe" -ForegroundColor Red; exit 1 }
Write-Host "启动 RoleChat ..."
Start-Process -FilePath $exe
