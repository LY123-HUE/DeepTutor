# Build EduBuddy end-to-end (Windows).
#   1. builds the offline runtime staging tree (embeddable python + deeptutor + node)
#   2. packages the native shell into dist\EduBuddyDesktop.exe  (PyInstaller)
#   3. (optional) compiles dist\EduBuddySetup.exe with Inno Setup 7
#
# 默认只产出两个包：EduBuddyDesktop.exe + EduBuddySetup.exe。
# dist\EduBuddyPortable.zip 默认【不】制作——它只是同一个运行时的另一种分发形态，
# 每次都要重新压缩 ~500MB / 2.1 万个文件（约 8 分钟），日常迭代没必要。
# 确实需要时显式加 -MakePortable。
#
# Prereqs: Python 3.11+ with PyInstaller, and Inno Setup 7 installed at the
#          standard path (only needed for step 3).
# Run:     powershell -ExecutionPolicy Bypass -File build\build.ps1 [-SkipRuntime] [-MakePortable] [-MakeZip] [-SkipInstaller]

param(
    [switch]$SkipRuntime,       # kept for compatibility; the version gate ALWAYS runs (see below)
    [switch]$MakePortable,      # also build dist\EduBuddyPortable.zip (slow, ~8 min)
    [switch]$MakeZip,           # also build dist\runtime.zip (slow, for Inno path)
    [switch]$SkipInstaller      # do not compile the Inno .iss
)
$ErrorActionPreference = "Stop"
$Root   = Split-Path -Parent $PSScriptRoot
$VenPy  = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenPy)) { Write-Host "venv missing. run:  python -m venv .venv && .\.venv\Scripts\pip install pywebview pillow pyinstaller" -ForegroundColor Red; exit 1 }

Push-Location $Root

Write-Host "[0/3] building and packaging the web frontend ..."
Push-Location (Join-Path (Split-Path -Parent $Root) "web")
try {
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "web build failed" }
} finally { Pop-Location }
& $VenPy (Join-Path (Split-Path -Parent $Root) "scripts\prepare_web_package.py") --skip-build
if ($LASTEXITCODE -ne 0) { throw "web package preparation failed" }
try {
    # ---------- 1. offline runtime (staging tree) ---------------------------
    # 无条件跑 build_runtime.py：它自身幂等——staging 版本与本地源一致且源码未变时
    # 只跑冒烟测试+版本门禁（十几秒）；版本不一致/源码 fingerprint 变化时自动重装。
    # 【版本门禁不可跳过】-SkipRuntime 不再绕过它：曾因跳过这一步把 1.6.9 旧运行时
    # 打进安装包而源码已是 1.6.10（1.6.7 时也发生过一次）。
    Write-Host "[1/3] building/verifying offline runtime (embeddable python + deeptutor + node) ..."
    if ($MakeZip) {
        & $VenPy tools\build_runtime.py
    } else {
        & $VenPy tools\build_runtime.py --no-zip
    }
    if ($LASTEXITCODE -ne 0) { throw "runtime build/gate failed" }

    # ---------- 1b. rebrand staging (DeepTutor -> EduBuddy) -----------------
    # 只重写 staging 产物里的用户可见品牌名；包名/类名/URL 受保护。幂等。
    Write-Host "[1b] rebranding staging (DeepTutor -> EduBuddy) ..."
    & $VenPy tools\rebrand.py
    if ($LASTEXITCODE -ne 0) { throw "rebrand failed" }

    # ---------- 2. native shell exe ------------------------------------------
    Write-Host "[2/3] packaging EduBuddyDesktop.exe (PyInstaller) ..."
    & $VenPy -m PyInstaller --noconfirm --clean build\EduBuddyDesktop.spec
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }

    # ---------- 2b. portable dir + zip (opt-in) ------------------------------
    if ($MakePortable) {
        Write-Host "[2b] assembling dist\portable & EduBuddyPortable.zip ..."
        & $VenPy tools\make_portable.py
        if ($LASTEXITCODE -ne 0) { throw "make_portable failed" }
    } else {
        Write-Host "[2b] skip EduBuddyPortable.zip (default). pass -MakePortable to build it."
    }

    # ---------- 3. click-installer (Inno Setup) ------------------------------
    if (-not $SkipInstaller) {
        $iscc = @(
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
            "C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
            "C:\Program Files\Inno Setup 7\ISCC.exe",
            "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            "C:\Program Files\Inno Setup 6\ISCC.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $iscc) {
            Write-Host "Inno Setup not found — skipping installer. Install from https://jrsoftware.org/isdl.php"
        } else {
            # version injection: app version tracks deeptutor/__version__.py
            $verPy = Join-Path (Split-Path -Parent $Root) "deeptutor\__version__.py"
            $m = Select-String -Path $verPy -Pattern '__version__\s*=\s*"([^"]+)"'
            if (-not $m) { throw "cannot parse __version__ from $verPy" }
            $appVer = "0.2.0+dt" + $m.Matches[0].Groups[1].Value
            Write-Host "[3/3] compiling EduBuddySetup.exe (Inno Setup, AppVersion=$appVer) ..."
            & $iscc "/DMyAppVersion=$appVer" "build\installer.iss"
            if ($LASTEXITCODE -ne 0) { throw "iscc failed" }
        }
    }
    Write-Host ""
    Get-ChildItem "$Root\dist" -Filter *.exe | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}, LastWriteTime | Format-Table -AutoSize
}
finally { Pop-Location }
