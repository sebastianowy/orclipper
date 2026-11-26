#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/orclipper"
MANIFEST="${INSTALL_DIR}/.install-manifest"
SERVICE_FILE="/etc/systemd/system/orclipper.service"
REPO_URL="${ORCLIPPER_REPO_URL:-https://github.com/sebastianowy/orclipper.git}"
RAW_URL="${ORCLIPPER_RAW_URL:-https://raw.githubusercontent.com/sebastianowy/orclipper/refs/heads/main/run.sh}"
ORCA_APPIMAGE_URL="${ORCA_APPIMAGE_URL:-https://github.com/SoftFever/OrcaSlicer/releases/download/v2.3.0/OrcaSlicer_Linux_AppImage_V2.3.0.AppImage}"

if [[ "${EUID}" -eq 0 ]]; then
  LOG_FILE="/var/log/orclipper.log"
else
  LOG_FILE="/tmp/orclipper.log"
fi
exec > >(tee -a "$LOG_FILE") 2>&1

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] $*"; }
warn() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] $*"; }
err()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERR]  $*" >&2; }

ask() {
  local prompt="$1" default="$2" reply=""
  read -r -p "${prompt} [${default}]: " reply < /dev/tty || true
  echo "${reply:-$default}"
}

confirm() {
  local prompt="$1" reply=""
  read -r -p "${prompt} [y/N]: " reply < /dev/tty || true
  [[ "$reply" =~ ^[Yy]$ ]]
}

