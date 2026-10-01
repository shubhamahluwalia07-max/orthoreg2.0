<#
.SYNOPSIS
    Bootstraps OrthoReg 2.0 with an isolated, portable Python environment on Windows.
.DESCRIPTION
    1. Sets up target directory and clones the repository.
    2. Downloads and configures the Python 3.12 Windows Embeddable Package into .\python_local.
    3. Uncomments 'import site' and configures '..' in python*._pth to activate site-packages and local modules.
    4. Downloads get-pip.py and bootstraps pip using the local Python executable.
    5. Installs all dependencies from requirements.txt into the isolated site-packages.
    6. Generates start_orthoreg.bat to run app.py / runner.py using only .\python_local\python.exe.
#>

[CmdletBinding()]
param(
    [string]$TargetDir = "E:\Custom Apps\orthoreg",
    [string]$RepoUrl   = "https://github.com/shubhamahluwalia07-max/orthoreg2.0.git",
    [string]$PythonEmbedUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip",
    [string]$GetPipUrl      = "https://bootstrap.pypa.io/get-pip.py"
)

$ErrorActionPreference = "Stop"
# Disable download progress streams to maximize download throughput
$ProgressPreference = "SilentlyContinue"

Write-Host "==========================================================================" -ForegroundColor Cyan
Write-Host "    ORTHOPEDIC RADIOLOGY REGISTRY (ORTHOREG) - LOCAL SETUP BOOTSTRAPPER   " -ForegroundColor Cyan
Write-Host "==========================================================================" -ForegroundColor Cyan
Write-Host "Target Directory : $TargetDir" -ForegroundColor Yellow
Write-Host "Python Package   : Python 3.12.8 (Windows 64-bit Embeddable)" -ForegroundColor Yellow
Write-Host "Isolation Policy : 100% Local (No global changes, No PATH pollution)" -ForegroundColor Yellow
Write-Host "--------------------------------------------------------------------------"

# Ensure target directory exists
if (-not (Test-Path $TargetDir)) {
    Write-Host "[1/5] Creating directory $TargetDir..." -ForegroundColor Cyan
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
} else {
    Write-Host "[1/5] Target directory verified: $TargetDir" -ForegroundColor Cyan
}

# -----------------------------------------------------------------------------
# 1. Directory Setup & Clone
# -----------------------------------------------------------------------------
Write-Host "`n[Step 1/5] Cloning repository into $TargetDir..." -ForegroundColor Green
$hasRepo = (Test-Path (Join-Path $TargetDir "app.py")) -and (Test-Path (Join-Path $TargetDir "requirements.txt"))

