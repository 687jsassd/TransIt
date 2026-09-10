<#
.SYNOPSIS
    构建 TransIt 单目录便携版，并打包为 ZIP。

.DESCRIPTION
    流程：前置校验 → 生成图标/版本资源 → PyInstaller → 放入用户文档
          → 冻结产物冒烟测试 → 压缩 ZIP → SHA256。

.PARAMETER SkipVerify
    跳过对 dist 产物的冒烟测试（不推荐）。

.PARAMETER NoZip
    只构建，不压缩 ZIP。

.PARAMETER Clean
    构建前额外删除 build/ 缓存（完全重建）。

.EXAMPLE
    pwsh -File build.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipVerify,
    [switch]$NoZip,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# 子进程（python/tools）输出的是 UTF-8；不设这个，中文会被按系统 ACP(GBK) 解码成乱码
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

$script:Failures = 0

function Write-Step($msg)  { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  [warn] $msg" -ForegroundColor Yellow }
function Fail($msg)        { Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }

<#
.SYNOPSIS
    用裸 socket 发一条 HTTP/1.1 请求，返回状态码。
    比 Invoke-WebRequest 更贴近真实协议行为 —— 尤其是伪造 Host 头这种
    受限头，.NET 的 HttpClient 会拒绝设置，测不出服务端行为。
#>
function Invoke-RawHttp {
    param(
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$RequestText
    )
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $client.Connect('127.0.0.1', $Port)
        $stream = $client.GetStream()
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($RequestText)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
        $buf = New-Object byte[] 8192
        $n = $stream.Read($buf, 0, $buf.Length)
        $resp = [System.Text.Encoding]::UTF8.GetString($buf, 0, $n)
        if ($resp -match '^HTTP/\d\.\d\s+(\d{3})') { return [int]$Matches[1] }
        return 0
    }
    finally { $client.Close() }
}

# ---------------------------------------------------------------- 前置校验
Write-Step '前置校验'

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { Fail 'PATH 中找不到 python' }
$pyVersion = (& python -c "import sys;print('.'.join(map(str,sys.version_info[:3])))").Trim()
Write-Ok "python $pyVersion ($($python.Source))"

& python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { Fail '未安装 PyInstaller。请先运行：python -m pip install pyinstaller' }
$piVersion = (& python -c "import PyInstaller;print(PyInstaller.__version__)").Trim()
Write-Ok "pyinstaller $piVersion"

if (Test-Path 'config.json') {
    $raw = Get-Content -Raw -Encoding UTF8 'config.json' | ConvertFrom-Json
    if ($raw.api.api_key) {
        Write-Warn2 '工作区 config.json 含 API key —— 已被 .gitignore 排除，且不会进入发布包'
    }
}

& python 'tools/check_config_example.py'
if ($LASTEXITCODE -ne 0) { Fail 'config.example.json 校验未通过' }

if (-not (Test-Path 'web/index.html')) { Fail '缺少 web/index.html' }
if (-not (Test-Path 'packaging/使用说明.txt')) { Fail '缺少 packaging/使用说明.txt' }

# ---------------------------------------------------------------- 生成资产
Write-Step '生成构建资产'

if (-not (Test-Path 'assets/transit.ico')) {
    & python 'tools/make_icon.py' | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail '图标生成失败' }
    Write-Ok '已生成 assets/transit.ico'
} else {
    Write-Ok 'assets/transit.ico 已存在'
}

& python 'tools/make_version_info.py' | Out-Null
if ($LASTEXITCODE -ne 0) { Fail '版本资源生成失败' }
$version = (& python -c "import sys;sys.path.insert(0,'.');from transit import __version__;print(__version__)").Trim()
Write-Ok "版本资源 build/version_info.txt (v$version)"

