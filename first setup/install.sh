#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
#  Void OS installer — Linux (Debian/Ubuntu/Raspberry Pi OS, Fedora/RHEL,
#  Arch, openSUSE, Alpine) and macOS.        Windows: use install.ps1
#
#  ONE FILE does everything: downloads Void OS, installs it and sets it up.
#
#    curl -fsSL https://raw.githubusercontent.com/Ishaan1313/voidos/main/first%20setup/install.sh | bash
#    curl -fsSL .../install.sh | bash -s -- --yes --port 8080     options after "--"
#
#  Or from a downloaded copy of the repository:
#    bash "first setup/install.sh"           interactive
#    bash "first setup/install.sh" --yes     accept all defaults (unattended)
#    bash "first setup/install.sh" --help    every option
#
#  Run from the web it first downloads the newest release (the main branch when
#  there is none yet) into ~/voidos (VOIDOS_DIR=/opt/voidos picks another folder).
#  Safe to re-run: it updates the Python packages, keeps your .env and data,
#  and refreshes the service. On Linux it needs administrator rights (it
#  installs packages and a system service): it re-runs itself with sudo, or
#  start it that way:  sudo bash "first setup/install.sh". Void OS itself still runs as the
#  account that started the installer, not as root.
# ══════════════════════════════════════════════════════════════════════
set -Eeuo pipefail

# ── step 0: get Void OS onto this machine (only when run from the web) ──
# Piped from curl there is no file next to this script, so download first, then run the downloaded
# copy of this same file. Started from inside a Void OS folder this does nothing.
_SELF="${BASH_SOURCE[0]:-}"
_HERE=""
[[ -n $_SELF && -f $_SELF ]] && _HERE="$(cd "$(dirname "$_SELF")" && pwd)"
if [[ -z $_HERE || ! -f "$_HERE/../voidos.py" ]]; then
  _REPO="Ishaan1313/voidos"
  _die() { printf '\n  ✗ %s\n\n' "$*" >&2; exit 1; }
  command -v tar >/dev/null 2>&1 || _die "tar is needed to unpack Void OS. Install it with your package manager and run this again."
  if   command -v curl >/dev/null 2>&1; then _fetch() { curl -fsSL "$1"; };  _fetch_to() { curl -fsSL -o "$2" "$1"; }
  elif command -v wget >/dev/null 2>&1; then _fetch() { wget -qO- "$1"; };   _fetch_to() { wget -qO "$2" "$1"; }
  else _die "curl (or wget) is needed to download Void OS. Install it and run this again."; fi
  # Started with sudo: install for the person who typed it, not for root.
  _OWNER=""
  if [[ ${EUID:-$(id -u)} == 0 && -n ${SUDO_USER:-} && $SUDO_USER != root ]]; then
    _OWNER="$SUDO_USER"
    _H="$(getent passwd "$_OWNER" 2>/dev/null | cut -d: -f6 || true)"; _H="${_H:-/home/$_OWNER}"
  else
    _H="$HOME"
  fi
  _DIR="${VOIDOS_DIR:-$_H/voidos}"
  _TAG="$(_fetch "https://api.github.com/repos/$_REPO/releases/latest" 2>/dev/null \
          | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1 || true)"
  if [[ -n $_TAG && $_TAG =~ ^[A-Za-z0-9._-]+$ ]]; then
    _URL="https://github.com/$_REPO/archive/refs/tags/$_TAG.tar.gz"; _WHAT="release $_TAG"
  else
    _URL="https://github.com/$_REPO/archive/refs/heads/main.tar.gz"; _WHAT="the main branch"
  fi
  printf '\n  Downloading Void OS (%s)…\n' "$_WHAT"
  _TMP="$(mktemp -d)"; trap 'rm -rf "$_TMP"' EXIT
  _fetch_to "$_URL" "$_TMP/voidos.tar.gz" || _die "Couldn't download $_URL. Check your internet connection."
  mkdir -p "$_DIR"
  if [[ -f "$_DIR/voidos.py" ]]; then printf '  Updating the existing install in %s (your .env and data are kept)…\n' "$_DIR"
  else printf '  Installing into %s …\n' "$_DIR"; fi
  tar -xzf "$_TMP/voidos.tar.gz" -C "$_DIR" --strip-components=1 || _die "The download couldn't be unpacked."
  [[ -f "$_DIR/first setup/install.sh" && -f "$_DIR/voidos.py" ]] || _die "The download doesn't look like Void OS (first setup/install.sh is missing)."
  [[ -n $_OWNER ]] && chown -R "$_OWNER" "$_DIR" 2>/dev/null || true
  trap - EXIT; rm -rf "$_TMP"
  # Piped from curl, stdin is this script: hand the keyboard back for the installer's questions.
  if [[ ! -t 0 ]] && ( : </dev/tty ) 2>/dev/null; then exec bash "$_DIR/first setup/install.sh" "$@" </dev/tty; fi
  [[ -t 0 ]] || set -- --yes "$@"      # no terminal to ask on (unattended): take the defaults
  exec bash "$_DIR/first setup/install.sh" "$@"
fi

SETUP_DIR="$_HERE"                                               # <app>/first setup
VOIDOS_DIR="$(cd "$SETUP_DIR/.." && pwd)"                        # <app>
LOG="$VOIDOS_DIR/install.log"
VENV="$VOIDOS_DIR/.venv"
TOOLS="$VOIDOS_DIR/.tools"
HELPER="$SETUP_DIR/helper.py"
SERVICE=voidos
PY_WANT="3.12"            # version fetched when the system has nothing new enough
MIN_PY_TEXT="3.10"

# ── administrator rights (Linux) ──────────────────────────────────────
# The installer installs system packages and a system service, so on Linux it must run as root.
# Started as a normal user, it re-runs itself through sudo (which asks for the password once).
ORIG_ARGS=("$@")
SELF="${BASH_SOURCE[0]:-$0}"
if [[ "$(uname -s)" == Linux && $EUID != 0 ]]; then
  case " ${ORIG_ARGS[*]-} " in
    *" -h "*|*" --help "*) ;;                      # help needs nothing
    *)
      if command -v sudo >/dev/null 2>&1; then
        printf '\n  Void OS needs administrator rights to install packages and its service.\n'
        printf '  Running again with sudo (it asks for your password once)...\n\n'
        exec sudo -E bash "$SELF" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
      fi
      printf '\n  This installer needs administrator rights, and sudo isn'"'"'t installed.\n'
      printf '  Run it as root instead:   su -c '"'"'bash "%s"'"'"'\n\n' "$SELF"
      exit 1 ;;
  esac
