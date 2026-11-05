<#
  OneOffice DocFlow - install or update from GitHub (Windows PowerShell 5.1+)

  First install on a new PC (paste in PowerShell):
    $env:DOCFLOW_REPO='OneOfficeautomation-Private-Limited/oneoffice-docauto'; irm "https://api.github.com/repos/$env:DOCFLOW_REPO/contents/windows/get-docflow.ps1" -Headers @{Accept='application/vnd.github.raw'} | iex

  Update an installed copy: double-click windows\update.bat (or the "Update OneOffice DocFlow"
  Start-menu shortcut, or Settings > Updates in the web UI).

  Settings come from environment variables (so the script also works through "irm | iex"):
    DOCFLOW_REPO          owner/repo                 (saved in config\local.env after the first run)
    DOCFLOW_REF           branch or tag, default main; "release" = latest GitHub release
    DOCFLOW_DIR           install folder, default %USERPROFILE%\OneOffice\DocFlow
    DOCFLOW_GITHUB_TOKEN  optional for the public repo; needed for a private copy, or when GitHub's
                          hourly limit for anonymous downloads is reached. Saved encrypted
                          for this Windows user only (DPAPI), so it is asked once.
    DOCFLOW_FORCE=1       reinstall even when already up to date
    DOCFLOW_NO_START=1    do not start DocFlow afterwards
    DOCFLOW_GITHUB_API    API base for GitHub Enterprise, e.g. https://github.mycompany.com/api/v3

  Your data is never overwritten: config\local.env, data\ (documents, flows, database),
  tessdata\ and .venv\ are kept. The previous code is backed up to backups\ before each update.
#>
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x faster without the progress bar

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    !!  $m" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  OneOffice DocFlow - install / update from GitHub" -ForegroundColor Magenta
Write-Host "  ------------------------------------------------" -ForegroundColor Magenta

# ------------------------------------------------------------------ install folder
$Dir = $env:DOCFLOW_DIR
if (-not $Dir -and $PSScriptRoot -and (Test-Path (Join-Path (Split-Path -Parent $PSScriptRoot) "docauto"))) {
    $Dir = Split-Path -Parent $PSScriptRoot          # running from <install>\windows
}
if (-not $Dir) { $Dir = Join-Path $env:USERPROFILE "OneOffice\DocFlow" }
$Dir = [IO.Path]::GetFullPath($Dir)
$EnvFile = Join-Path $Dir "config\local.env"
$VerFile = Join-Path $Dir ".docflow-version"
$IsNew = -not (Test-Path (Join-Path $Dir "docauto"))

function Read-LocalEnv($key) {
    if (Test-Path $EnvFile) {
        foreach ($l in Get-Content $EnvFile) { if ($l -match "^\s*$key=(.*)$") { return $matches[1].Trim() } }
    }
    return ""
}
function Save-LocalEnv($key, $value) {
    if (-not (Test-Path $EnvFile)) { return }
    $lines = @(Get-Content $EnvFile)
    $found = $false
    $out = foreach ($l in $lines) { if ($l -match "^\s*$key=") { $found = $true; "$key=$value" } else { $l } }
    if (-not $found) { $out = @($out) + "$key=$value" }
    Set-Content -Path $EnvFile -Value $out -Encoding UTF8
}

# ------------------------------------------------------------------ repo + ref
$Repo = $env:DOCFLOW_REPO
if (-not $Repo) { $Repo = Read-LocalEnv "DOCFLOW_REPO" }
if (-not $Repo) { $Repo = Read-Host "GitHub repository (owner/name, e.g. OneOfficeautomation-Private-Limited/oneoffice-docauto)" }
$Repo = $Repo.Trim().Trim("/") -replace '^https://github.com/', '' -replace '\.git$', ''
if ($Repo -notmatch '^[\w.-]+/[\w.-]+$') { throw "Not a GitHub repository name: '$Repo' (expected owner/name)" }
$Ref = $env:DOCFLOW_REF
if (-not $Ref) { $Ref = Read-LocalEnv "DOCFLOW_REF" }
if (-not $Ref) { $Ref = "main" }
Write-Host "  Repository : $Repo ($Ref)"
Write-Host "  Folder     : $Dir"

