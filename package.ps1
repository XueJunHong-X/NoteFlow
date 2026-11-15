# NoteFlow 一条命令：构建 + 红线核对 + 打包验收 + 结论表
#
# 用法：
#   .\package.ps1               # 完整流程：重新构建，然后自动验收
#   .\package.ps1 -SkipBuild    # 跳过构建，只对现有产物做验收（调试用）
#
# 任一项不通过时以非零退出码结束，并在输出中指明是哪一项、为什么。
# 明细固定写入仓库根的 package_report.json。

param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Exe = Join-Path $Root "2026-11-11\note-flow-competition\outputs\NoteFlow\NoteFlow.exe"

if (-not $SkipBuild) {
    Write-Host "== [1/2] 构建（沿用 PyInstaller onedir 配置与产物路径）==" -ForegroundColor Cyan
    try {
        & (Join-Path $Root "build.ps1") -SkipInstall
    } catch {
        Write-Host "构建失败：$($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "== [1/2] 跳过构建（-SkipBuild）==" -ForegroundColor DarkGray
}

if (-not (Test-Path $Exe)) {
    Write-Host "产物不存在：$Exe" -ForegroundColor Red
    exit 1
}

Write-Host "== [2/2] 红线核对与打包验收 ==" -ForegroundColor Cyan
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

& $Python (Join-Path $Root "pack_verify.py")
exit $LASTEXITCODE