# ---------------------------------------------------------------- 清理
if ($Clean -and (Test-Path 'build')) {
    Remove-Item -Recurse -Force 'build'
    Write-Ok '已清理 build/'
}
if (Test-Path 'dist/TransIt') { Remove-Item -Recurse -Force 'dist/TransIt' }

# ---------------------------------------------------------------- 构建
Write-Step 'PyInstaller 构建（单目录，两个 exe 共用 _internal）'
& python -m PyInstaller --noconfirm --clean 'TransIt.spec'
if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller 构建失败' }

$distDir = 'dist/TransIt'
$exeWeb  = Join-Path $distDir 'TransIt.exe'
$exeCli  = Join-Path $distDir 'TransIt-CLI.exe'
foreach ($f in @($exeWeb, $exeCli)) {
    if (-not (Test-Path $f)) { Fail "构建产物缺失：$f" }
}
$sizeMb = [math]::Round(((Get-ChildItem $distDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Ok "产物就绪：$distDir ($sizeMb MB)"

# 确认没把用户数据/密钥/测试残留裹进去
foreach ($forbidden in @('config.json', 'output', 'uploads', '_t_edge_profile', '示例翻译文件')) {
    if (Test-Path (Join-Path $distDir $forbidden)) {
        Fail "发布物中不应包含 $forbidden"
    }
}
Write-Ok '发布物不含 config.json / output / uploads / 测试残留'

# ---------------------------------------------------------------- 用户文档
Write-Step '放入用户文档'
$utf8Bom = New-Object System.Text.UTF8Encoding $true
foreach ($doc in @(@('packaging/使用说明.txt', '使用说明.txt'),
                   @('config.example.json', 'config.example.json'))) {
    # 加 BOM，保证记事本双击打开不乱码
    [System.IO.File]::WriteAllText(
        (Join-Path (Resolve-Path -LiteralPath $distDir).Path $doc[1]),
        (Get-Content -Raw -Encoding UTF8 $doc[0]),
        $utf8Bom)
}
Write-Ok '已放入 使用说明.txt + config.example.json'

# ---------------------------------------------------------------- 冒烟测试
if (-not $SkipVerify) {
    Write-Step '冻结产物冒烟测试'

    # 1) CLI：--help 不应创建任何文件（argparse 在加载配置前退出）
    & $exeCli --help | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "TransIt-CLI.exe --help 退出码 $LASTEXITCODE" }
    Write-Ok 'TransIt-CLI.exe --help 正常'

    # 2) CLI：真实运行一次，验证数据目录落在 exe 同目录（冻结路径逻辑）
    $probe = Join-Path $env:TEMP ("transit_probe_" + [guid]::NewGuid().ToString('N') + '.json')
    [System.IO.File]::WriteAllText($probe, '{"こんにちは":"你好"}', (New-Object System.Text.UTF8Encoding $false))
    & $exeCli export $probe 2>&1 | Out-Null   # 无进度文件 → 退出码 1，但配置应已生成
    if (-not (Test-Path (Join-Path $distDir 'config.json'))) {
        Fail 'TransIt-CLI.exe 未在 exe 同目录生成 config.json（冻结数据目录解析错误）'
    }
    Write-Ok '数据目录正确落在 exe 同目录（config.json 已生成）'

    # 3) WebUI：起服务，验证静态资源与三层防线
    $port = 8791
    $env:TRANSIT_API_TOKEN = 'smoke-test-token'
    $proc = Start-Process -FilePath $exeWeb -ArgumentList @('--no-browser', '--port', "$port") `
                          -PassThru -WindowStyle Hidden
    try {
        $base = "http://127.0.0.1:$port"
        $up = $false
        foreach ($i in 1..40) {
            Start-Sleep -Milliseconds 500
            $code = Invoke-RawHttp -Port $port -RequestText "GET / HTTP/1.1`r`nHost: 127.0.0.1:$port`r`nConnection: close`r`n`r`n"
            if ($code -eq 200) { $up = $true; break }
        }
        if (-not $up) { Fail 'WebUI 未能在 20 秒内启动' }
        Write-Ok 'WebUI 已监听，web/ 静态资源在冻结环境中可正常提供'

        # 3a) 正确令牌 → 应放行
        $ok = Invoke-RawHttp -Port $port -RequestText (
            "GET /api/state HTTP/1.1`r`nHost: 127.0.0.1:$port`r`n" +
            "X-TransIt-Token: smoke-test-token`r`nConnection: close`r`n`r`n")
        if ($ok -ne 200) { Fail "带正确令牌访问 /api/state 应 200，实际 $ok" }
        Write-Ok '带正确令牌访问 /api/* 放行（200）'

        # 3b) 无令牌 → 应拒绝
        $noTok = Invoke-RawHttp -Port $port -RequestText (
            "GET /api/state HTTP/1.1`r`nHost: 127.0.0.1:$port`r`nConnection: close`r`n`r`n")
        if ($noTok -ne 403) { Fail "无令牌访问 /api/state 应 403，实际 $noTok" }
        Write-Ok '无令牌访问 /api/* 被拒绝（403）'

        # 3c) 跨站 Origin（CSRF）→ 应拒绝
        $evilOrigin = Invoke-RawHttp -Port $port -RequestText (
            "GET /api/state HTTP/1.1`r`nHost: 127.0.0.1:$port`r`n" +
            "Origin: http://evil.example`r`nX-TransIt-Token: smoke-test-token`r`nConnection: close`r`n`r`n")
        if ($evilOrigin -ne 403) { Fail "跨站 Origin 应 403，实际 $evilOrigin" }
        Write-Ok '跨站 Origin 被拒绝（403，CSRF 防线生效）'

        # 3d) 伪造 Host（DNS 重绑定）→ 应拒绝
        $badHost = Invoke-RawHttp -Port $port -RequestText (
            "GET /api/state HTTP/1.1`r`nHost: attacker.example`r`n" +
            "X-TransIt-Token: smoke-test-token`r`nConnection: close`r`n`r`n")
        if ($badHost -ne 403) { Fail "伪造 Host 应 403，实际 $badHost" }
        Write-Ok '伪造 Host 被拒绝（403，DNS 重绑定防线生效）'
    }
    finally {
        if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
        Remove-Item Env:\TRANSIT_API_TOKEN -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $probe -ErrorAction SilentlyContinue
        # 清掉测试期间产生的运行时数据，保证发布包干净
        Remove-Item -LiteralPath (Join-Path $distDir 'config.json') -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $distDir 'output') -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $distDir 'uploads') -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $distDir 'transit-error.log') -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------- 压缩
if (-not $NoZip) {
    Write-Step '压缩发布包'
    New-Item -ItemType Directory -Force -Path 'release' | Out-Null
    $zipName = "TransIt-$version-win64.zip"
    $zipPath = Join-Path (Resolve-Path -LiteralPath 'release').Path $zipName
    Remove-Item -LiteralPath $zipPath -ErrorAction SilentlyContinue

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    # 以 dist/TransIt 为根 + includeBaseDirectory：解压出来直接是 TransIt\ 一个文件夹
    # （若以 dist 为根会多出一层 dist\TransIt\）。显式 UTF-8 条目名，保证中文名不乱码。
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        (Resolve-Path -LiteralPath $distDir).Path,
        $zipPath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $true,
        [System.Text.UTF8Encoding]::new($false))

    $zipMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Write-Ok "release/$zipName ($zipMb MB)"

    $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
    "$hash  $zipName" | Set-Content -LiteralPath 'release/SHA256SUMS.txt' -Encoding UTF8
    Write-Ok "SHA256 $hash"
}

Write-Step '完成'
Write-Host @"
  dist\TransIt\                        可直接运行的便携版目录
  release\TransIt-$version-win64.zip   发布包
  分发给用户时请连同 SHA256SUMS.txt 一起提供
"@