fi

# ── options ───────────────────────────────────────────────────────────
ASSUME_YES=0; NO_SERVICE=0; NO_START=0; UNINSTALL=0; PRIVATE_PY=0
OPT_PORT=""; OPT_ROOT=""; WITH=""; WITHOUT=""
usage() {
  cat <<EOF
Void OS installer

  bash "first setup/install.sh" [options]

  -y, --yes              Don't ask questions; use defaults (or the options below)
      --port N           Web port (default 7777)
      --file-root DIR    Folder shown in the Files tab (default ~/voidos-files)
      --with LIST        Also install these features (comma-separated)
      --without LIST     Skip these features
      --private-python   Use a private Python $PY_WANT in .tools/ even if the
                         system has a suitable one (fully isolated install)
      --no-service       Don't set up start-on-boot
      --no-start         Don't start Void OS at the end
      --uninstall        Remove the service, the 'voidos' command and the Python
                         environment (keeps .env, your files and all Void OS data)
  -h, --help             This help

  Everything Void OS can use is installed by default, so nothing is missing later:
  Features: docker   (App Store & Containers)        default: on
            storage  (USB drives, SMB/NFS shares)     default: on
            tunnel   (cloudflared + Node for Cloudflare remote access;
                      installing it does not switch the tunnel on)
                                                      default: on
            sync     (rclone continuous cloud sync)   default: on
            intercom (Bluetooth + PipeWire audio)     default: on
            office   (LibreOffice, for pixel-exact PowerPoint/Word/
                      Excel previews; they already preview
                      in the browser without it)         default: off
            mount    (let Void OS mount network shares without a
                      password — adds a sudo rule)    default: off

  After installing, control it with:  voidos start | stop | restart | status | logs
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --port) OPT_PORT="${2:?--port needs a number}"; shift ;;
    --file-root) OPT_ROOT="${2:?--file-root needs a folder}"; shift ;;
    --with) WITH="${2:?--with needs a list}"; shift ;;
    --without) WITHOUT="${2:?--without needs a list}"; shift ;;
    --no-service) NO_SERVICE=1 ;;
    --private-python) PRIVATE_PY=1 ;;
    --no-start) NO_START=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

# ── look ──────────────────────────────────────────────────────────────
UTF8=0; [[ "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" =~ [Uu][Tt][Ff]-?8 ]] && UTF8=1
if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-}" != "dumb" ]]; then
  TTY=1
  B=$'\e[1m'; D=$'\e[2m'; R=$'\e[0m'
  P=$'\e[38;5;141m'; P2=$'\e[38;5;99m'; G=$'\e[38;5;114m'; Y=$'\e[38;5;221m'; RD=$'\e[38;5;203m'; C=$'\e[38;5;117m'
else
  TTY=0; B=""; D=""; R=""; P=""; P2=""; G=""; Y=""; RD=""; C=""
fi
if [[ $UTF8 == 1 ]]; then OK="✓"; NO="✗"; WARN="!"; DOT="•"; else OK="ok"; NO="x"; WARN="!"; DOT="*"; fi
COLS=$( (tput cols 2>/dev/null || echo 72) | tail -1 ); (( COLS > 78 )) && COLS=78

banner() {
  printf '\n'
  if [[ $UTF8 == 1 ]]; then
    local shades=(93 99 105 141 147 183) i=0 line
    while IFS= read -r line; do
      if [[ $TTY == 1 ]]; then printf '  \e[38;5;%sm%s\e[0m\n' "${shades[i]}" "$line"; else printf '  %s\n' "$line"; fi
      i=$((i+1))
    done <<'ART'
██╗   ██╗ ██████╗ ██╗██████╗      ██████╗ ███████╗
██║   ██║██╔═══██╗██║██╔══██╗    ██╔═══██╗██╔════╝
██║   ██║██║   ██║██║██║  ██║    ██║   ██║███████╗
╚██╗ ██╔╝██║   ██║██║██║  ██║    ██║   ██║╚════██║
 ╚████╔╝ ╚██████╔╝██║██████╔╝    ╚██████╔╝███████║
  ╚═══╝   ╚═════╝ ╚═╝╚═════╝      ╚═════╝ ╚══════╝
ART
  else
    printf '  %s\n' "${P}${B}V O I D   O S${R}"
  fi
  printf '  %s\n\n' "${D}Self-hosted home server  ${DOT}  installer${R}"
}
STEP=0; STEPS=7
step()  { STEP=$((STEP+1)); printf '\n%s\n' "${P}${B}[$STEP/$STEPS]${R} ${B}$*${R}"; echo "=== [$STEP/$STEPS] $*" >>"$LOG"; }
ok()    { printf '  %s %s\n' "${G}${OK}${R}" "$*"; echo "  ok: $*" >>"$LOG"; }
info()  { printf '  %s %s\n' "${D}${DOT}${R}" "$*"; echo "  info: $*" >>"$LOG"; }
warn()  { printf '  %s %s\n' "${Y}${WARN}${R}" "$*"; echo "  WARN: $*" >>"$LOG"; WARNINGS+=("$*"); }
fail()  { printf '\n  %s %s\n' "${RD}${NO}${R}" "${B}$*${R}" >&2; echo "  FAIL: $*" >>"$LOG"; exit 1; }
WARNINGS=()

