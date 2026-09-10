# 生成 GitHub 社交预览图（1280x640）——在仓库链接被分享时显示的卡片。
# 需要 Windows 自带的 System.Drawing（GDI+）与「微软雅黑」字体。
#
# 用法: pwsh -File tools/make_social_preview.ps1
# 产物: assets/social-preview.png
# 注意: 上传需手工操作 —— 仓库 Settings -> General -> Social preview -> Upload an image
[CmdletBinding()]
param([string]$Out = 'assets/social-preview.png')

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

$W = 1280; $H = 640
$bmp = New-Object System.Drawing.Bitmap $W, $H
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic

function C([string]$hex) { [System.Drawing.ColorTranslator]::FromHtml($hex) }
function RoundRect([single]$x, [single]$y, [single]$w, [single]$h, [single]$r) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $r * 2
    $p.AddArc($x, $y, $d, $d, 180, 90)
    $p.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $p.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $p.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $p.CloseFigure()
    return $p
}

$bg       = C '#0D1017'
$panel    = C '#151924'
$border   = C '#262D3F'
$accent   = C '#7C5CFF'
$accent2  = C '#38BDF8'
$text     = C '#E8EAF2'
$muted    = C '#8B93A7'

# ---------- 背景：对角渐变 + 两团辉光 ----------
$bgBrush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
    (New-Object System.Drawing.Point 0, 0),
    (New-Object System.Drawing.Point $W, $H), (C '#0B0E14'), (C '#1A1F2E'))
$g.FillRectangle($bgBrush, 0, 0, $W, $H)
$bgBrush.Dispose()

foreach ($glow in @(@{c='#7C5CFF'; x=210; y=150; r=430}, @{c='#38BDF8'; x=1080; y=520; r=380})) {
    $col = C $glow.c
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddEllipse($glow.x - $glow.r, $glow.y - $glow.r, $glow.r * 2, $glow.r * 2)
    $pg = New-Object System.Drawing.Drawing2D.PathGradientBrush $path
    $pg.CenterColor = [System.Drawing.Color]::FromArgb(46, $col)
    $pg.SurroundColors = @([System.Drawing.Color]::FromArgb(0, $col))
    $g.FillPath($pg, $path)
    $pg.Dispose(); $path.Dispose()
}

# ---------- 应用图标（圆角方块 + 白色箭头） ----------
$ix = 92; $iy = 214; $is = 212
$iconPath = RoundRect $ix $iy $is $is 46
$iconBrush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
    (New-Object System.Drawing.Point $ix, $iy),
    (New-Object System.Drawing.Point ($ix + $is), ($iy + $is)), $accent, $accent2)
$g.FillPath($iconBrush, $iconPath)
$iconBrush.Dispose(); $iconPath.Dispose()

$pts = @(
    @(0.200, 0.435), @(0.495, 0.435), @(0.495, 0.295),
    @(0.805, 0.500), @(0.495, 0.705), @(0.495, 0.565), @(0.200, 0.565)
) | ForEach-Object { New-Object System.Drawing.PointF (($ix + $_[0] * $is), ($iy + $_[1] * $is)) }
$g.FillPolygon([System.Drawing.Brushes]::White, [System.Drawing.PointF[]]$pts)

# ---------- 文字 ----------
$tx = 352
$fTitle = New-Object System.Drawing.Font 'Segoe UI', 82, ([System.Drawing.FontStyle]::Bold), ([System.Drawing.GraphicsUnit]::Pixel)
$g.DrawString('TransIt', $fTitle, (New-Object System.Drawing.SolidBrush $text), $tx, 176)
$szT = $g.MeasureString('TransIt', $fTitle)
$fTitle.Dispose()

# 标题下的强调色短线
$lineBrush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
    (New-Object System.Drawing.Point $tx, 0), (New-Object System.Drawing.Point ($tx + 150), 0), $accent, $accent2)
$g.FillRectangle($lineBrush, $tx, 280, 150, 6)
$lineBrush.Dispose()

$fSub = New-Object System.Drawing.Font 'Microsoft YaHei', 34, ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
$g.DrawString('Mtool 翻译文件 AI 精翻流水线', $fSub, (New-Object System.Drawing.SolidBrush $text), $tx, 312)
$fSub.Dispose()

$fTag = New-Object System.Drawing.Font 'Microsoft YaHei', 24, ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
$g.DrawString('世界观分析建术语库  ·  带行上下文精翻  ·  断点续传  ·  原格式导出',
    $fTag, (New-Object System.Drawing.SolidBrush $muted), $tx, 366)
$fTag.Dispose()

# ---------- 特性标签 ----------
$chips = @('纯 Python 标准库', '零第三方依赖', '浏览器界面', '免安装便携版')
$fChip = New-Object System.Drawing.Font 'Microsoft YaHei', 23, ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
$chipBrush = New-Object System.Drawing.SolidBrush $text
$chipBg = New-Object System.Drawing.SolidBrush (C '#1E2433')
$chipBorder = New-Object System.Drawing.Pen $border, 1.5
$cx = $tx
foreach ($c in $chips) {
    $szc = $g.MeasureString($c, $fChip)
    $cw = [single]($szc.Width + 12)
    $ch = 46
    $path = RoundRect $cx 424 $cw $ch 23
    $g.FillPath($chipBg, $path)
    $g.DrawPath($chipBorder, $path)
    $g.DrawString($c, $fChip, $chipBrush, ($cx + 6), 435)
    $path.Dispose()
    $cx += $cw + 14
}
$fChip.Dispose(); $chipBg.Dispose(); $chipBorder.Dispose(); $chipBrush.Dispose()

# ---------- 页脚 ----------
$fFoot = New-Object System.Drawing.Font 'Consolas', 22, ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
$g.DrawString('github.com/687jsassd/TransIt', $fFoot, (New-Object System.Drawing.SolidBrush $accent2), $tx, 512)
$fFoot.Dispose()

# ---------- 输出 ----------
$dir = Split-Path -Parent $Out
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$bmp.Save((Join-Path (Get-Location) $Out), [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()

$fi = Get-Item $Out
Write-Host "已生成 $($fi.FullName)  ($([math]::Round($fi.Length/1KB,1)) KB, ${W}x${H})"
