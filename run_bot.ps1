# =============================================================================
# AI Trade bot 啟動腳本（供 Windows 工作排程器呼叫）
# - 切換至 repo 目錄，透過 uv 執行 bot.py
# - stdout/stderr 寫入 logs\runtime_YYYYMMDD.log（bot 內部 print 全數保留）
# - 排程設定：平日 08:25 觸發、喚醒電腦執行（見 Register-ScheduledTask）
# =============================================================================

$repo = "c:\Users\yinya\git\AI_trade"
Set-Location $repo

New-Item -ItemType Directory -Force -Path (Join-Path $repo "logs") | Out-Null
$log = Join-Path $repo ("logs\runtime_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

# 解析 uv 路徑（winget 安裝位置優先，其次 PATH）
$uv = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe"
if (-not (Test-Path $uv)) {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { $uv = $cmd.Source } else {
        "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] 找不到 uv，中止。" | Out-File -Append -Encoding utf8 $log
        exit 1
    }
}

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ── 排程啟動 bot.py ──────────────" | Out-File -Append -Encoding utf8 $log

# 透過 cmd 原生轉向寫 log：避免 PowerShell 5.1 將 stderr 包裝成 UTF-16 錯誤記錄造成亂碼
$env:PYTHONIOENCODING = "utf-8"
& cmd /c "`"$uv`" run python bot.py >> `"$log`" 2>&1"

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ── bot.py 結束（exit=$LASTEXITCODE）──" | Out-File -Append -Encoding utf8 $log