if (-not $hasRepo) {
    $existingGit = Get-Command "git" -ErrorAction SilentlyContinue
    if ($existingGit) {
        Write-Host "Detected Git: $($existingGit.Source)"
        $existingItems = Get-ChildItem -Path $TargetDir -Force
        if ($existingItems.Count -eq 0) {
            & git clone $RepoUrl $TargetDir
        } else {
            $stagingDir = Join-Path $env:TEMP "orthoreg_clone_$(Get-Random)"
            & git clone $RepoUrl $stagingDir
            Copy-Item -Path "$stagingDir\*" -Destination $TargetDir -Recurse -Force
            if (Test-Path "$stagingDir\.git") {
                Copy-Item -Path "$stagingDir\.git" -Destination $TargetDir -Recurse -Force
            }
            Remove-Item -Path $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host "No global Git installation detected. Using portable MinGit staging..." -ForegroundColor Yellow
        $minGitZip = Join-Path $env:TEMP "mingit_portable_$(Get-Random).zip"
        $minGitDir = Join-Path $env:TEMP "mingit_portable_$(Get-Random)"
        
        $cloneSucceeded = $false
        try {
            $minGitUrl = "https://github.com/git-for-windows/git/releases/download/v2.48.1.windows.1/MinGit-2.48.1-64-bit.zip"
            Write-Host "Downloading portable MinGit..."
            Invoke-WebRequest -Uri $minGitUrl -OutFile $minGitZip
            
            Write-Host "Extracting MinGit..."
            Expand-Archive -Path $minGitZip -DestinationPath $minGitDir -Force
            
            $portableGitExe = Join-Path $minGitDir "cmd\git.exe"
            Write-Host "Cloning $RepoUrl into $TargetDir..."
            
            $existingItems = Get-ChildItem -Path $TargetDir -Force
            if ($existingItems.Count -eq 0) {
                & $portableGitExe clone $RepoUrl $TargetDir
            } else {
                $stagingDir = Join-Path $env:TEMP "orthoreg_clone_$(Get-Random)"
                & $portableGitExe clone $RepoUrl $stagingDir
                Copy-Item -Path "$stagingDir\*" -Destination $TargetDir -Recurse -Force
                if (Test-Path "$stagingDir\.git") {
                    Copy-Item -Path "$stagingDir\.git" -Destination $TargetDir -Recurse -Force
                }
                Remove-Item -Path $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
            }
            $cloneSucceeded = $true
            Write-Host "Git clone completed successfully via portable Git." -ForegroundColor Green
        } catch {
            Write-Warning "MinGit cloning encountered an error: $_. Falling back to direct GitHub repository archive..."
        } finally {
            if (Test-Path $minGitZip) { Remove-Item -Path $minGitZip -Force -ErrorAction SilentlyContinue }
            if (Test-Path $minGitDir) { Remove-Item -Path $minGitDir -Recurse -Force -ErrorAction SilentlyContinue }
        }

        if (-not $cloneSucceeded) {
            Write-Host "Downloading repository zip archive from GitHub..." -ForegroundColor Yellow
            $repoZipUrl = "https://github.com/shubhamahluwalia07-max/orthoreg2.0/archive/refs/heads/main.zip"
            $tempRepoZip = Join-Path $env:TEMP "orthoreg_repo_$(Get-Random).zip"
            $tempExtractDir = Join-Path $env:TEMP "orthoreg_extract_$(Get-Random)"
            
            Invoke-WebRequest -Uri $repoZipUrl -OutFile $tempRepoZip
            Expand-Archive -Path $tempRepoZip -DestinationPath $tempExtractDir -Force
            
            $extractedRoot = Get-ChildItem -Path $tempExtractDir | Where-Object { $_.PSIsContainer } | Select-Object -First 1
            if ($extractedRoot) {
                Copy-Item -Path "$($extractedRoot.FullName)\*" -Destination $TargetDir -Recurse -Force
                Write-Host "Repository files extracted successfully into $TargetDir." -ForegroundColor Green
            } else {
                throw "Failed to extract repository archive contents."
            }
            Remove-Item -Path $tempRepoZip -Force -ErrorAction SilentlyContinue
            Remove-Item -Path $tempExtractDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
} else {
    Write-Host "Repository files already exist in $TargetDir. Skipping clone step." -ForegroundColor Yellow
}

# -----------------------------------------------------------------------------
# 2. Portable Python Environment
# -----------------------------------------------------------------------------
Write-Host "`n[Step 2/5] Setting up Portable Python Environment in .\python_local..." -ForegroundColor Green
$pythonLocal = Join-Path $TargetDir "python_local"
if (-not (Test-Path $pythonLocal)) {
    New-Item -ItemType Directory -Path $pythonLocal -Force | Out-Null
}

$pythonExe = Join-Path $pythonLocal "python.exe"
if (-not (Test-Path $pythonExe)) {
    $tempPyZip = Join-Path $env:TEMP "python_embed_$(Get-Random).zip"
    Write-Host "Downloading Python embeddable package from $PythonEmbedUrl..."
    Invoke-WebRequest -Uri $PythonEmbedUrl -OutFile $tempPyZip
    
    Write-Host "Extracting Python package to $pythonLocal..."
    Expand-Archive -Path $tempPyZip -DestinationPath $pythonLocal -Force
    Remove-Item -Path $tempPyZip -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "Python binary already present at $pythonExe."
}

# Programmatically modify the python*._pth file inside the extracted folder to uncomment 'import site'
Write-Host "Modifying ._pth file to enable site-packages support ('import site') and local root..."
$pthFiles = Get-ChildItem -Path $pythonLocal -Filter "*._pth"
if ($pthFiles.Count -eq 0) {
    throw "No ._pth file found in $pythonLocal! Cannot configure isolated Python."
}

foreach ($pthFile in $pthFiles) {
    Write-Host "Processing $($pthFile.FullName)..."
    $lines = Get-Content -Path $pthFile.FullName
    $newLines = @()
    foreach ($line in $lines) {
        if ($line.Trim() -eq "#import site") {
            $newLines += "import site"
        } else {
            $newLines += $line
        }
    }
    
    if (-not ($newLines -contains "import site")) {
        $newLines += "import site"
    }
    
    # Ensure current directory '.' and parent directory '..' are included
    if (-not ($newLines -contains ".")) {
        $newLines += "."
    }
    if (-not ($newLines -contains "..")) {
        $newLines += ".."
    }
    
    Set-Content -Path $pthFile.FullName -Value $newLines -Encoding Ascii
    Write-Host "Successfully configured 'import site' and local paths in $($pthFile.Name)." -ForegroundColor Green
}

# -----------------------------------------------------------------------------
# 3. Pip Bootstrap
# -----------------------------------------------------------------------------
Write-Host "`n[Step 3/5] Bootstrapping pip with isolated Python engine..." -ForegroundColor Green
$pipInstalled = Test-Path (Join-Path $pythonLocal "Lib\site-packages\pip")
if (-not $pipInstalled) {
    $getPipScript = Join-Path $pythonLocal "get-pip.py"
    Write-Host "Downloading get-pip.py from $GetPipUrl..."
    Invoke-WebRequest -Uri $GetPipUrl -OutFile $getPipScript

    Write-Host "Running get-pip.py via $pythonExe..."
    & $pythonExe $getPipScript --no-warn-script-location

    Remove-Item -Path $getPipScript -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "Pip is already bootstrapped in localized environment."
}

Write-Host "Validating pip installation..."
& $pythonExe -m pip --version

# -----------------------------------------------------------------------------
# 4. Dependency Installation
# -----------------------------------------------------------------------------
Write-Host "`n[Step 4/5] Installing dependencies from requirements.txt into local site-packages..." -ForegroundColor Green
$reqFile = Join-Path $TargetDir "requirements.txt"
if (-not (Test-Path $reqFile)) {
    throw "Missing requirements.txt in $TargetDir!"
}

Write-Host "Executing: $pythonExe -m pip install -r requirements.txt"
& $pythonExe -m pip install --no-warn-script-location -r $reqFile

Write-Host "Dependency installation completed successfully." -ForegroundColor Green

# -----------------------------------------------------------------------------
# 5. Launch Script Generation
# -----------------------------------------------------------------------------
Write-Host "`n[Step 5/5] Generating launch script start_orthoreg.bat..." -ForegroundColor Green
$batPath = Join-Path $TargetDir "start_orthoreg.bat"

$batContent = @'
@echo off
title Orthopedic Radiology Registry (ORTHOREG)
cd /d "%~dp0"

echo =========================================================================
echo    ORTHOPEDIC RADIOLOGY REGISTRY - LAUNCHER
echo =========================================================================

if not defined NGROK_AUTHTOKEN (
    set "NGROK_AUTHTOKEN=3JzZPYwP2A7gb9Q6xyBGbNtsDBP_2fpuxh8kAvYS4rok9ke7D"
)

REM Priority 1: Check for isolated portable environment in python_local
if exist "python_local\python.exe" (
    echo [INFO] Detected isolated Python environment at .\python_local\python.exe
    if "%~1"=="app" (
        echo [INFO] Launching Flask application directly: app.py
        .\python_local\python.exe app.py
    ) else if exist "runner.py" (
        echo [INFO] Starting OrthoReg application and pyngrok tunnel: runner.py
        .\python_local\python.exe runner.py
    ) else (
        echo [INFO] Starting OrthoReg application: app.py
        .\python_local\python.exe app.py
    )
    goto :check_exit
)

REM Priority 2: Check for virtual environment venv
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
    python runner.py %*
    goto :check_exit
)

echo [ERROR] Neither .\python_local\python.exe nor .\venv was found!
echo Please run setup_orthoreg.ps1 to configure the isolated environment.

:check_exit
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ALERT] Application stopped with exit code %ERRORLEVEL%.
    pause
)
'@

