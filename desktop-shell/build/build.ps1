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
    [switch]$SkipRuntime,       # reuse the existing runtime-build staging
    [switch]$MakePortable,      # also build dist\EduBuddyPortable.zip (slow, ~8 min)
    [switch]$MakeZip,           # also build dist\runtime.zip (slow, for Inno path)
    [switch]$SkipInstaller      # do not compile the Inno .iss
)
$ErrorActionPreference = "Stop"
$Root   = Split-Path -Parent $PSScriptRoot
$VenPy  = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenPy)) { Write-Host "venv missing. run:  python -m venv .venv && .\.venv\Scripts\pip install pywebview pillow pyinstaller" -ForegroundColor Red; exit 1 }

Push-Location $Root
try {
    # ---------- 1. offline runtime (staging tree) ---------------------------
    if (-not $SkipRuntime -and -not (Test-Path "$Root\runtime-build\staging\python\python.exe")) {
        Write-Host "[1/3] building offline runtime (embeddable python + deeptutor + node) ..."
        if ($MakeZip) {
            & $VenPy tools\build_runtime.py
        } else {
            & $VenPy tools\build_runtime.py --no-zip
        }
        if ($LASTEXITCODE -ne 0) { throw "runtime build failed" }
    } else {
        Write-Host "[1/3] reusing runtime-build\staging"
    }

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
            Write-Host "[3/3] compiling EduBuddySetup.exe (Inno Setup) ..."
            & $iscc "build\installer.iss"
            if ($LASTEXITCODE -ne 0) { throw "iscc failed" }
        }
    }
    Write-Host ""
    Get-ChildItem "$Root\dist" -Filter *.exe | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}, LastWriteTime | Format-Table -AutoSize
}
finally { Pop-Location }