# ------------------------------------------------------------------ git working copy (developer PC)
if (Test-Path (Join-Path $Dir ".git")) {
    Step "This folder is a git working copy: pulling with git"
    if (Get-Command git -ErrorAction SilentlyContinue) {
        & git -C $Dir pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw "git pull failed. Commit or discard your local changes (GitHub Desktop), then try again." }
        Ok "pulled"
    } else {
        Warn "git is not installed. Use GitHub Desktop: Fetch origin, then Pull origin."
    }
    $UseGit = $true
} else { $UseGit = $false }

# ------------------------------------------------------------------ GitHub access
$TokenFile = Join-Path $env:LOCALAPPDATA "OneOffice\DocFlow\github-token.dat"
function Get-SavedToken {
    if ($env:DOCFLOW_GITHUB_TOKEN) { return $env:DOCFLOW_GITHUB_TOKEN }
    if (Test-Path $TokenFile) {
        try {
            $sec = Get-Content $TokenFile | ConvertTo-SecureString
            return [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        } catch { Remove-Item $TokenFile -Force }
    }
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        try { $t = (& gh auth token 2>$null); if ($LASTEXITCODE -eq 0 -and $t) { return "$t".Trim() } } catch {}
    }
    return ""
}
function Ask-Token {
    Write-Host ""
    Write-Host "  GitHub refused access to $Repo. A GitHub token fixes this (asked once per PC)." -ForegroundColor Yellow
    Write-Host "  Public repository: GitHub's hourly limit for anonymous downloads was probably reached"
    Write-Host "  on this network. Wait an hour, or use any token (Repository access: Public repositories)."
    Write-Host "  Private copy: the token needs Repository access: only $Repo, Contents = Read-only."
    Write-Host "  Create one: github.com > Settings > Developer settings > Fine-grained tokens > Generate"
    $sec = Read-Host "  Paste the token (or press Enter to try again without one)" -AsSecureString
    $t = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    return $t.Trim()
}
function Save-Token($t) {
    New-Item -ItemType Directory -Force -Path (Split-Path $TokenFile) | Out-Null
    ConvertTo-SecureString $t -AsPlainText -Force | ConvertFrom-SecureString | Set-Content $TokenFile
}
$Token = Get-SavedToken
$Api = if ($env:DOCFLOW_GITHUB_API) { $env:DOCFLOW_GITHUB_API.TrimEnd("/") } else { "https://api.github.com" }   # GitHub Enterprise: https://<host>/api/v3
function GH($path) {
    $h = @{ "User-Agent" = "oneoffice-docflow-updater"; Accept = "application/vnd.github+json" }
    if ($script:Token) { $h.Authorization = "Bearer $script:Token" }
    return Invoke-RestMethod -Uri "$Api/$path" -Headers $h
}
function GH-Try($path) {
    for ($i = 0; $i -lt 3; $i++) {
        try { return GH $path }
        catch {
            $code = 0; try { $code = [int]$_.Exception.Response.StatusCode } catch {}
            if ($code -in 401, 403, 404) {
                if ($i -eq 2) { throw "GitHub refused access to $Repo (HTTP $code). Check the repository name; for HTTP 403 wait an hour or use a token; a private copy needs a token with Contents: Read-only." }
                if (Test-Path $TokenFile) { Remove-Item $TokenFile -Force }
                $script:Token = Ask-Token
                continue
            }
            throw
        }
    }
}

