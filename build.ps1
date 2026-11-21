# NoteFlow 一键构建脚本（Windows PowerShell 5+）
#
# 用法：
#   .\build.ps1                # 默认构建到 2026-11-11\note-flow-competition\outputs\NoteFlow
#   .\build.ps1 -SkipInstall   # 已装好依赖与 PyInstaller，跳过 pip
#   .\build.ps1 -Console       # 构建带控制台窗口的版本（调试用）
#   .\build.ps1 -DistRoot dist # 换产物输出目录（公开仓库使用者建议改成这个）
#
# 产物：<DistRoot>\NoteFlow\NoteFlow.exe（--onedir，启动快、被杀软误报少）

param(
    [switch]$SkipInstall,
    [switch]$Console,
    # 产物版本号：写进包内 VERSION.txt，发布 zip 与 GitHub Release 同名
    [string]$Version = "0.1.2",
    # 产物输出根目录（默认沿用备赛目录；公开仓库使用者可改成 .\dist）
    [string]$DistRoot = "2026-11-11\note-flow-competition\outputs"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "[1/4] 没有找到 .venv，改用系统 python" -ForegroundColor Yellow
    $Python = "python"
} else {
    Write-Host "[1/4] 使用虚拟环境: $Python" -ForegroundColor Cyan
}

if (-not $SkipInstall) {
    Write-Host "[2/4] 安装依赖（首次会下载几百 MB）..." -ForegroundColor Cyan
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r requirements.txt
    & $Python -m pip install pyinstaller
} else {
    Write-Host "[2/4] 跳过依赖安装" -ForegroundColor DarkGray
}

$DistPath = Join-Path $RepoRoot $DistRoot
$WorkPath = Join-Path $DistPath "..\work\pyinstaller"

Write-Host "[3/4] 开始打包（onedir + 内置样例图 + 隐藏导入）..." -ForegroundColor Cyan

# 只收集合成样例图（*.png）。tests\sample_images\real\ 是个人真实笔记，绝不能进安装包。
$Staging = Join-Path $WorkPath "assets\sample_images"
if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
New-Item -ItemType Directory -Path $Staging -Force | Out-Null
Copy-Item (Join-Path $RepoRoot "tests\sample_images\*.png") -Destination $Staging -Force
Write-Host "      内置样例图: $((Get-ChildItem $Staging -File).Count) 张（不含 real 目录）" -ForegroundColor DarkGray

$SampleData = $Staging + ";sample_images"

# 应用图标：--icon 写进 exe 资源（资源管理器/任务栏），--add-data 供窗口标题栏运行时加载
$IconFile = Join-Path $RepoRoot "assets\icon\NoteFlow.ico"
if (-not (Test-Path $IconFile)) {
    throw "找不到图标文件：$IconFile"
}
# 只把 .ico 打进包内（图标源图和生成脚本是开发用的，不进安装包）
$IconStage = Join-Path $WorkPath "assets\icon"
if (Test-Path $IconStage) { Remove-Item $IconStage -Recurse -Force }
New-Item -ItemType Directory -Path $IconStage -Force | Out-Null
Copy-Item $IconFile -Destination $IconStage -Force
$IconData = $IconStage + ";assets\icon"
Write-Host "      应用图标: $IconFile" -ForegroundColor DarkGray

# 本地离线说明书：设置页「说明书」按钮直接打开这份 HTML，打包后也要在
$ManualFile = Join-Path $RepoRoot "docs\byok-guide.html"
if (-not (Test-Path $ManualFile)) {
    throw "找不到本地说明书：$ManualFile"
}
$ManualData = $ManualFile + ";docs"
Write-Host "      离线说明书: $ManualFile" -ForegroundColor DarkGray