# Runs a command with a spinner; output goes to install.log, and the last
# lines are shown only if it fails.
run() {
  local msg=$1; shift
  printf '\n$ %s\n' "$*" >>"$LOG"
  local rc=0
  if [[ $TTY == 1 ]]; then
    ( "$@" ) >>"$LOG" 2>&1 &
    local pid=$! i=0
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    [[ $UTF8 == 1 ]] || frames=('-' '\' '|' '/')
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r  %s %s' "${P}${frames[i % ${#frames[@]}]}${R}" "$msg"
      i=$((i+1)); sleep 0.1
    done
    if wait "$pid"; then rc=0; else rc=$?; fi
    printf '\r\e[K'
  else
    if ( "$@" ) >>"$LOG" 2>&1; then rc=0; else rc=$?; fi
  fi
  if [[ $rc == 0 ]]; then ok "$msg"; else
    printf '  %s %s\n' "${RD}${NO}${R}" "$msg ${D}(exit $rc)${R}"
    printf '%s\n' "${D}$(tail -n 12 "$LOG" | sed 's/^/      /')${R}"
  fi
  return $rc
}

ask_yn() { # ask_yn "Question" default(y/n) → 0 for yes
  local q=$1 def=$2 a
  if [[ $ASSUME_YES == 1 || ! -t 0 ]]; then [[ $def == y ]]; return; fi
  local hint="[y/N]"; [[ $def == y ]] && hint="[Y/n]"
  printf '  %s %s %s ' "${C}?${R}" "$q" "${D}$hint${R}"
  read -r a || a=""
  a=${a:-$def}; [[ $a =~ ^[Yy] ]]
}
ask_val() { # ask_val "Question" default → echoes the answer
  local q=$1 def=$2 a
  if [[ $ASSUME_YES == 1 || ! -t 0 ]]; then echo "$def"; return; fi
  printf '  %s %s %s ' "${C}?${R}" "$q" "${D}[$def]${R}" >&2
  read -r a || a=""
  echo "${a:-$def}"
}

trap 'rc=$?; [[ $rc != 0 ]] && printf "\n  %s\n  %s\n\n" "${RD}Installation stopped (exit $rc).${R}" "${D}Full details: $LOG${R}" >&2' EXIT
trap 'printf "\n"; exit 130' INT

# ── privileges ───────────────────────────────────────────────────────
# Linux always arrives here as root (see above); Void OS runs as whoever started the installer.
if [[ $EUID == 0 ]]; then
  SUDO=""
  RUN_USER="${SUDO_USER:-root}"
else
  SUDO="sudo"
  RUN_USER="$(id -un)"
fi
RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6 || true)"; RUN_HOME="${RUN_HOME:-$HOME}"
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"
as_root() { if [[ -z $SUDO ]]; then "$@"; else sudo "$@"; fi; }
as_user() {  # files Void OS writes must belong to the account it runs as
  if [[ $EUID == 0 && $RUN_USER != root ]]; then sudo -u "$RUN_USER" -H "$@"; else "$@"; fi
}
HAVE_ROOT=1
need_root() {
  [[ -z $SUDO ]] && return 0
  if ! command -v sudo >/dev/null; then HAVE_ROOT=0; return 1; fi
  if sudo -n true 2>/dev/null; then return 0; fi
  if [[ ! -t 0 ]]; then HAVE_ROOT=0; return 1; fi
  info "Some steps need administrator rights — sudo will ask for ${B}your${R} password."
  sudo -v || { HAVE_ROOT=0; return 1; }
  ( while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done ) 2>/dev/null &
}

# ── system detection ─────────────────────────────────────────────────
OS="$(uname -s)"; ARCH="$(uname -m)"
DISTRO="unknown"; DISTRO_NAME="$OS"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO="${ID:-unknown}"; DISTRO_NAME="${PRETTY_NAME:-$DISTRO}"
elif [[ $OS == Darwin ]]; then
  DISTRO="macos"; DISTRO_NAME="macOS $(sw_vers -productVersion 2>/dev/null)"
fi
PM=""
for c in apt-get dnf yum pacman zypper apk brew; do command -v "$c" >/dev/null 2>&1 && { PM=$c; break; }; done
HAS_SYSTEMD=0; [[ -d /run/systemd/system ]] && HAS_SYSTEMD=1
IS_PI=0; grep -qi "raspberry" /proc/device-tree/model 2>/dev/null && IS_PI=1
IS_WSL=0; grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1
case "$ARCH" in
  x86_64|amd64) CF_ARCH=amd64 ;; aarch64|arm64) CF_ARCH=arm64 ;; armv7l|armv6l|armhf) CF_ARCH=arm ;; i386|i686) CF_ARCH=386 ;; *) CF_ARCH="" ;;
esac