Set-Content -Path $batPath -Value $batContent -Encoding Ascii
Write-Host "Generated launch script at: $batPath" -ForegroundColor Green
# Also patch run.bat if present to use isolated python_local
$repoRunBat = Join-Path $TargetDir "run.bat"
if (Test-Path $repoRunBat) {
    Copy-Item -Path $batPath -Destination $repoRunBat -Force
    Write-Host "Updated run.bat to use isolated Python engine." -ForegroundColor Green
}

# Copy this setup script into TargetDir for future portability
$inRepoScript = Join-Path $TargetDir "setup_orthoreg.ps1"
if ($MyInvocation.MyCommand.Path -and (Test-Path $MyInvocation.MyCommand.Path)) {
    if ($MyInvocation.MyCommand.Path -ne $inRepoScript) {
        Copy-Item -Path $MyInvocation.MyCommand.Path -Destination $inRepoScript -Force
    }
}

# -----------------------------------------------------------------------------
# Verification & Smoke Test
# -----------------------------------------------------------------------------
Write-Host "`n==========================================================================" -ForegroundColor Cyan
Write-Host "    SYSTEM VALIDATION & SMOKE TEST                                        " -ForegroundColor Cyan
Write-Host "==========================================================================" -ForegroundColor Cyan

Push-Location $TargetDir
try {
    Write-Host "1. Testing Python & core library imports..." -ForegroundColor Cyan
    & $pythonExe -c "import flask, sqlalchemy, pydicom, pyngrok, pandas, openpyxl, cryptography; print('[PASS] Core dependencies imported successfully!')"

    Write-Host "2. Testing application and database initialization..." -ForegroundColor Cyan
    & $pythonExe -c "from app import app, db; print('[PASS] OrthoReg app initialized successfully in local context:', app.name)"
} finally {
    Pop-Location
}

Write-Host "`n==========================================================================" -ForegroundColor Green
Write-Host "    BOOTSTRAP COMPLETE - ORTHOREG 2.0 READY                               " -ForegroundColor Green
Write-Host "==========================================================================" -ForegroundColor Green
Write-Host "To start the application, execute:" -ForegroundColor Yellow
Write-Host "    $batPath" -ForegroundColor White
Write-Host "Or run from PowerShell:" -ForegroundColor Yellow
Write-Host "    cd '$TargetDir'; .\start_orthoreg.bat" -ForegroundColor White


