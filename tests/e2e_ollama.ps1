# 端到端验证：真实 Ollama 模型 + exe + 记忆系统全链路（使用隔离测试数据，不碰真实数据）
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$td = Join-Path $PSScriptRoot "tmp-e2e"
if (Test-Path $td) { Remove-Item $td -Recurse -Force -Confirm:$false }
New-Item -ItemType Directory -Force (Join-Path $td "data") | Out-Null

$cfgJson = @'
{
  "server": {"host": "127.0.0.1", "port": 8799, "open_browser": false},
  "model": {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "qwen2.5:7b",
            "context_chars": 6000, "reply_max_chars": 1200, "max_tokens": 4096, "temperature": 0.7, "timeout_sec": 180},
  "memory": {"max_memories_in_prompt": 5, "min_score": 0.15, "auto_extract_every": 4, "dedup_threshold": 0.55},
  "voice": {"tts_voice": "female", "auto_tts": false, "speed": 1.0, "server": "http://127.0.0.1:8790"}
}
'@
$out = Join-Path $td "config.json"
[IO.File]::WriteAllText($out, $cfgJson)

$env:ROLECHAT_DATA_DIR = Join-Path $td "data"
$env:ROLECHAT_CONFIG = $out
$exe = Join-Path $root "dist\RoleChat.exe"
$p = Start-Process -FilePath $exe -ArgumentList "--no-browser", "--port", "8799" -PassThru -WindowStyle Hidden

$base = "http://127.0.0.1:8799"
$ok = $false
foreach ($i in 1..40) {
    Start-Sleep -Milliseconds 800
    try { $h = Invoke-RestMethod "$base/api/health" -TimeoutSec 2; $ok = $true; break } catch {}
}
if (-not $ok) { throw "测试实例启动失败" }
Write-Host ("实例: v{0}, provider={1}, model={2}" -f $h.version, $h.provider, $h.model)

function PostJson($url, $obj) {
    $json = $obj | ConvertTo-Json -Depth 5
    Invoke-RestMethod $url -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($json)) -ContentType "application/json; charset=utf-8" -TimeoutSec 200
}

$pass = 0; $fail = 0
function Check($name, $cond, $detail) {
    if ($cond) { $script:pass++; Write-Host "  [OK] $name" -ForegroundColor Green }
    else { $script:fail++; Write-Host "  [FAIL] $name  $detail" -ForegroundColor Red }
}

try {
    # 1) 普通对话：应是真实模型回复（不含「模拟模型」标记）
    $chars = (Invoke-RestMethod "$base/api/characters").characters
    $cid = $chars[0].id
    Write-Host ("使用角色: " + $chars[0].name)
    $sess = PostJson "$base/api/sessions" @{ character_id = $cid }
    $sid = $sess.session.id

    $t0 = Get-Date
    $r1 = PostJson "$base/api/chat" @{ session_id = $sid; message = "你好呀！用一句话介绍一下你自己。" }
    $dt1 = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $reply1 = $r1.reply
    Write-Host ("第1条回复（${dt1}s）: " + $reply1.Substring(0, [Math]::Min(100, $reply1.Length)))
    Check "真实模型回复（非模拟标记）" ($reply1 -notmatch "模拟模型") ""
    Check "回复非空且通顺" ($reply1.Length -gt 10) ""

    # 2) 显式记忆指令 → 写入长期记忆
    $r2 = PostJson "$base/api/chat" @{ session_id = $sid; message = "记住：我的名字是王小明，我最讨厌香菜。" }
    Start-Sleep -Milliseconds 500
    $mems = (Invoke-RestMethod "$base/api/memories").memories
    $hasName = $mems | Where-Object { $_.content -match "王小明" }
    $hasCilantro = $mems | Where-Object { $_.content -match "香菜" }
    Check "「记住：」写入长期记忆（名字）" (!!$hasName) (($mems | ForEach-Object { $_.content }) -join " | ")
    Check "「记住：」写入长期记忆（偏好）" (!!$hasCilantro) ""

    # 3) 新会话跨会话检索：换新会话问名字，应引用到记忆
    $sess2 = PostJson "$base/api/sessions" @{ character_id = $cid }
    $sid2 = $sess2.session.id
    $t0 = Get-Date
    $r3 = PostJson "$base/api/chat" @{ session_id = $sid2; message = "你还记得我叫什么名字吗？" }
    $dt3 = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $refs = $r3.used_memories
    $refHasName = @($refs | Where-Object { $_.content -match "王小明" }).Count -gt 0
    Write-Host ("第3条回复（${dt3}s）: " + $r3.reply.Substring(0, [Math]::Min(100, $r3.reply.Length)))
    Check "新会话检索到名字记忆" ($refHasName) (($refs | ForEach-Object { $_.content }) -join " | ")
    Check "回复内容用上了记忆（提到王小明）" ($r3.reply -match "王小明") $r3.reply

    # 4) 语音全链路（应用代理 -> 本地语音服务）
    $cap = Invoke-RestMethod "$base/api/voice" -TimeoutSec 10
    Check "语音服务能力探测（男/女声就绪）" ($cap.available -and $cap.voices.Count -ge 2) ($cap | ConvertTo-Json -Compress)
    $ttsJson = @{ text = "你好，我是语音测试。"; sid = "female"; speed = 1.0 } | ConvertTo-Json
    $t0 = Get-Date
    $wav = Invoke-WebRequest "$base/api/voice/tts" -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($ttsJson)) -ContentType "application/json; charset=utf-8" -TimeoutSec 120 -UseBasicParsing
    $dtTts = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $wavBytes = $wav.Content
    if ($wavBytes -is [string]) { $wavBytes = [Text.Encoding]::GetEncoding("ISO-8859-1").GetBytes($wavBytes) }
    Write-Host ("TTS 经应用代理: ${dtTts}s, " + $wavBytes.Length + " 字节")
    Check "语音合成（女声）经应用代理返回 WAV" ($wavBytes.Length -gt 10000 -and $wavBytes[0] -eq 0x52 -and $wavBytes[1] -eq 0x49) ("len=" + $wavBytes.Length)
    $t0 = Get-Date
    $asr = Invoke-RestMethod "$base/api/voice/asr" -Method Post -Body $wavBytes -ContentType "audio/wav" -TimeoutSec 60
    Write-Host ("ASR 回环: " + $asr.text)
    Check "语音识别回环（合成→识别文字一致）" ($asr.text -match "语音" -and $asr.text -match "测试") $asr.text

    Write-Host ""
    Write-Host ("结果: {0}/{1} 通过 {2}" -f $pass, ($pass + $fail), $(if ($fail) { "[FAIL]" } else { "[ALL OK]" }))
    if ($fail) { exit 1 }
} finally {
    Remove-Item Env:ROLECHAT_DATA_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:ROLECHAT_CONFIG -ErrorAction SilentlyContinue
    try { Stop-Process -Id $p.Id -Force -Confirm:$false } catch {}
    Start-Sleep -Milliseconds 600
    Get-Process -Name RoleChat -ErrorAction SilentlyContinue | Stop-Process -Force -Confirm:$false
    Start-Sleep -Milliseconds 600
    if (Test-Path $td) { Remove-Item $td -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue }
}