APT_UPDATED=0
pkg_install() { # pkg_install pkg... — installs what it can, returns 1 if any were unavailable
  [[ $HAVE_ROOT == 1 ]] || return 1
  case "$PM" in
    apt-get)
      if [[ $APT_UPDATED == 0 ]]; then as_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq || true; APT_UPDATED=1; fi
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" ;;
    dnf)    as_root dnf install -y -q "$@" ;;
    yum)    as_root yum install -y -q "$@" ;;
    pacman) as_root pacman -S --noconfirm --needed "$@" ;;
    zypper) as_root zypper --non-interactive install --no-recommends "$@" ;;
    apk)    as_root apk add --no-cache "$@" ;;
    brew)   as_user brew install "$@" ;;
    *) return 1 ;;
  esac
}
# Package names differ per distro: pkgs_for <feature>
pkgs_for() {
  case "$1:$PM" in
    python:apt-get) echo "python3 python3-venv python3-pip ca-certificates curl" ;;
    python:dnf|python:yum) echo "python3 python3-pip ca-certificates curl" ;;
    python:pacman) echo "python python-pip ca-certificates curl" ;;
    python:zypper) echo "python3 python3-pip ca-certificates curl" ;;
    python:apk) echo "python3 py3-pip ca-certificates curl" ;;
    python:brew) echo "python@$PY_WANT" ;;
    build:apt-get) echo "build-essential python3-dev" ;;
    build:dnf|build:yum) echo "gcc python3-devel" ;;
    build:pacman) echo "base-devel" ;;
    build:zypper) echo "gcc python3-devel" ;;
    build:apk) echo "build-base python3-dev linux-headers" ;;
    storage:apt-get) echo "udisks2 cifs-utils nfs-common util-linux e2fsprogs dosfstools exfatprogs ntfs-3g" ;;
    storage:dnf|storage:yum) echo "udisks2 cifs-utils nfs-utils util-linux e2fsprogs dosfstools exfatprogs ntfs-3g" ;;
    storage:pacman) echo "udisks2 cifs-utils nfs-utils util-linux e2fsprogs dosfstools exfatprogs ntfs-3g" ;;
    storage:zypper) echo "udisks2 cifs-utils nfs-client util-linux e2fsprogs dosfstools exfatprogs ntfs-3g" ;;
    storage:apk) echo "udisks2 cifs-utils nfs-utils util-linux e2fsprogs dosfstools exfatprogs ntfs-3g-progs" ;;
    node:apt-get|node:dnf|node:yum|node:pacman|node:zypper|node:apk) echo "nodejs npm" ;;
    intercom:apt-get) echo "bluez pipewire pipewire-bin wireplumber pipewire-audio libportaudio2" ;;
    intercom:dnf|intercom:yum) echo "bluez pipewire pipewire-utils wireplumber portaudio" ;;
    intercom:pacman) echo "bluez bluez-utils pipewire wireplumber portaudio" ;;
    intercom:zypper) echo "bluez pipewire pipewire-tools wireplumber" ;;
    intercom:apk) echo "bluez pipewire pipewire-tools wireplumber" ;;
    office:apt-get) echo "libreoffice-impress libreoffice-writer libreoffice-calc fonts-liberation fonts-dejavu-core" ;;
    office:dnf|office:yum) echo "libreoffice-impress libreoffice-writer libreoffice-calc liberation-fonts" ;;
    office:pacman) echo "libreoffice-fresh ttf-liberation" ;;
    office:zypper) echo "libreoffice-impress libreoffice-writer libreoffice-calc liberation-fonts" ;;
    office:apk) echo "libreoffice font-liberation" ;;
    *) echo "" ;;
  esac
}
# Installs a feature's packages all at once, falling back to one at a time
# so one package missing on an older release doesn't block the rest.
install_feature_pkgs() {
  local feature=$1 label=$2 pkgs missing=()
  pkgs=$(pkgs_for "$feature")
  [[ -z $pkgs ]] && { warn "$label: no package list for this system ($PM) — install manually"; return 1; }
  # shellcheck disable=SC2086
  if run "$label" pkg_install $pkgs; then return 0; fi
  for p in $pkgs; do pkg_install "$p" >>"$LOG" 2>&1 || missing+=("$p"); done
  if [[ ${#missing[@]} -gt 0 ]]; then warn "$label: not available here: ${missing[*]}"; return 1; fi
  ok "$label (installed one by one)"
}

# ── uninstall ─────────────────────────────────────────────────────────
if [[ $UNINSTALL == 1 ]]; then
  banner
  printf '  %s\n\n' "${B}Uninstalling Void OS${R} ${D}(your files, .env and Void OS data are kept)${R}"
  if [[ $OS == Darwin ]]; then
    launchctl unload "$HOME/Library/LaunchAgents/com.voidos.server.plist" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/com.voidos.server.plist" && ok "Removed login item"
  elif [[ -f /etc/systemd/system/$SERVICE.service ]]; then
    need_root || fail "Removing the service needs sudo."
    as_root systemctl disable --now "$SERVICE" >>"$LOG" 2>&1 || true
    as_root rm -f "/etc/systemd/system/$SERVICE.service" "/etc/sudoers.d/voidos-mount"
    as_root systemctl daemon-reload || true
    ok "Removed the $SERVICE service"
  fi
  if [[ -e /usr/local/bin/voidos ]]; then as_root rm -f /usr/local/bin/voidos && ok "Removed the voidos command"; fi
  rm -f "$RUN_HOME/.local/bin/voidos" 2>/dev/null || true
  rm -rf "$VENV" "$TOOLS" && ok "Removed the Python environment (.venv)"
  printf '\n  %s\n  %s\n\n' "Void OS is uninstalled. To remove it completely, delete:" "${C}$VOIDOS_DIR${R}"
  trap - EXIT; exit 0
fi

# ══════════════════════════════════════════════════════════════════════
banner
{ echo; echo "──── install.sh $(date '+%Y-%m-%d %H:%M:%S') ────"; } >>"$LOG"
[[ -f "$VOIDOS_DIR/voidos.py" && -f "$SETUP_DIR/helper.py" ]] || fail "voidos.py isn't in the folder above this script — keep install.sh inside the Void OS \"first setup\" folder."
[[ $OS == Linux || $OS == Darwin ]] || fail "This installer is for Linux and macOS. On Windows, run install.cmd."

# ── 1. System ─────────────────────────────────────────────────────────
step "Checking this system"
info "System    ${B}$DISTRO_NAME${R} ${D}($ARCH)${R}$([[ $IS_PI == 1 ]] && echo " ${D}— Raspberry Pi${R}")$([[ $IS_WSL == 1 ]] && echo " ${D}— WSL${R}")"
info "Folder    $VOIDOS_DIR"
info "Runs as   ${B}$RUN_USER${R}"
if [[ -n $PM ]]; then info "Packages  $PM"; else warn "No supported package manager found — system packages will be skipped"; fi
MEM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
[[ $MEM_MB -gt 0 && $MEM_MB -lt 900 ]] && warn "Only ${MEM_MB} MB of RAM — Void OS runs, but App Store apps may struggle"
FREE_MB=$(df -Pm "$VOIDOS_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
[[ -n ${FREE_MB:-} && $FREE_MB -lt 1024 ]] && warn "Less than 1 GB free where Void OS is installed"
if curl -fsS --max-time 8 -o /dev/null https://pypi.org/simple/flask/ 2>/dev/null || \
   python3 -c "import urllib.request;urllib.request.urlopen('https://pypi.org/simple/flask/',timeout=8)" 2>/dev/null; then
  ok "Internet connection"
else
  warn "Couldn't reach pypi.org — downloads may fail (check the connection or a proxy)"
fi
if [[ $PM != brew && $PM != "" ]]; then need_root || warn "No sudo access — skipping system packages; Void OS will run with what's already installed"; fi
[[ $IS_WSL == 1 ]] && info "WSL detected — Windows users can also install natively with install.cmd"

# ── features ──────────────────────────────────────────────────────────
# Plain variables, not an associative array: macOS still ships bash 3.2.
FEATURES="docker storage tunnel sync intercom office mount"
feat()     { local v="F_$1"; [[ ${!v:-0} == 1 ]]; }
feat_set() { printf -v "F_$1" '%s' "$2"; }
# Everything is installed unless you say otherwise: the point of this installer is that nothing is
# missing later. (The password-less mount rule is a privilege, so it stays off.)
F_docker=1; F_storage=1; F_tunnel=1; F_sync=1; F_intercom=1; F_office=0; F_mount=0
[[ $OS == Darwin ]] && { F_storage=0; F_intercom=0; F_office=0; F_mount=0; }
TUNNEL_ENABLE=0       # installing cloudflared never switches the public tunnel on by itself
for spec in "$WITH:1" "$WITHOUT:0"; do
  list=${spec%:*}; val=${spec##*:}
  IFS=',' read -ra _w <<<"$list"
  for f in ${_w[@]+"${_w[@]}"}; do
    [[ -z $f ]] && continue
    [[ " $FEATURES " == *" $f "* ]] || fail "Unknown feature '$f' — choose from: ${FEATURES// /, }"
    feat_set "$f" "$val"
    [[ $f == tunnel && $val == 1 ]] && TUNNEL_ENABLE=1      # asked for by name: switch it on too
  done
done
if [[ $ASSUME_YES == 0 && -t 0 ]]; then
  printf '\n  %s\n' "${B}Optional features${R} ${D}— Void OS works without any of these; you can re-run this later.${R}"
  q() { local k=$1 text=$2; if ask_yn "$text" "$(feat "$k" && echo y || echo n)"; then feat_set "$k" 1; else feat_set "$k" 0; fi; }
  q docker   "Docker — App Store & Containers?"
  [[ $OS == Linux ]] && q storage  "USB drives & network shares (udisks2, SMB, NFS)?"
  q tunnel   "Cloudflare Tunnel — install it (cloudflared) so Void OS can be reached from anywhere?"
  q sync     "rclone — continuous Dropbox/Google Drive sync?"
  [[ $OS == Linux ]] && q intercom "Bluetooth audio intercom (BlueZ + PipeWire)?"
  [[ $OS == Linux ]] && q office   "LibreOffice for pixel-exact Office previews (about 150-400 MB; previews already work without it)?"
  if [[ $OS == Linux && $F_storage == 1 ]]; then
    printf '  %s\n' "${D}Mounting SMB/NFS shares needs root. Void OS can be allowed to run only${R}"
    printf '  %s\n' "${D}mount/umount without a password — convenient, but anyone controlling Void OS${R}"
    printf '  %s\n' "${D}could then mount things over system folders. Leave off if unsure.${R}"
    q mount  "Allow password-less mount for network shares?"
  fi
fi

# All questions are asked here, up front, so the rest runs unattended.
if [[ ! -f "$VOIDOS_DIR/.env" && $ASSUME_YES == 0 && -t 0 ]]; then
  printf '\n  %s\n' "${B}Settings${R} ${D}— change any time in .env${R}"
  if [[ -z $OPT_PORT ]]; then
    while true; do
      OPT_PORT=$(ask_val "Web port" "7777")
      [[ $OPT_PORT =~ ^[0-9]+$ ]] && (( OPT_PORT >= 1 && OPT_PORT <= 65535 )) && break
      printf '  %s\n' "${Y}'$OPT_PORT' isn't a port number (1-65535)${R}"
    done
  fi
  [[ -z $OPT_ROOT ]] && OPT_ROOT=$(ask_val "Folder for your files (shown in the Files tab)" "$RUN_HOME/voidos-files")
  printf '\n  %s\n' "${D}That's everything — the rest runs on its own.${R}"
fi
[[ -n $OPT_PORT && ! $OPT_PORT =~ ^[0-9]+$ ]] && fail "--port must be a number"

# ── 2. Python ─────────────────────────────────────────────────────────
step "Python ${MIN_PY_TEXT}+"
find_python() {
  local c p
  for p in "$TOOLS"/python/*/bin/python3; do [[ -x $p ]] && { echo "$p"; return 0; }; done
  [[ $PRIVATE_PY == 1 ]] && return 1
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    p=$(command -v "$c" 2>/dev/null) || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then echo "$p"; return 0; fi
  done
  return 1
}
has_venv() { "$1" -c 'import venv, ensurepip' >/dev/null 2>&1; }
PY="$(find_python || true)"
if [[ -z $PY && $PRIVATE_PY == 0 && -n $PM && $HAVE_ROOT == 1 ]]; then
  # shellcheck disable=SC2046
  run "Installing Python from $PM" pkg_install $(pkgs_for python) || true
  PY="$(find_python || true)"
fi
if [[ -z $PY ]]; then
  # The system's own Python is too old (Raspberry Pi OS 11, Debian 11,
  # Ubuntu 20.04 ship 3.8/3.9). Fetch a standalone Python into .tools/ with
  # uv — no system changes, works on x86_64, ARM64 and 32-bit ARM.
  if [[ $PRIVATE_PY == 1 ]]; then info "Fetching a private Python $PY_WANT for Void OS (--private-python)"
  else info "This system's Python is older than ${MIN_PY_TEXT} — fetching a private Python $PY_WANT for Void OS"; fi
  mkdir -p "$TOOLS/uv"
  libc=gnu; [[ $DISTRO == alpine ]] && libc=musl
  case "$OS:$ARCH" in
    Linux:x86_64)  UV_T="x86_64-unknown-linux-$libc" ;;
    Linux:aarch64) UV_T="aarch64-unknown-linux-$libc" ;;
    Linux:armv7l)  UV_T="armv7-unknown-linux-${libc}eabihf" ;;
    Linux:armv6l)  UV_T="arm-unknown-linux-${libc}eabihf" ;;
    Linux:i686)    UV_T="i686-unknown-linux-$libc" ;;
    Darwin:arm64)  UV_T="aarch64-apple-darwin" ;;
    Darwin:x86_64) UV_T="x86_64-apple-darwin" ;;
    *) fail "No standalone Python for $OS/$ARCH. Install Python ${MIN_PY_TEXT}+ yourself, then re-run." ;;
  esac
  # uv's official release archive from GitHub (no `curl | sh`); it then
  # fetches a standalone CPython build, also from GitHub.
  get_uv() { curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$UV_T.tar.gz" \
             | tar -xz -C "$TOOLS/uv" --strip-components=1; }
  run "Downloading uv (Python installer)" get_uv || fail "Couldn't download uv. Install Python ${MIN_PY_TEXT}+ yourself, then re-run."
  run "Downloading Python $PY_WANT" env UV_PYTHON_INSTALL_DIR="$TOOLS/python" "$TOOLS/uv/uv" python install "$PY_WANT" \
    || fail "Couldn't download Python $PY_WANT. Install Python ${MIN_PY_TEXT}+ yourself, then re-run."
  PY="$(env UV_PYTHON_INSTALL_DIR="$TOOLS/python" "$TOOLS/uv/uv" python find "$PY_WANT" 2>/dev/null || find_python || true)"
  [[ -n $PY ]] || fail "Python $PY_WANT was downloaded but couldn't be found."
fi
PYV="$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"
ok "Python $PYV ${D}($PY)${R}"
if ! has_venv "$PY"; then
  # Debian/Ubuntu split venv/ensurepip into its own package
  short="${PYV%.*}"
  case "$PM" in
    apt-get) run "Installing python$short-venv" pkg_install "python$short-venv" || run "Installing python3-venv" pkg_install python3-venv || true ;;
    apk) run "Installing py3-pip" pkg_install py3-pip || true ;;
    *) # intentional word-split: a space-separated package list
       # shellcheck disable=SC2046
       run "Installing pip/venv support" pkg_install $(pkgs_for python) || true ;;
  esac
  has_venv "$PY" || fail "Python's venv module is missing. Install it (e.g. sudo apt install python$short-venv) and re-run."
fi
case "$ARCH" in armv6l|armv7l|i686) NEED_BUILD=1 ;; *) NEED_BUILD=0 ;; esac
[[ $DISTRO == alpine ]] && NEED_BUILD=1   # musl: some wheels must be compiled
if [[ $NEED_BUILD == 1 && -n $PM && $HAVE_ROOT == 1 ]]; then
  install_feature_pkgs build "Build tools (needed on $ARCH for some packages)" || true
fi

# ── 3. Python packages ────────────────────────────────────────────────
step "Void OS packages"
if [[ -x "$VENV/bin/python" ]] && ! "$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
  info "Existing .venv uses an old Python — rebuilding it"; rm -rf "$VENV"
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  run "Creating a private Python environment (.venv)" as_user "$PY" -m venv "$VENV" || fail "Couldn't create .venv"
else
  ok "Python environment (.venv) already exists"
fi
VPY="$VENV/bin/python"
run "Updating pip" as_user "$VPY" -m pip install --disable-pip-version-check -q --upgrade pip setuptools wheel || warn "Couldn't update pip — continuing with the bundled one"
run "Installing requirements (Flask, Docker SDK, Gunicorn...)" \
  as_user "$VPY" -m pip install --disable-pip-version-check -q -r "$VOIDOS_DIR/requirements.txt" \
  || fail "Installing Python packages failed — see $LOG"
as_user "$VPY" -c 'import flask, psutil, docker, yaml, flask_sock, dotenv' 2>>"$LOG" && ok "Packages verified" || fail "Packages installed but can't be imported — see $LOG"
as_user "$VPY" "$HELPER" mark-requirements 2>>"$LOG" || true

# ── 4. System features ───────────────────────────────────────────────
step "System features"
any=0
if [[ $F_docker == 1 ]]; then any=1
  if command -v docker >/dev/null 2>&1; then ok "Docker already installed ${D}($(docker --version 2>/dev/null | cut -d, -f1))${R}"
  elif [[ $OS == Darwin ]]; then warn "Docker: install Docker Desktop from https://docker.com/products/docker-desktop"
  elif [[ $HAVE_ROOT == 1 ]]; then
    case "$PM" in
      pacman) run "Installing Docker" pkg_install docker docker-compose || warn "Docker install failed" ;;
      zypper) run "Installing Docker" pkg_install docker docker-compose || warn "Docker install failed" ;;
      apk)    run "Installing Docker" pkg_install docker docker-cli-compose || warn "Docker install failed" ;;
      *) get_docker() { curl -fsSL https://get.docker.com | as_root sh; }
         run "Installing Docker (official get.docker.com script)" get_docker || warn "Docker install failed — see https://docs.docker.com/engine/install/" ;;
    esac
  else warn "Docker: needs sudo to install"; fi
  if command -v docker >/dev/null 2>&1 && [[ $OS == Linux && $HAVE_ROOT == 1 ]]; then
    if [[ $HAS_SYSTEMD == 1 ]]; then as_root systemctl enable --now docker >>"$LOG" 2>&1 || true; fi
    if [[ $RUN_USER != root ]] && ! id -nG "$RUN_USER" | grep -qw docker; then
      as_root groupadd -f docker >>"$LOG" 2>&1 || true
      as_root usermod -aG docker "$RUN_USER" && ok "Added $RUN_USER to the docker group"
    fi
    docker compose version >/dev/null 2>&1 || as_root docker compose version >/dev/null 2>&1 \
      || warn "Docker Compose v2 ('docker compose') not found — the App Store needs it"
  fi
fi
if [[ $F_storage == 1 && $OS == Linux ]]; then any=1; install_feature_pkgs storage "USB & network-share tools" || true; fi
if [[ $F_office == 1 && $OS == Linux ]]; then any=1; install_feature_pkgs office "LibreOffice (document previews)" || true; fi
if [[ $F_intercom == 1 && $OS == Linux ]]; then any=1
  install_feature_pkgs intercom "Bluetooth & PipeWire" || true
  [[ $HAS_SYSTEMD == 1 && $HAVE_ROOT == 1 ]] && as_root systemctl enable --now bluetooth >>"$LOG" 2>&1 || true
  # PipeWire runs in the user's session; without lingering it only exists while someone is logged in,
  # so a headless server would have no audio after a reboot.
  [[ $HAS_SYSTEMD == 1 && $HAVE_ROOT == 1 && $RUN_USER != root ]] && as_root loginctl enable-linger "$RUN_USER" >>"$LOG" 2>&1 || true
fi
if [[ $F_tunnel == 1 ]]; then any=1
  if command -v cloudflared >/dev/null 2>&1; then ok "cloudflared already installed"
  elif [[ $OS == Darwin ]]; then run "Installing cloudflared" pkg_install cloudflared || warn "cloudflared install failed"
  elif [[ -n $CF_ARCH && $HAVE_ROOT == 1 ]]; then
    get_cf() { curl -fsSL -o "$TOOLS/cloudflared" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH" \
               && as_root install -m 0755 "$TOOLS/cloudflared" /usr/local/bin/cloudflared && rm -f "$TOOLS/cloudflared"; }
    mkdir -p "$TOOLS"; run "Installing cloudflared ($CF_ARCH)" get_cf || warn "cloudflared install failed"
  else warn "cloudflared: install it from https://github.com/cloudflare/cloudflared/releases"; fi
  # Node (npx) is only used to keep a Cloudflare Worker pointed at a changing Quick Tunnel address
  if [[ $OS == Linux && $HAVE_ROOT == 1 ]] && ! command -v npx >/dev/null 2>&1; then
    install_feature_pkgs node "Node.js (for the optional Cloudflare Worker sync)" || true
  fi
fi
if [[ $F_sync == 1 ]]; then any=1
  rcv=$(rclone version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)
  if [[ -n $rcv ]] && awk -v v="$rcv" 'BEGIN{split(v,a,".");exit !(a[1]>1||(a[1]==1&&a[2]>=58))}'; then ok "rclone $rcv already installed"
  elif [[ $OS == Darwin ]]; then run "Installing rclone" pkg_install rclone || warn "rclone install failed"
  elif [[ $HAVE_ROOT == 1 ]]; then
    # distro packages are often older than 1.58, which bisync needs
    get_rclone() { curl -fsSL https://rclone.org/install.sh | as_root bash; }
    run "Installing rclone (official script)" get_rclone || warn "rclone install failed"
  else warn "rclone: needs sudo to install"; fi
fi
if [[ $F_mount == 1 && $OS == Linux && $RUN_USER != root && $HAVE_ROOT == 1 ]]; then any=1
  MNT=$(command -v mount); UMNT=$(command -v umount)
  # also the drive checkers (safe automatic modes only), so a drive left dirty by a power cut can be repaired
  FSCK_LIST=""
  for t in e2fsck fsck.vfat dosfsck fsck.fat fsck.exfat exfatfsck ntfsfix; do
    p=$(command -v "$t" 2>/dev/null || true); [[ -n $p ]] && FSCK_LIST="$FSCK_LIST, $p"
  done
  rule="$RUN_USER ALL=(root) NOPASSWD: $MNT, $UMNT$FSCK_LIST"
  if echo "$rule" | as_root tee /etc/sudoers.d/voidos-mount >/dev/null && as_root chmod 0440 /etc/sudoers.d/voidos-mount \
     && as_root visudo -cf /etc/sudoers.d/voidos-mount >>"$LOG" 2>&1; then ok "Password-less mount and drive repair allowed for $RUN_USER"
  else as_root rm -f /etc/sudoers.d/voidos-mount; warn "Couldn't add the sudo rule for mounting"; fi
fi
[[ $any == 0 ]] && info "No optional features selected"

# ── 5. Configuration ─────────────────────────────────────────────────
step "Configuration"
H() { as_user "$VPY" "$HELPER" "$@"; }
created=$(H env-init)
if [[ $created == created ]]; then ok "Created .env from .env.example (every setting, with its default)"
else
  ok "Keeping your existing .env"
  added=$(H env-sync 2>>"$LOG" || echo 0)
  [[ ${added:-0} -gt 0 ]] && ok "Added $added new setting(s) to .env, commented out with their defaults"
fi
[[ $(H ensure-secret) == generated ]] && ok "Generated a new secret key" || ok "Secret key already set"
PORT="${OPT_PORT:-$(H env-get VOID_PORT)}"; PORT="${PORT:-7777}"
if [[ $created == created || -n $OPT_PORT ]]; then
  [[ -z $OPT_PORT ]] && PORT=$(ask_val "Web port" "$PORT")
  while ! H port-free "$PORT" && ! (curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" 2>/dev/null | grep -q '"ok"'); do
    warn "Port $PORT is already in use by another program"
    [[ $ASSUME_YES == 1 || ! -t 0 ]] && { PORT=$((PORT+1)); continue; }
    PORT=$(ask_val "Choose another port" "$((PORT+1))")
  done
  H env-set VOID_PORT "$PORT"
fi
ok "Port ${B}$PORT${R}"
ROOT_CUR="$(H env-get VOID_FILE_ROOT)"
if [[ -n $OPT_ROOT ]]; then FILE_ROOT="$OPT_ROOT"
elif [[ $created == created || -z $ROOT_CUR ]]; then
  FILE_ROOT=$(ask_val "Folder for your files (shown in the Files tab)" "$RUN_HOME/voidos-files")
else FILE_ROOT="$ROOT_CUR"; fi
FILE_ROOT="${FILE_ROOT/#\~/$RUN_HOME}"
if [[ ! -d $FILE_ROOT ]]; then
  if mkdir -p "$FILE_ROOT" 2>/dev/null || as_root mkdir -p "$FILE_ROOT"; then ok "Created $FILE_ROOT"; else fail "Couldn't create $FILE_ROOT"; fi
  [[ $RUN_USER != root ]] && as_root chown "$RUN_USER:$RUN_GROUP" "$FILE_ROOT" 2>/dev/null || true
fi
as_user test -w "$FILE_ROOT" || warn "$RUN_USER can't write to $FILE_ROOT — fix its permissions, or Void OS can't save files there"
H env-set VOID_FILE_ROOT "$FILE_ROOT"
ok "Files folder ${B}$FILE_ROOT${R}"
[[ $TUNNEL_ENABLE == 1 && $created == created ]] && H env-set VOID_CLOUDFLARE_TUNNEL true && ok "Cloudflare Tunnel switched on"
# The installing user owns the app folder's runtime data (installs via sudo)
if [[ $EUID == 0 && $RUN_USER != root ]]; then chown -R "$RUN_USER:$RUN_GROUP" "$VENV" "$VOIDOS_DIR/.env" "$LOG" 2>/dev/null || true; fi

# ── 6. Start on boot ─────────────────────────────────────────────────
step "Start on boot"
SERVICE_OK=0
if [[ $NO_SERVICE == 1 ]]; then info "Skipped (--no-service)"
elif [[ $OS == Darwin ]]; then
  PL="$HOME/Library/LaunchAgents/com.voidos.server.plist"; mkdir -p "$(dirname "$PL")"
  cat >"$PL" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.voidos.server</string>
  <key>ProgramArguments</key><array><string>$VPY</string><string>$VOIDOS_DIR/voidos.py</string></array>
  <key>WorkingDirectory</key><string>$VOIDOS_DIR</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$VOIDOS_DIR/voidos.log</string>
  <key>StandardErrorPath</key><string>$VOIDOS_DIR/voidos.log</string>
</dict></plist>
EOF
  launchctl unload "$PL" 2>/dev/null || true
  [[ $NO_START == 1 ]] || launchctl load "$PL" 2>>"$LOG" || true
  ok "Starts when you log in ${D}(launchd: com.voidos.server)${R}"; SERVICE_OK=1
elif [[ $HAS_SYSTEMD == 0 ]]; then warn "systemd isn't running here — start Void OS manually (see below)"
elif [[ $HAVE_ROOT == 0 ]]; then warn "Setting up the service needs sudo — skipped"
else
  # Restart=always (not on-failure): the in-app "Restart Void OS" button
  # exits with code 0 and relies on the service manager to start it again.
  # No EnvironmentFile=: voidos.py reads .env itself, and systemd's parser
  # would mangle the inline "# comments" .env uses (see README).
  UNIT=$(cat <<EOF
[Unit]
Description=Void OS home server
Documentation=file://${VOIDOS_DIR// /%%20}/README.md
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$VOIDOS_DIR
ExecStart="$VPY" "$VOIDOS_DIR/voidos.py"
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
)
  echo "$UNIT" | as_root tee "/etc/systemd/system/$SERVICE.service" >/dev/null
  as_root systemctl daemon-reload
  if as_root systemctl enable "$SERVICE" >>"$LOG" 2>&1; then ok "Installed the ${B}$SERVICE${R} service ${D}(starts on boot)${R}"; SERVICE_OK=1
  else warn "Couldn't enable the $SERVICE service"; fi
fi

# The 'voidos' command: start, stop, restart, status, logs. Works with the service (systemd), a login
# item (launchd) or, with neither, a plain background process.
CTL=""
install_ctl() {
  local src="$SETUP_DIR/voidos-ctl.sh" body tmp dest
  [[ -f $src ]] || { warn "voidos-ctl.sh is missing — the 'voidos' command wasn't installed"; return 1; }
  body=$(<"$src")
  body=${body//__VOIDOS_DIR__/"$VOIDOS_DIR"}
  body=${body//__SERVICE__/"$SERVICE"}
  mkdir -p "$TOOLS"; tmp="$TOOLS/voidos.ctl"
  printf '%s\n' "$body" >"$tmp"; chmod 755 "$tmp"
  if [[ $HAVE_ROOT == 1 ]] && as_root install -m 0755 "$tmp" /usr/local/bin/voidos >>"$LOG" 2>&1; then
    CTL=/usr/local/bin/voidos
  else
    dest="$RUN_HOME/.local/bin"
    if as_user mkdir -p "$dest" && as_user install -m 0755 "$tmp" "$dest/voidos" >>"$LOG" 2>&1; then CTL="$dest/voidos"; fi
  fi
  rm -f "$tmp"
  [[ -n $CTL ]]
}
if install_ctl; then ok "Installed the ${B}voidos${R} command ${D}($CTL)${R}"; else warn "Couldn't install the voidos command"; fi

# ── 7. Start ─────────────────────────────────────────────────────────
step "Starting Void OS"
STARTED=0
if [[ $NO_START == 1 ]]; then info "Skipped (--no-start)"
elif [[ $SERVICE_OK == 1 && $OS == Linux ]]; then
  as_root systemctl restart "$SERVICE"
  if run "Waiting for Void OS to answer" H wait-health "$PORT" 60; then STARTED=1
  else warn "Void OS didn't come up — see: ${CTL:-voidos} logs 50"; fi
elif [[ $SERVICE_OK == 1 ]]; then
  run "Waiting for Void OS to answer" H wait-health "$PORT" 60 && STARTED=1 || warn "Void OS didn't come up — see: ${CTL:-voidos} logs 50"
else
  info "Start it with:  ${C}cd \"$VOIDOS_DIR\" && .venv/bin/python voidos.py${R}"
fi

# ── done ──────────────────────────────────────────────────────────────
IPS="$(H lan-ips 2>/dev/null || true)"
line() { printf '  %s\n' "$*"; }
rule() { local n=$((COLS-4)) s=""; local ch="─"; [[ $UTF8 == 1 ]] || ch="-"; for ((i=0;i<n;i++)); do s+="$ch"; done; printf '  %s\n' "${P2}$s${R}"; }
printf '\n'; rule
if [[ $STARTED == 1 ]]; then line "${G}${B}${OK} Void OS is running${R}"; else line "${P}${B}Void OS is installed${R}"; fi
rule
line "Open it in a browser:"
line "   ${C}${B}http://localhost:$PORT${R}"
for ip in $IPS; do line "   ${C}http://$ip:$PORT${R}   ${D}from other devices on your network${R}"; done
printf '\n'
line "${B}First visit:${R} create your account — the first one becomes the Super User."
if [[ $F_docker == 1 && $RUN_USER != root && $OS == Linux ]] && ! id -nG "$(id -un)" | grep -qw docker; then
  line "${D}Docker in your own terminal needs a fresh login (the group was just added).${R}"
fi
printf '\n'
if [[ -n $CTL ]]; then
  line "${B}Control it${R} with the ${C}voidos${R} command:"
  line "   ${C}voidos start${R}  ${C}voidos stop${R}  ${C}voidos restart${R}  ${C}voidos status${R}  ${C}voidos logs${R}"
  [[ $CTL != /usr/local/bin/voidos ]] && line "   ${D}(installed in ${CTL%/*} — add it to your PATH if 'voidos' isn't found)${R}"
elif [[ $SERVICE_OK == 1 && $OS == Linux ]]; then
  line "${D}Status   ${R}sudo systemctl status $SERVICE"
  line "${D}Logs     ${R}${CTL:-voidos} logs"
  line "${D}Restart  ${R}sudo systemctl restart $SERVICE"
fi
line "${D}Settings ${R}$VOIDOS_DIR/.env ${D}(see \"first setup/.env.example\" for every option)${R}"
line "${D}Update   ${R}Settings → Updates in Void OS (or replace the files and re-run this installer)"
line "${D}Remove   ${R}bash \"first setup/install.sh\" --uninstall"
if [[ ${#WARNINGS[@]} -gt 0 ]]; then
  printf '\n  %s\n' "${Y}${B}Worth checking:${R}"
  for w in "${WARNINGS[@]}"; do line "${Y}${WARN}${R} $w"; done
fi
rule; printf '\n'
trap - EXIT
