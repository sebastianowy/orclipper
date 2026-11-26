$ErrorActionPreference = "Stop"

$RawUrl = if ($env:ORCLIPPER_RAW_URL) { $env:ORCLIPPER_RAW_URL } else { "https://raw.githubusercontent.com/sebastianowy/orclipper/refs/heads/main/run.sh" }

function Ask($Prompt, $Default = "") {
    $suffix = if ($Default) { " [$Default]" } else { "" }
    $reply = Read-Host "$Prompt$suffix"
    if ([string]::IsNullOrWhiteSpace($reply)) { return $Default }
    return $reply.Trim()
}

function Confirm-Yes($Prompt) {
    $reply = Read-Host "$Prompt [y/N]"
    return $reply -match '^[Yy]$'
}

function Clean-Path($Path) {
    $p = $Path.Trim().Trim('"').Trim("'")
    if ($p.StartsWith("~")) {
        $p = $HOME + $p.Substring(1)
    }
    return $p
}

function Import-3mfRemote($SshUser, $SshHost, $SshPort) {
    while ($true) {
        $threeMf = Ask "Path to local .3mf file to import (drag & drop or Enter to finish)" ""
        if ([string]::IsNullOrWhiteSpace($threeMf)) { break }
        $threeMf = Clean-Path $threeMf
        if (-not (Test-Path -LiteralPath $threeMf -PathType Leaf)) {
            Write-Warning "File not found: $threeMf"
            continue
        }

        $remoteTmp = "/tmp/orclipper_import_$([guid]::NewGuid().ToString('N')).3mf"
        Write-Host "[INFO] Uploading $threeMf to ${SshUser}@${SshHost}..." -ForegroundColor Cyan
        & scp -P $SshPort $threeMf "${SshUser}@${SshHost}:${remoteTmp}"
        if ($LASTEXITCODE -ne 0) {
            Write-Error "SCP upload failed."
            continue
        }

        Write-Host "[INFO] Extracting profiles on remote host..." -ForegroundColor Cyan
        $remoteCmd = "bash -c 'source /opt/orclipper/.install-manifest && python3 /opt/orclipper/app/extract_profiles.py $remoteTmp `$PROFILES_DIR; rc=`$?; rm -f $remoteTmp; exit `$rc'"
        & ssh -p $SshPort "${SshUser}@${SshHost}" $remoteCmd
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Profile extraction returned exit code $LASTEXITCODE."
        }

        if (-not (Confirm-Yes "Import another .3mf file?")) { break }
    }
}

# Sprawdzenie wbudowanego OpenSSH (Windows 10/11)
if (-not (Get-Command ssh -ErrorAction SilentlyContinue) -or -not (Get-Command scp -ErrorAction SilentlyContinue)) {
    Write-Host "[ERR] OpenSSH Client (ssh/scp) not found." -ForegroundColor Red
    Write-Host "Install it via: Settings -> Apps -> Optional features -> OpenSSH Client" -ForegroundColor Yellow
    return
}

Write-Host "=== Orclipper Remote Manager (Windows) ===" -ForegroundColor Cyan
Write-Host "  1) Install / Update Orclipper"
Write-Host "  2) Import profiles from .3mf"
Write-Host "  3) Uninstall Orclipper"

$actChoice = Ask "Select action" "1"
$action = switch ($actChoice) {
    "2" { "import" }
    "3" { "uninstall" }
    default { "install" }
}

$sshHost = Ask "Remote host IP or hostname" ""
if ([string]::IsNullOrWhiteSpace($sshHost)) {
    Write-Host "[ERR] Host cannot be empty." -ForegroundColor Red
    return
}
$sshUser = Ask "SSH username" "mks"
$sshPort = Ask "SSH port" "22"

switch ($action) {
    "install" {
        & ssh -t -p $sshPort "${sshUser}@${sshHost}" "curl -fsSL $RawUrl | sudo bash -s -- --local --install"
        if ($LASTEXITCODE -eq 0) {
            Import-3mfRemote $sshUser $sshHost $sshPort
        }
    }
    "import" {
        Import-3mfRemote $sshUser $sshHost $sshPort
    }
    "uninstall" {
        & ssh -t -p $sshPort "${sshUser}@${sshHost}" "curl -fsSL $RawUrl | sudo bash -s -- --local --uninstall"
    }
}