$ArgsList = @(
    "--noconfirm",
    "--clean",
    "--onedir",
    "--name", "NoteFlow",
    "--icon", $IconFile,
    "--distpath", $DistPath,
    "--workpath", $WorkPath,
    "--specpath", $WorkPath,
    # 把合成样例图打进去：没有仓库也能点「用内置样例演示」
    "--add-data", $SampleData,
    # 图标打进包内，运行时设置窗口图标用
    "--add-data", $IconData,
    # 本地离线说明书打进包内，设置页「说明书」按钮直接打开
    "--add-data", $ManualData,
    # 这几个模块是运行时才 import 的，PyInstaller 静态分析看不到
    "--hidden-import", "cv2",
    "--hidden-import", "numpy",
    "--hidden-import", "PIL.Image",
    "--hidden-import", "fitz",
    "--hidden-import", "pymupdf",
    "--hidden-import", "watchdog.observers",
    "--hidden-import", "watchdog.observers.winapi",
    "--hidden-import", "requests",
    "--hidden-import", "src.loader",
    "--hidden-import", "src.preprocessor",
    "--hidden-import", "src.recognizer",
    "--hidden-import", "src.markdown_gen",
    "--hidden-import", "src.mindmap_gen",
    "--hidden-import", "src.linker",
    "--hidden-import", "src.organizer",
    "--hidden-import", "src.watcher",
    "--hidden-import", "src.failure_log",
    "--hidden-import", "src.onedrive",
    "--hidden-import", "src.cleanup",
    "--hidden-import", "src.manifest",
    "--hidden-import", "send2trash",
    "--hidden-import", "src.appconfig",
    "--hidden-import", "src.offline_samples",
    "--hidden-import", "src.resources",
    "--hidden-import", "src.openers",
    "--hidden-import", "src.controller",
    "--hidden-import", "src.ui.theme",
    "--hidden-import", "src.ui.widgets",
    "--hidden-import", "src.ui.settings_dialog",
    "--hidden-import", "src.ui.cleanup_dialog",
    "--hidden-import", "src.ui.app",
    "--hidden-import", "windnd",
    # 体积控制：这些可选依赖用不到
    "--exclude-module", "matplotlib",
    "--exclude-module", "scipy",
    "--exclude-module", "pandas",
    "--exclude-module", "tkinter.test",
    "--exclude-module", "test",
    # 入口
    "gui.py"
)

if ($Console) {
    Write-Host "      构建带控制台版本（调试用）" -ForegroundColor DarkGray
    $ArgsList = @("--console") + $ArgsList
} else {
    $ArgsList = @("--windowed") + $ArgsList
}

& $Python -m PyInstaller @ArgsList

if ($LASTEXITCODE -ne 0) {
    throw "打包失败，退出码 $LASTEXITCODE"
}

$Exe = Join-Path $DistPath "NoteFlow\NoteFlow.exe"
if (-not (Test-Path $Exe)) {
    throw "打包结束但找不到 $Exe"
}
Set-Content -Path (Join-Path $DistPath "NoteFlow\VERSION.txt") -Value "v$Version" -Encoding utf8

# 体积裁剪：这两块运行时用不到，PyInstaller 的 hook 会把它们整包收进来。
# opencv 的视频 I/O 编解码库（只做静态图处理）、PyMuPDF 的版面/表格 ONNX 模型（只做页面栅格化）。
$Prune = @(
    (Join-Path $DistPath "NoteFlow\_internal\cv2"),
    (Join-Path $DistPath "NoteFlow\_internal\pymupdf\layout\resources\onnx")
)
foreach ($dir in $Prune) {
    if (Test-Path $dir) {
        Get-ChildItem $dir -File | Where-Object {
            $_.Name -like "opencv_videoio_ffmpeg*" -or $_.Extension -eq ".onnx"
        } | ForEach-Object {
            Remove-Item $_.FullName -Force
        }
    }
}

$SizeMB = [math]::Round(((Get-ChildItem (Split-Path $Exe) -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)

Write-Host "[4/4] 完成" -ForegroundColor Green
Write-Host "      版本  : v$Version"
Write-Host "      exe   : $Exe"
Write-Host "      体积  : $SizeMB MB（整个 NoteFlow 目录）"
Write-Host "      自检  : & `"$Exe`" --selftest"
Write-Host "      命令行: & `"$Exe`" --cli --once --mock"