if (-not $UseGit) {
    # -------------------------------------------------------------- which version
    Step "Checking the latest version on GitHub"
    $info = GH-Try "repos/$Repo"
    if ($Token -and -not $env:DOCFLOW_GITHUB_TOKEN -and -not (Test-Path $TokenFile)) { Save-Token $Token }
    if ($env:DOCFLOW_GITHUB_TOKEN -and $info.private) { Save-Token $env:DOCFLOW_GITHUB_TOKEN }
    if ($Ref -eq "release") {
        $rel = GH-Try "repos/$Repo/releases/latest"
        $target = $rel.tag_name
        $commit = (GH-Try "repos/$Repo/commits/$target").sha
        $label = "release $target"
    } else {
        $target = $Ref
        $c = GH-Try "repos/$Repo/commits/$Ref"
        $commit = $c.sha
        $label = "$Ref @ $($commit.Substring(0,7)) ($($c.commit.message.Split("`n")[0]))"
    }
    Ok "latest: $label"

    $installed = $null
    if (Test-Path $VerFile) { try { $installed = Get-Content $VerFile -Raw | ConvertFrom-Json } catch {} }
    if ($installed -and $installed.commit -eq $commit -and -not $env:DOCFLOW_FORCE) {
        Ok "already up to date ($($commit.Substring(0,7)))"
        $UpToDate = $true
    } else { $UpToDate = $false }
}

# ------------------------------------------------------------------ stop a running DocFlow
$Port = Read-LocalEnv "DOCAUTO_PORT"; if (-not $Port) { $Port = "8085" }
$WasRunning = $false
if (-not $UpToDate) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -match '^python') {
            Step "Stopping DocFlow (port $Port) for the update; it restarts afterwards"
            Stop-Process -Id $p.Id -Force
            Start-Sleep -Seconds 1
            $WasRunning = $true
        } elseif ($p) { Warn "Port $Port is used by $($p.ProcessName); not stopping it." }
    }
}

if (-not $UseGit -and -not $UpToDate) {
    # -------------------------------------------------------------- download
    Step "Downloading $label"
    $tmp = Join-Path $env:TEMP ("docflow-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $zip = Join-Path $tmp "src.zip"
    $h = @{ "User-Agent" = "oneoffice-docflow-updater" }
    if ($Token) { $h.Authorization = "Bearer $Token" }
    Invoke-WebRequest -Uri "$Api/repos/$Repo/zipball/$commit" -Headers $h -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp "x") -Force
    $src = Get-ChildItem (Join-Path $tmp "x") -Directory | Select-Object -First 1
    if (-not (Test-Path (Join-Path $src.FullName "docauto"))) { throw "Downloaded archive does not look like DocFlow (no docauto folder)." }
    Ok ("downloaded {0:N1} MB" -f ((Get-Item $zip).Length / 1MB))

    # -------------------------------------------------------------- backup
    if (-not $IsNew) {
        Step "Backing up the current version"
        $bdir = Join-Path $Dir "backups"
        New-Item -ItemType Directory -Force -Path $bdir | Out-Null
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $bzip = Join-Path $bdir "docflow-$stamp.zip"
        Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
        $za = [IO.Compression.ZipFile]::Open($bzip, [IO.Compression.ZipArchiveMode]::Create)
        try {
            $skip = @(".venv", "data", "tessdata", "backups", ".pytest_cache", ".git")
            foreach ($top in (Get-ChildItem $Dir -Force | Where-Object { $_.Name -notin $skip })) {
                $files = if ($top.PSIsContainer) { Get-ChildItem $top.FullName -Recurse -File -Force | Where-Object { $_.FullName -notmatch '__pycache__' } } else { @($top) }
                foreach ($f in $files) {
                    $rel = $f.FullName.Substring($Dir.Length).TrimStart('\', '/')
                    [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($za, $f.FullName, $rel)
                }
            }
        } finally { $za.Dispose() }
        $db = Join-Path $Dir "data\docauto.db"
        $vpyOld = Join-Path $Dir ".venv\Scripts\python.exe"
        if ((Test-Path (Join-Path $Dir "docauto\backup.py")) -and (Test-Path $vpyOld)) {
            # PostgreSQL (DocFlow 4): pg_dump of every tenant's schema
            Push-Location $Dir
            & $vpyOld -m docauto.backup (Join-Path $Dir "data") (Join-Path $bdir "docflow-db-$stamp.dump")
            if ($LASTEXITCODE -ne 0) { Warn "database backup failed - continuing with the update" }
            Pop-Location
        } elseif (Test-Path $db) { Copy-Item $db (Join-Path $bdir "docauto-$stamp.db") }
        Get-ChildItem $bdir -Filter "docflow-*.zip" | Sort-Object Name -Descending | Select-Object -Skip 5 | Remove-Item -Force
        Get-ChildItem $bdir -Filter "docauto-*.db" | Sort-Object Name -Descending | Select-Object -Skip 5 | Remove-Item -Force
        Get-ChildItem $bdir -Filter "docflow-db-*.dump" | Sort-Object Name -Descending | Select-Object -Skip 5 | Remove-Item -Force
        Ok "saved backups\docflow-$stamp.zip"
    }

    # -------------------------------------------------------------- copy (keeping local data)
    Step "Installing files"
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $s = $src.FullName
    # code folders are mirrored (removes files deleted upstream); they hold no user data
    foreach ($d in @("docauto", "options", "windows", "clients", "integration", "tests", ".github")) {
        if (Test-Path (Join-Path $s $d)) {
            robocopy (Join-Path $s $d) (Join-Path $Dir $d) /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "copy failed for $d (robocopy $LASTEXITCODE)" }
        }
    }
    # everything else is added/updated, never deleted; local files are excluded
    robocopy $s $Dir /E /XD (Join-Path $s "docauto") (Join-Path $s "options") (Join-Path $s "windows") (Join-Path $s "clients") `
        (Join-Path $s "integration") (Join-Path $s "tests") (Join-Path $s ".github") .venv data tessdata backups `
        /XF local.env /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copy failed (robocopy $LASTEXITCODE)" }
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Ok "files updated"
}

# ------------------------------------------------------------------ dependencies
Set-Location $Dir
$reqHash = (Get-FileHash (Join-Path $Dir "requirements.txt") -Algorithm SHA256).Hash
$prevHash = ""
if (Test-Path $VerFile) { try { $prevHash = (Get-Content $VerFile -Raw | ConvertFrom-Json).requirements } catch {} }
$needInstall = $IsNew -or -not (Test-Path (Join-Path $Dir ".venv\Scripts\python.exe")) -or -not (Test-Path $EnvFile)
if ($needInstall) {
    Step "First-time setup (Python, Tesseract, packages)"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir "windows\install.ps1")
    if ($LASTEXITCODE -ne 0) { throw "install.ps1 failed - see above." }
} elseif ($reqHash -ne $prevHash -and -not $UpToDate) {
    Step "Updating Python packages"
    & (Join-Path $Dir ".venv\Scripts\python.exe") -m pip install -r (Join-Path $Dir "requirements.txt") --quiet
    if ($LASTEXITCODE -ne 0) { throw "pip install failed - see above." }
    Ok "packages up to date"
}

# ------------------------------------------------------------------ remember source + version
Save-LocalEnv "DOCFLOW_REPO" $Repo
Save-LocalEnv "DOCFLOW_REF" $Ref
if ($UseGit) { try { $commit = (& git -C $Dir rev-parse HEAD 2>$null) } catch { $commit = "" }; $label = "git working copy" }
if (-not $UpToDate) {
    $ver = (Get-Content (Join-Path $Dir "VERSION") -ErrorAction SilentlyContinue | Select-Object -First 1)
    [ordered]@{ version = "$ver".Trim(); commit = "$commit".Trim(); ref = $Ref; repo = $Repo; source = $label
                installedAt = (Get-Date).ToString("s"); requirements = $reqHash } |
        ConvertTo-Json | Set-Content $VerFile -Encoding UTF8
}

$v = Get-Content $VerFile -Raw | ConvertFrom-Json
$short = "$($v.commit)"; if ($short.Length -gt 7) { $short = $short.Substring(0, 7) }
Write-Host ""
Write-Host "  OneOffice DocFlow $($v.version) ($short) is ready in $Dir" -ForegroundColor Green
if (($IsNew -or $WasRunning) -and -not $env:DOCFLOW_NO_START) {
    Write-Host "  Starting DocFlow..." -ForegroundColor Green
    Start-Process -FilePath (Join-Path $Dir "windows\start.bat") -WorkingDirectory $Dir
} else {
    Write-Host "  Start it with the 'OneOffice DocFlow' shortcut or windows\start.bat"
}
