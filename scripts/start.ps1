<#
.SYNOPSIS
    Agent App 一键启动脚本（后端 FastAPI + 前端 Next.js）。

.DESCRIPTION
    - 自动准备环境：Python 虚拟环境、后端依赖、.env、前端依赖、.env.local
    - 启动前检查端口占用，并把端口同步进 backend/.env 与 frontend/.env.local
    - 启动后端（uvicorn --reload）与前端（next dev），日志写入 .run/*.log
    - 启动后轮询健康检查，就绪后再提示访问地址
    - Ctrl+C 一键停止两个服务（连带子进程）
    - 支持 -Stop 清理残留进程、-Setup 只准备环境不启动

.EXAMPLE
    ./scripts/start.ps1                 # 准备环境并启动前后端
    ./scripts/start.ps1 -SkipInstall    # 跳过依赖安装（仍做一次轻量探测）
    ./scripts/start.ps1 -Target backend # 只启动后端
    ./scripts/start.ps1 -Setup          # 只准备环境
    ./scripts/start.ps1 -Stop           # 停止上次启动的服务
#>
#Requires -Version 5.1

[CmdletBinding()]
param(
    [ValidateSet('both', 'backend', 'frontend')]
    [string]$Target = 'both',

    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000,

    [switch]$SkipInstall,
    [switch]$Setup,
    [switch]$Stop
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# 仅在输出被重定向时切 UTF-8，避免交互终端（GBK 代码页）变乱码
if ([Console]::IsOutputRedirected) {
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
}

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
$Root        = Split-Path -Parent $PSScriptRoot
$BackendDir  = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$RunDir      = Join-Path $Root '.run'
$PidFile     = Join-Path $RunDir 'pids.json'

$VenvDir     = Join-Path $BackendDir '.venv'
$VenvPython  = Join-Path $VenvDir 'Scripts\python.exe'

$BackendEnv  = Join-Path $BackendDir '.env'
$FrontendEnv = Join-Path $FrontendDir '.env.local'

$ReadyTimeout = 40

# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------
function Write-Step { param([string]$Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "  [OK] $Message" -ForegroundColor Green }
function Write-Warn2{ param([string]$Message) Write-Host "  [!]  $Message" -ForegroundColor Yellow }
function Write-Err  { param([string]$Message) Write-Host "  [X]  $Message" -ForegroundColor Red }

# 致命错误：整行提示后退出，避免抛出 PowerShell 异常栈噪音
function Fail {
    param([string]$Message)
    Write-Host ''
    Write-Err $Message
    exit 1
}

function Write-Banner {
    Write-Host ''
    Write-Host '  Agent App 快速启动' -ForegroundColor White
    Write-Host '  ------------------' -ForegroundColor DarkGray
    Write-Host ("  后端: http://localhost:{0}      (接口文档 /docs)" -f $BackendPort) -ForegroundColor DarkGray
    if ($Target -ne 'backend') {
        Write-Host ("  前端: http://localhost:{0}" -f $FrontendPort) -ForegroundColor DarkGray
    }
    Write-Host ''
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# ---------------------------------------------------------------------------
# 端口检查（避免「端口被占用 → 服务启动即静默退出」）
# ---------------------------------------------------------------------------
function Get-PortHolder {
    param([int]$Port)

    if (Test-CommandExists 'Get-NetTCPConnection') {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $conn) { return $null }
        $owner = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        $name = if ($owner) { $owner.ProcessName } else { 'unknown' }
        return [pscustomobject]@{ Pid = [int]$conn.OwningProcess; Name = $name }
    }

    # 回退：尝试独占绑定，绑不上即视为被占用（拿不到占用者信息）
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $Port
        $listener.Start()
        return $null
    } catch {
        return [pscustomobject]@{ Pid = 0; Name = 'unknown' }
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

function Assert-PortAvailable {
    param([int]$Port, [string]$Label, [string]$PortSwitch)

    $holder = Get-PortHolder -Port $Port
    if (-not $holder) { return }

    $who = if ($holder.Pid -gt 0) { "PID {0} / {1}" -f $holder.Pid, $holder.Name } else { '未知进程' }
    Fail ("{0} 端口 {1} 已被占用（{2}）。请先释放该端口，或用 {3} 指定其他端口" -f $Label, $Port, $who, $PortSwitch)
}

# ---------------------------------------------------------------------------
# .env 读写（端口联动 + 默认密钥检查）
# ---------------------------------------------------------------------------
function Get-EnvValue {
    param([string]$Path, [string]$Key)

    if (-not (Test-Path $Path)) { return $null }
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*=(.*)$'
    foreach ($line in @(Get-Content -Path $Path -Encoding UTF8)) {
        if ($line -match $pattern) { return $Matches[1].Trim() }
    }
    return $null
}

function Set-EnvValue {
    param([string]$Path, [string]$Key, [string]$Value)

    $lines = @(Get-Content -Path $Path -Encoding UTF8)
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*='
    $done = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Key=$Value"
            $done = $true
            break
        }
    }
    if (-not $done) { $lines += "$Key=$Value" }
    Set-Content -Path $Path -Value $lines -Encoding UTF8
}

# 仅当原值为本机地址时改写；用户自定义过的值只提示、不覆盖
function Sync-LocalUrl {
    param([string]$Path, [string]$Key, [int]$Port)

    $current = Get-EnvValue -Path $Path -Key $Key
    if (-not $current) { return }

    $target = "http://localhost:$Port"
    if ($current -eq $target) { return }

    if ($current -match '^https?://(localhost|127\.0\.0\.1)(:\d+)?/?$') {
        Set-EnvValue -Path $Path -Key $Key -Value $target
        Write-Ok ("{0}: {1} -> {2}" -f $Key, $current, $target)
    } else {
        Write-Warn2 ("{0} 已自定义为 {1}，脚本未改动；请自行确认其中包含 {2}" -f $Key, $current, $target)
    }
}

function Sync-PortConfig {
    if (Test-Path $BackendEnv) {
        Sync-LocalUrl -Path $BackendEnv -Key 'CORS_ORIGINS' -Port $FrontendPort
        Sync-LocalUrl -Path $BackendEnv -Key 'APP_BASE_URL' -Port $FrontendPort
    }
    if (Test-Path $FrontendEnv) {
        Sync-LocalUrl -Path $FrontendEnv -Key 'NEXT_PUBLIC_API_BASE_URL' -Port $BackendPort
    }
}

function Test-DefaultSecrets {
    if (-not (Test-Path $BackendEnv)) { return }

    $warned = $false
    foreach ($item in @(
            @{ Key = 'JWT_SECRET'; Value = 'dev-secret-change-me' },
            @{ Key = 'ENCRYPTION_KEY'; Value = 'dev-encryption-key-change-me' }
        )) {
        if ((Get-EnvValue -Path $BackendEnv -Key $item.Key) -eq $item.Value) {
            Write-Warn2 ("{0} 仍是默认值，建议替换为随机长字符串（生成方式见 .env.example 注释）" -f $item.Key)
            $warned = $true
        }
    }
    if (-not (Get-EnvValue -Path $BackendEnv -Key 'ADMIN_PASSWORD')) {
        Write-Warn2 'ADMIN_PASSWORD 为空，启动时不会自动创建管理员账号'
        $warned = $true
    }
    if ($warned) { Write-Host '' }
}

# ---------------------------------------------------------------------------
# 进程记录（记录 PID + 进程名 + 启动时间，停止前校验，避免 PID 复用误杀）
# ---------------------------------------------------------------------------
function New-ProcessRecord {
    param([System.Diagnostics.Process]$Process)
    return [pscustomobject]@{
        Pid       = [int]$Process.Id
        Name      = $Process.ProcessName
        StartedAt = $Process.StartTime
    }
}

function Test-ProcessRecord {
    param($Record)

    if (-not $Record) { return $false }
    $proc = Get-Process -Id $Record.Pid -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    if ($Record.Name -and $proc.ProcessName -ne $Record.Name) { return $false }
    if ($Record.StartedAt) {
        if ([math]::Abs(($proc.StartTime - $Record.StartedAt).TotalSeconds) -gt 2) { return $false }
    }
    return $true
}

# 兼容旧格式（纯 PID）与新格式（对象）
function ConvertTo-ProcessRecord {
    param($Value)

    if ($null -eq $Value) { return $null }
    if ($Value -is [int] -or $Value -is [long]) {
        return [pscustomobject]@{ Pid = [int]$Value; Name = ''; StartedAt = $null }
    }
    $startedAt = $null
    if ($Value.startedAt) {
        try {
            $startedAt = [datetime]::Parse([string]$Value.startedAt, $null,
                [System.Globalization.DateTimeStyles]::RoundtripKind)
        } catch { $startedAt = $null }
    }
    return [pscustomobject]@{ Pid = [int]$Value.pid; Name = [string]$Value.name; StartedAt = $startedAt }
}

function Save-Pids {
    param([hashtable]$Map)
    if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir -Force | Out-Null }
    $obj = [ordered]@{}
    foreach ($key in $Map.Keys) {
        $rec = $Map[$key]
        if (-not $rec) { continue }
        $obj[$key] = [ordered]@{
            pid       = [int]$rec.Pid
            name      = [string]$rec.Name
            startedAt = $rec.StartedAt.ToString('o')
        }
    }
    $obj | ConvertTo-Json -Depth 4 | Set-Content -Path $PidFile -Encoding UTF8
}

function Stop-ProcessTree {
    param([int]$ProcessId, [string]$Label)

    if (-not $ProcessId) { return }
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return }
    Write-Host ("  停止 {0} (PID {1})" -f $Label, $ProcessId) -ForegroundColor DarkGray
    # /T 连带子进程（uvicorn --reload 的 worker、next dev 的 node 子进程）
    & taskkill.exe /PID $ProcessId /T /F *> $null
}

function Stop-PreviousServices {
    if (-not (Test-Path $PidFile)) {
        Write-Warn2 '没有找到上次启动记录的进程'
        return
    }
    try {
        $saved = Get-Content $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Warn2 '进程记录已损坏，跳过'
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        return
    }
    Write-Step '停止上次启动的服务'
    foreach ($prop in $saved.PSObject.Properties) {
        $rec = ConvertTo-ProcessRecord -Value $prop.Value
        if (-not $rec) { continue }
        if (-not (Get-Process -Id $rec.Pid -ErrorAction SilentlyContinue)) { continue }
        if (-not (Test-ProcessRecord -Record $rec)) {
            Write-Warn2 ("PID {0} 已不是启动时的进程（记录为 {1}），跳过以免误杀" -f $rec.Pid, $prop.Name)
            continue
        }
        Stop-ProcessTree -ProcessId $rec.Pid -Label $prop.Name
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Ok '已清理'
}

# ---------------------------------------------------------------------------
# 环境准备
# ---------------------------------------------------------------------------
function Initialize-Backend {
    Write-Step '准备后端环境'

    if (-not (Test-Path $VenvPython)) {
        if (-not (Test-CommandExists 'python')) {
            Fail '未找到 python，请先安装 Python 3.11+ 并加入 PATH'
        }
        Write-Host '  创建虚拟环境 .venv ...' -ForegroundColor DarkGray
        & python -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { Fail '创建虚拟环境失败' }
    }
    Write-Ok ((& $VenvPython --version 2>&1) -join ' ')

    if (-not (Test-Path $BackendEnv)) {
        Copy-Item (Join-Path $BackendDir '.env.example') $BackendEnv
        Write-Ok '已根据 .env.example 生成 backend/.env'
    } else {
        Write-Ok 'backend/.env 已存在'
    }

    # 即使跳过安装也做一次毫秒级探测，避免依赖缺失拖到启动阶段才暴露
    & $VenvPython -c 'import fastapi, uvicorn, sqlalchemy' 2> $null
    $depsOk = ($LASTEXITCODE -eq 0)

    if ($SkipInstall) {
        if ($depsOk) { Write-Ok '后端依赖就绪' }
        else { Write-Warn2 '后端依赖不完整（已按 -SkipInstall 跳过安装），启动可能失败' }
        return
    }

    if (-not $depsOk) {
        Write-Host '  安装后端依赖 (requirements-dev.txt) ...' -ForegroundColor DarkGray
        & $VenvPython -m pip install --upgrade pip *> $null
        & $VenvPython -m pip install -r (Join-Path $BackendDir 'requirements-dev.txt')
        if ($LASTEXITCODE -ne 0) { Fail '后端依赖安装失败' }
    }
    Write-Ok '后端依赖就绪'
}

function Initialize-Frontend {
    Write-Step '准备前端环境'

    if (-not (Test-CommandExists 'npm')) {
        Fail '未找到 npm，请先安装 Node.js 18+ 并加入 PATH'
    }
    Write-Ok ("Node {0} / npm {1}" -f (& node --version), (& npm --version))

    if (-not (Test-Path $FrontendEnv)) {
        Copy-Item (Join-Path $FrontendDir '.env.local.example') $FrontendEnv
        Write-Ok '已根据 .env.local.example 生成 frontend/.env.local'
    } else {
        Write-Ok 'frontend/.env.local 已存在'
    }

    $hasModules = Test-Path (Join-Path $FrontendDir 'node_modules')

    if ($SkipInstall) {
        if ($hasModules) { Write-Ok '前端依赖就绪' }
        else { Write-Warn2 'node_modules 不存在（已按 -SkipInstall 跳过安装），启动可能失败' }
        return
    }

    if (-not $hasModules) {
        Write-Host '  安装前端依赖 (npm install) ...' -ForegroundColor DarkGray
        Push-Location $FrontendDir
        try {
            & npm install
            if ($LASTEXITCODE -ne 0) { Fail '前端依赖安装失败' }
        } finally {
            Pop-Location
        }
    }
    Write-Ok '前端依赖就绪'
}

# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
function Start-Backend {
    $logFile = Join-Path $RunDir 'backend.log'
    # 让 uvicorn 日志按 UTF-8 落盘，避免中文在重定向文件中变成乱码
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    # --reload 默认监视整个 backend/，其中 .venv 里有上万个第三方 .py。服务运行期间一旦
    # pip install（例如补装 pypdf），.venv 会瞬间产生海量文件事件，重载器可能卡在
    # 「WatchFiles detected changes ... Reloading...」而不再重启 worker：表面上服务还在跑，
    # 实际一直执行旧代码（改了后端不生效，排查时极具误导性），故显式排除。
    $argList = @(
        '-m', 'uvicorn', 'app.main:app', '--reload',
        '--reload-exclude', '.venv', '--reload-exclude', '__pycache__',
        '--port', "$BackendPort"
    )
    $proc = Start-Process -FilePath $VenvPython -ArgumentList $argList `
        -WorkingDirectory $BackendDir -PassThru -RedirectStandardOutput $logFile `
        -RedirectStandardError (Join-Path $RunDir 'backend.err.log')
    Write-Ok ("后端已启动 (PID {0})，日志: .run/backend.log" -f $proc.Id)
    return (New-ProcessRecord -Process $proc)
}

function Start-Frontend {
    $logFile = Join-Path $RunDir 'frontend.log'
    # 经 cmd.exe 启动 npm（.cmd 无法被 CreateProcess 直接执行，且需重定向输出）
    # 端口统一由此处传入（package.json 的 dev 脚本不再写死端口）
    $argList = @('/c', 'npm', 'run', 'dev', '--', '-p', "$FrontendPort")
    $proc = Start-Process -FilePath 'cmd.exe' -ArgumentList $argList `
        -WorkingDirectory $FrontendDir -PassThru -RedirectStandardOutput $logFile `
        -RedirectStandardError (Join-Path $RunDir 'frontend.err.log')
    Write-Ok ("前端已启动 (PID {0})，日志: .run/frontend.log" -f $proc.Id)
    return (New-ProcessRecord -Process $proc)
}

function Test-TcpPort {
    param([int]$Port, [int]$TimeoutMs = 1000)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $task.Wait($TimeoutMs)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# 就绪判定：先等端口监听（稳定），再做一次宽松的 HTTP 确认
# （next dev 首屏要现编译，用短超时做 HTTP 轮询会误判为「未就绪」）
function Wait-ServiceReady {
    param(
        [string]$Url,
        [int]$Port,
        [string]$Label,
        [string]$LogPrefix,
        [int]$TimeoutSeconds = $ReadyTimeout
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $listening = $false
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPort -Port $Port) { $listening = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $listening) {
        Write-Warn2 ("{0} 在 {1}s 内未监听端口 {2}，请查看 .run/{3}*.log" -f $Label, $TimeoutSeconds, $Port, $LogPrefix)
        return $false
    }

    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 20
        if ($resp.StatusCode -lt 400) {
            Write-Ok ("{0} 就绪: {1}" -f $Label, $Url)
            return $true
        }
    } catch { }

    Write-Ok ("{0} 已在端口 {1} 监听（首次访问可能仍在编译）: {2}" -f $Label, $Port, $Url)
    return $true
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir -Force | Out-Null }

if ($Stop) {
    Stop-PreviousServices
    exit 0
}

Stop-PreviousServices
Write-Banner

# 端口预检：占用则直接失败，避免服务启动后立刻静默退出
if ($Target -ne 'frontend') { Assert-PortAvailable -Port $BackendPort -Label '后端' -PortSwitch '-BackendPort' }
if ($Target -ne 'backend') { Assert-PortAvailable -Port $FrontendPort -Label '前端' -PortSwitch '-FrontendPort' }

if ($Target -ne 'frontend') { Initialize-Backend }
if ($Target -ne 'backend')  { Initialize-Frontend }

Write-Step '同步端口配置'
Sync-PortConfig
Test-DefaultSecrets

if ($Setup) {
    Write-Host ''
    Write-Ok '环境准备完成（未启动服务）'
    exit 0
}

$started = @{ 'backend' = $null; 'frontend' = $null }
$exitCode = 0
try {
    Write-Step '启动服务'
    if ($Target -ne 'frontend') { $started['backend'] = Start-Backend }
    if ($Target -ne 'backend')  { $started['frontend'] = Start-Frontend }
    Save-Pids $started

    Write-Host ''
    Write-Step '等待服务就绪'
    if ($started['backend']) {
        [void](Wait-ServiceReady -Url "http://127.0.0.1:$BackendPort/api/v1/health" -Port $BackendPort -Label '后端' -LogPrefix 'backend')
    }
    if ($started['frontend']) {
        [void](Wait-ServiceReady -Url "http://127.0.0.1:$FrontendPort/" -Port $FrontendPort -Label '前端' -LogPrefix 'frontend')
    }

    Write-Host ''
    Write-Host '  服务运行中，按 Ctrl+C 停止全部服务。' -ForegroundColor Yellow
    if ($Target -ne 'backend') { Write-Host ("  浏览器打开: http://localhost:{0}" -f $FrontendPort) -ForegroundColor White }

    # 保持前台存活，Ctrl+C 时进入 finally 清理
    while ($true) {
        Start-Sleep -Seconds 1
        foreach ($key in @('backend', 'frontend')) {
            $rec = $started[$key]
            if ($rec -and -not (Test-ProcessRecord -Record $rec)) {
                Write-Warn2 ("{0} 进程已退出 (PID {1})，请查看 .run/{0}*.log" -f $key, $rec.Pid)
                $started[$key] = $null
            }
        }
        if (-not $started['backend'] -and -not $started['frontend']) {
            Write-Err '所有服务均已退出'
            $exitCode = 1
            break
        }
    }
} finally {
    Write-Host ''
    Write-Step '停止服务'
    foreach ($key in @('backend', 'frontend')) {
        $rec = $started[$key]
        if (Test-ProcessRecord -Record $rec) {
            Stop-ProcessTree -ProcessId $rec.Pid -Label $key
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Ok '已全部停止'
}
exit $exitCode
