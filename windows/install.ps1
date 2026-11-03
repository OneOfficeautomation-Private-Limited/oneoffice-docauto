<#
  OneOffice DocFlow - Windows installer
  Run from PowerShell in the folder that contains this script:
      powershell -ExecutionPolicy Bypass -File .\install.ps1
  What it does (no admin needed except for the optional winget installs):
    1. Checks / installs Python 3.10+ and Tesseract OCR (via winget)
    2. Prepares a local tessdata folder with English + Arabic language data
    3. Creates a Python virtual environment and installs dependencies
    4. Creates config\local.env, generates sample documents and runs the test suite
#>
param([switch]$SkipTests, [switch]$NoShortcuts)
$ErrorActionPreference = "Stop"
$App = Split-Path -Parent $PSScriptRoot           # ...\oneoffice-docauto
Set-Location $App
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    !!  $m" -ForegroundColor Yellow }

# ---------------------------------------------------------------- Python
Step "Checking Python 3.10+"
$py = $null
$candidates = @(@("py","-3.12"), @("py","-3.11"), @("py","-3.10"), @("py","-3"), @("python"))
function Get-PyVer($cand) {
    try {
        $exe = $cand[0]; $pre = @($cand | Select-Object -Skip 1)
        $v = & $exe @pre -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { return [int]($v | Select-Object -Last 1) }
    } catch {}
    return 0
}
foreach ($c in $candidates) { if ((Get-PyVer $c) -ge 310) { $py = $c; break } }
if (-not $py) {
    Warn "Python 3.10+ not found. Installing Python 3.12 with winget..."
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    foreach ($c in $candidates) { if ((Get-PyVer $c) -ge 310) { $py = $c; break } }
    if (-not $py) { throw "Python install finished but python was not found. Open a new PowerShell window and re-run install.ps1." }
}
Ok ("Using: " + ($py -join " "))

# ---------------------------------------------------------------- Tesseract
Step "Checking Tesseract OCR"
$tessExe = "C:\Program Files\Tesseract-OCR\tesseract.exe"
$cmdTess = Get-Command tesseract -ErrorAction SilentlyContinue
if ($cmdTess) { $tessExe = $cmdTess.Source }
if (-not (Test-Path $tessExe)) {
    Warn "Tesseract not found. Installing (UB Mannheim build) with winget..."
    winget install --id UB-Mannheim.TesseractOCR -e --accept-source-agreements --accept-package-agreements
}
if (-not (Test-Path $tessExe)) { throw "Tesseract still not found at $tessExe. Install it from https://github.com/UB-Mannheim/tesseract/wiki and re-run." }
Ok "Tesseract: $tessExe"

Step "Preparing language data (English + Arabic) in $App\tessdata"
$td = Join-Path $App "tessdata"
New-Item -ItemType Directory -Force -Path $td | Out-Null
$sysTd = Join-Path (Split-Path $tessExe) "tessdata"
New-Item -ItemType Directory -Force -Path (Join-Path $td "configs") | Out-Null
# language models + files Tesseract needs to write searchable PDFs (configs\pdf, pdf.ttf)
$needed = @(
    @{ f = "eng.traineddata"; url = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata" },
    @{ f = "osd.traineddata"; url = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/osd.traineddata" },
    @{ f = "ara.traineddata"; url = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/ara.traineddata" },
    @{ f = "pdf.ttf";         url = "https://github.com/tesseract-ocr/tesseract/raw/main/tessdata/pdf.ttf" },
    @{ f = "configs\pdf";     url = "https://github.com/tesseract-ocr/tesseract/raw/main/tessdata/configs/pdf" },
    @{ f = "configs\hocr";    url = "https://github.com/tesseract-ocr/tesseract/raw/main/tessdata/configs/hocr" },
    @{ f = "configs\txt";     url = "https://github.com/tesseract-ocr/tesseract/raw/main/tessdata/configs/txt" }
)
foreach ($n in $needed) {
    $f = $n.f
    $dst = Join-Path $td $f
    if (Test-Path $dst) { continue }
    $src = Join-Path $sysTd $f
    if (Test-Path $src) { Copy-Item $src $dst; Ok "copied $f" }
    else {
        $url = $n.url
        try { Invoke-WebRequest -Uri $url -OutFile $dst -UseBasicParsing; Ok "downloaded $f" }
        catch { Warn "could not download $f ($url). Copy it into $td manually." }
    }
}

# ---------------------------------------------------------------- venv + deps
Step "Creating virtual environment (.venv) and installing packages"
if (-not (Test-Path ".venv\Scripts\python.exe")) { $exe = $py[0]; $pre = @($py | Select-Object -Skip 1); & $exe @pre -m venv .venv }
$vpy = Join-Path $App ".venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r requirements.txt --quiet
Ok "packages installed"

# ---------------------------------------------------------------- local config
Step "Creating config\local.env"
$envFile = Join-Path $App "config\local.env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $App "config\local.env.example") $envFile
    (Get-Content $envFile) `
        -replace '^TESSERACT_CMD=.*', "TESSERACT_CMD=$tessExe" `
        -replace '^TESSDATA_PREFIX=.*', "TESSDATA_PREFIX=$td" | Set-Content $envFile -Encoding UTF8
    Ok "created (edit later with windows\configure.ps1)"
} else { Ok "kept existing config\local.env" }

# ---------------------------------------------------------------- samples + tests
Step "Checking samples and running tests"
$env:TESSERACT_CMD = $tessExe
$env:TESSDATA_PREFIX = $td
$env:Path = (Split-Path $tessExe) + ";" + $env:Path
& $vpy samples\make_samples.py
$testsFailed = $false
if (-not $SkipTests) {
    & $vpy -m pytest -q
    if ($LASTEXITCODE -ne 0) { Warn "Some tests failed - see output above."; $testsFailed = $true } else { Ok "all tests passed" }
}

if (-not $NoShortcuts -and -not $env:CI) {
    Step "Creating shortcuts"
    & (Join-Path $PSScriptRoot "shortcuts.ps1")
}

Write-Host "`nInstall complete." -ForegroundColor Green
Write-Host "  Configure engines : powershell -ExecutionPolicy Bypass -File .\windows\configure.ps1"
Write-Host "  Start the service : 'OneOffice DocFlow' shortcut on the Desktop, or windows\start.bat"
Write-Host "  Get updates       : Start menu > OneOffice DocFlow > Update, or windows\update.bat"
if ($testsFailed -and $env:CI) { exit 1 }
exit 0