has_systemd() {
  [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1
}

clean_path() {
  local p="$1"
  p="${p%\"}"; p="${p#\"}"
  p="${p%\'}"; p="${p#\'}"
  echo "${p/#\~/$HOME}"
}

import_3mf_local() {
  local profiles_dir="./profiles"
  local extractor="./extract_profiles.py"

  if [[ -f "$MANIFEST" ]]; then
    # shellcheck disable=SC1090
    source "$MANIFEST"
    profiles_dir="${PROFILES_DIR:-${INSTALL_DIR}/profiles}"
    extractor="${INSTALL_DIR}/app/extract_profiles.py"
  fi

  if [[ ! -f "$extractor" ]]; then
    err "Extractor not found at ${extractor}. Install Orclipper first or run from repo directory."
    exit 1
  fi

  while true; do
    local threemf
    threemf="$(ask "Path to .3mf file to import (or Enter to finish)" "")"
    [[ -z "$threemf" ]] && break
    threemf="$(clean_path "$threemf")"
    if [[ ! -f "$threemf" ]]; then
      warn "File not found: ${threemf}"
      continue
    fi
    log "Extracting profiles from ${threemf} -> ${profiles_dir}"
    python3 "$extractor" "$threemf" "$profiles_dir"
    confirm "Import another .3mf file?" || break
  done
}

import_3mf_remote() {
  local ssh_user="$1" ssh_host="$2" ssh_port="$3"
  while true; do
    local threemf
    threemf="$(ask "Path to local .3mf file to import (or Enter to finish)" "")"
    [[ -z "$threemf" ]] && break
    threemf="$(clean_path "$threemf")"
    if [[ ! -f "$threemf" ]]; then
      warn "File not found: ${threemf}"
      continue
    fi
    log "Uploading & extracting ${threemf} on ${ssh_user}@${ssh_host}..."
    ssh -p "$ssh_port" "${ssh_user}@${ssh_host}" \
      "bash -c 'source /opt/orclipper/.install-manifest && python3 /opt/orclipper/app/extract_profiles.py - \"\$PROFILES_DIR\"'" < "$threemf"
    confirm "Import another .3mf file?" || break
  done
}

uninstall_local() {
  log "=== Starting Orclipper uninstallation ==="

  if has_systemd && systemctl list-unit-files | grep -q "^orclipper\.service"; then
    log "Stopping and disabling orclipper.service..."
    systemctl stop orclipper.service 2>/dev/null || true
    systemctl disable orclipper.service 2>/dev/null || true
  fi

  if [[ -f "$SERVICE_FILE" ]]; then
    rm -f "$SERVICE_FILE"
    has_systemd && systemctl daemon-reload || true
    log "Removed $SERVICE_FILE"
  fi

  if [[ -f "$MANIFEST" ]]; then
    # shellcheck disable=SC1090
    source "$MANIFEST"
    if [[ -n "${APT_INSTALLED:-}" ]]; then
      log "Purging only APT packages installed by Orclipper: ${APT_INSTALLED}"
      DEBIAN_FRONTEND=noninteractive apt-get purge -y ${APT_INSTALLED} || warn "Some packages could not be removed."
      DEBIAN_FRONTEND=noninteractive apt-get autoremove -y || true
    fi
  else
    warn "Manifest not found ($MANIFEST) — skipping APT purge."
  fi

  if [[ -d "$INSTALL_DIR" ]]; then
    if confirm "Remove ${INSTALL_DIR} completely (including profiles)?"; then
      rm -rf "$INSTALL_DIR"
      log "Removed $INSTALL_DIR"
    fi
  fi

  log "=== Uninstallation complete. Log: ${LOG_FILE} ==="
}

install_local() {
  log "=== Starting Orclipper installation ==="

  ARCH="$(uname -m)"
  RAM_MB="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
  FREE_MB="$(df -Pm /opt 2>/dev/null | awk 'NR==2 {print $4}')"
  log "System: arch=${ARCH}, RAM=${RAM_MB}MB, free in /opt=${FREE_MB}MB"

  if (( FREE_MB < 2048 )); then
    warn "Low disk space (${FREE_MB}MB). Recommended minimum is 2048MB."
    confirm "Continue anyway?" || exit 1
  fi

  if (( RAM_MB < 2048 )); then
    warn "Less than 2GB RAM detected (${RAM_MB}MB)."
    confirm "Continue anyway?" || exit 1
  fi

  CUSTOM_ORCA_BIN=""
  if [[ "$ARCH" != "x86_64" ]]; then
    warn "Detected architecture ${ARCH}. Official OrcaSlicer AppImage supports x86_64 only."
    CUSTOM_ORCA_BIN="$(ask "Path to existing orca-slicer binary (or Enter to abort)" "")"
    if [[ -z "$CUSTOM_ORCA_BIN" || ! -x "$CUSTOM_ORCA_BIN" ]]; then
      err "No compatible OrcaSlicer binary for ${ARCH}. Aborted with no system changes."
      exit 1
    fi
  fi

  DEFAULT_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
  SERVICE_USER="$(ask "System user for the service" "$DEFAULT_USER")"
  PORT="$(ask "Web UI port" "8080")"
  PROFILES_DIR="$(ask "Profiles directory" "${INSTALL_DIR}/profiles")"

  HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  DEFAULT_MOONRAKER=""
  if curl -sf --max-time 2 "http://127.0.0.1:7125/server/info" >/dev/null 2>&1; then
    DEFAULT_MOONRAKER="http://127.0.0.1:7125"
    log "Detected local Moonraker at ${DEFAULT_MOONRAKER}"
  fi

  DEFAULT_FLUIDD=""
  if [[ -n "$HOST_IP" ]]; then
    for fport in 80 10088 4408 81; do
      if curl -sf --max-time 2 "http://127.0.0.1:${fport}/" >/dev/null 2>&1; then
        [[ "$fport" == "80" ]] && DEFAULT_FLUIDD="http://${HOST_IP}" || DEFAULT_FLUIDD="http://${HOST_IP}:${fport}"
        log "Detected local Fluidd/Mainsail UI -> ${DEFAULT_FLUIDD}"
        break
      fi
    done
  fi

  MOONRAKER_URL="$(ask "Moonraker URL" "${DEFAULT_MOONRAKER:-http://127.0.0.1:7125}")"
  FLUIDD_PUBLIC_URL="$(ask "Fluidd/Mainsail public URL" "${DEFAULT_FLUIDD:-http://${HOST_IP:-127.0.0.1}}")"

  apt-get update -qq
  REQUIRED_PKGS=(git curl ca-certificates python3 python3-venv xvfb libgl1 libglib2.0-0 libgtk-3-0)
  for webkit in libwebkit2gtk-4.1-0 libwebkit2gtk-4.0-37; do
    if apt-cache show "$webkit" >/dev/null 2>&1; then
      REQUIRED_PKGS+=("$webkit")
      break
    fi
  done

  TO_INSTALL=()
  for pkg in "${REQUIRED_PKGS[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
      TO_INSTALL+=("$pkg")
    fi
  done

  PREV_APT=""
  if [[ -f "$MANIFEST" ]]; then
    PREV_APT="$(awk -F'"' '/^APT_INSTALLED=/{print $2}' "$MANIFEST" || true)"
  fi

  if [[ -d "$INSTALL_DIR" ]]; then
    log "Cleaning previous installation in ${INSTALL_DIR} (preserving profiles/)..."
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name 'profiles' -exec rm -rf {} +
  fi

  mkdir -p "$INSTALL_DIR" "$PROFILES_DIR"

  if (( ${#TO_INSTALL[@]} > 0 )); then
    log "Installing missing APT packages: ${TO_INSTALL[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${TO_INSTALL[@]}"
  fi

  ALL_APT="$(echo "${PREV_APT} ${TO_INSTALL[*]}" | xargs -n1 2>/dev/null | sort -u | xargs || true)"
  cat << MANIFEST_EOF > "$MANIFEST"
APT_INSTALLED="${ALL_APT}"
SERVICE_USER="${SERVICE_USER}"
PORT="${PORT}"
PROFILES_DIR="${PROFILES_DIR}"
MOONRAKER_URL="${MOONRAKER_URL}"
FLUIDD_PUBLIC_URL="${FLUIDD_PUBLIC_URL}"
INSTALLED_AT="$(date -Iseconds)"
MANIFEST_EOF

  if [[ -d "${INSTALL_DIR}/app/.git" ]]; then
    log "Updating repository in ${INSTALL_DIR}/app..."
    git -C "${INSTALL_DIR}/app" pull --ff-only
  else
    log "Cloning repository to ${INSTALL_DIR}/app..."
    git clone --depth 1 "$REPO_URL" "${INSTALL_DIR}/app"
  fi

  log "Creating Python virtual environment..."
  python3 -m venv "${INSTALL_DIR}/venv"
  "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
  if [[ -f "${INSTALL_DIR}/app/requirements.txt" ]]; then
    "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/app/requirements.txt" -q
  fi

  ORCA_BIN="$CUSTOM_ORCA_BIN"
  if [[ -z "$ORCA_BIN" ]]; then
    log "Downloading OrcaSlicer AppImage..."
    TMP_APPIMAGE="${INSTALL_DIR}/orca.AppImage"
    curl -fsSL "$ORCA_APPIMAGE_URL" -o "$TMP_APPIMAGE"
    chmod +x "$TMP_APPIMAGE"
    log "Extracting AppImage to ${INSTALL_DIR}/orcaslicer..."
    (cd "$INSTALL_DIR" && "$TMP_APPIMAGE" --appimage-extract >/dev/null)
    rm -rf "${INSTALL_DIR}/orcaslicer"
    mv "${INSTALL_DIR}/squashfs-root" "${INSTALL_DIR}/orcaslicer"
    rm -f "$TMP_APPIMAGE"
    ORCA_BIN="${INSTALL_DIR}/orcaslicer/bin/orca-slicer"
  fi

  cp -f "${INSTALL_DIR}/app/run.sh" "${INSTALL_DIR}/run.sh"
  chmod +x "${INSTALL_DIR}/run.sh"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR" "$PROFILES_DIR"

  cat << UNIT_EOF > "$SERVICE_FILE"
[Unit]
Description=Orclipper Web Slicer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}/app
Environment=PORT=${PORT}
Environment=PROFILES_DIR=${PROFILES_DIR}
Environment=ORCA_SLICER_BIN=${ORCA_BIN}
Environment=MOONRAKER_URL=${MOONRAKER_URL}
Environment=FLUIDD_PUBLIC_URL=${FLUIDD_PUBLIC_URL}
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn --app-dir ${INSTALL_DIR}/app app:app --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=5
Nice=19
IOSchedulingClass=idle
OOMScoreAdjust=500
CPUQuota=75%

[Install]
WantedBy=multi-user.target
UNIT_EOF

  if has_systemd; then
    systemctl daemon-reload
    if confirm "Enable and start orclipper.service now?"; then
      systemctl enable --now orclipper.service
      log "Service started on port ${PORT}."
    fi
  else
    warn "systemd is not running (container/WSL detected). Skipping service activation."
    log "Start manually with:"
    log "  PORT=${PORT} PROFILES_DIR=${PROFILES_DIR} ORCA_SLICER_BIN=${ORCA_BIN} MOONRAKER_URL=${MOONRAKER_URL} FLUIDD_PUBLIC_URL=${FLUIDD_PUBLIC_URL} ${INSTALL_DIR}/venv/bin/uvicorn --app-dir ${INSTALL_DIR}/app app:app --host 0.0.0.0 --port ${PORT}"
  fi

  log "=== Installation complete. Log: ${LOG_FILE} ==="
}

# --- Parsowanie argumentów i menu ---
MODE=""
ACTION=""
for arg in "$@"; do
  case "$arg" in
    --local)     MODE="local" ;;
    --remote)    MODE="remote" ;;
    --install)   ACTION="install" ;;
    --import)    ACTION="import" ;;
    --uninstall) ACTION="uninstall" ;;
  esac
done

if [[ -z "$ACTION" ]]; then
  echo "=== Orclipper Manager ==="
  echo "  1) Install / Update Orclipper"
  echo "  2) Import profiles from .3mf"
  echo "  3) Uninstall Orclipper"
  act_choice="$(ask "Select action" "1")"
  case "$act_choice" in
    2) ACTION="import" ;;
    3) ACTION="uninstall" ;;
    *) ACTION="install" ;;
  esac
fi

if [[ -z "$MODE" ]]; then
  echo "Target machine:"
  echo "  1) This machine (local)"
  echo "  2) Remote host via SSH (e.g. printer / mini-PC)"
  mode_choice="$(ask "Select target" "1")"
  [[ "$mode_choice" == "2" ]] && MODE="remote" || MODE="local"
fi

if [[ "$MODE" == "remote" ]]; then
  SSH_HOST="$(ask "Remote host IP or hostname" "")"
  [[ -z "$SSH_HOST" ]] && { err "Host cannot be empty."; exit 1; }
  SSH_USER="$(ask "SSH username" "mks")"
  SSH_PORT="$(ask "SSH port" "22")"

  case "$ACTION" in
    install)
      ssh -t -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" "curl -fsSL ${RAW_URL} | sudo bash -s -- --local --install"
      import_3mf_remote "$SSH_USER" "$SSH_HOST" "$SSH_PORT"
      ;;
    import)
      import_3mf_remote "$SSH_USER" "$SSH_HOST" "$SSH_PORT"
      ;;
    uninstall)
      ssh -t -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" "curl -fsSL ${RAW_URL} | sudo bash -s -- --local --uninstall"
      ;;
  esac
  exit 0
fi

# Tryb lokalny
if [[ "$ACTION" == "import" ]]; then
  import_3mf_local
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  log "Local ${ACTION} requires root — escalating with sudo..."
  exec sudo bash -c "curl -fsSL ${RAW_URL} | bash -s -- --local --${ACTION}"
fi

if [[ "$ACTION" == "uninstall" ]]; then
  uninstall_local
else
  install_local
  confirm "Import profiles from a local .3mf file now?" && import_3mf_local
fi
