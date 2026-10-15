#!/usr/bin/env bash
# voidos — start, stop and watch the Void OS server.
# Installed by "first setup/install.sh" (as /usr/local/bin/voidos); the two values below are filled in then.
VOIDOS_DIR="__VOIDOS_DIR__"
SERVICE="__SERVICE__"

if [[ -t 1 ]]; then B=$'\e[1m'; D=$'\e[2m'; G=$'\e[32m'; R=$'\e[31m'; C=$'\e[36m'; Y=$'\e[33m'; N=$'\e[0m'; else B=""; D=""; G=""; R=""; C=""; Y=""; N=""; fi
OS="$(uname -s)"
VPY="$VOIDOS_DIR/.venv/bin/python"
[[ -x $VPY ]] || VPY="$(command -v python3 || command -v python)"
LOGFILE="$VOIDOS_DIR/voidos.log"
PIDFILE="$VOIDOS_DIR/tmp/voidos.pid"
PLIST="$HOME/Library/LaunchAgents/com.voidos.server.plist"

have_systemd() { [[ $OS == Linux && -d /run/systemd/system ]] && [[ -f /etc/systemd/system/$SERVICE.service ]]; }
as_root() { if [[ $EUID == 0 ]]; then "$@"; else sudo "$@"; fi; }
journal() {
  if [[ $EUID == 0 ]] || id -nG | tr ' ' '\n' | grep -qxE 'adm|systemd-journal'; then journalctl "$@"; else sudo journalctl "$@"; fi
}
port() {
  local p; p=$(grep -E '^[[:space:]]*VOID_PORT[[:space:]]*=' "$VOIDOS_DIR/.env" 2>/dev/null | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*#.*$//')
  echo "${p:-7777}"
}
# Tidy up what the server prints: drop journald's "Oct 15 16:42:00 host python[123]:" prefix down to the
# time, line the messages up in one column, colour warnings / errors / tracebacks, dim the systemd notes.
# `voidos logs --raw` shows the untouched journal instead.
pretty() {
  awk -v B="$B" -v D="$D" -v G="$G" -v R="$R" -v Y="$Y" -v C="$C" -v N="$N" '
  function out(ts, msg, kind,   col, pre) {
    col = ""
    if (kind == "sys") col = D
    else if (msg ~ /^\[[^]]*\] x /  || msg ~ /^x /  || msg ~ /^Traceback/ || msg ~ /^[A-Za-z_.]+(Error|Exception|Exit)(:|$)/) col = R
    else if (msg ~ /^\[[^]]*\] ! / || msg ~ /^! /) col = Y
    else if (msg ~ /^\[[^]]*\] \+ / || msg ~ /^\+ /) col = G
    else if (msg ~ /^ +File "/ || msg ~ /^ +[~^]+ *$/) col = D
    pre = (ts != "") ? D ts N "  " : ""
    print pre col msg (col != "" ? N : "")
    fflush()
  }
  {
    line = $0; ts = ""; kind = ""
    if (match(line, /^[A-Z][a-z][a-z] +[0-9]+ [0-9][0-9]:[0-9][0-9]:[0-9][0-9] [^ ]+ [^ ]+: /)) {
      hdr = substr(line, 1, RLENGTH); line = substr(line, RLENGTH + 1)
      split(hdr, a, " "); ts = a[3]
      if (a[5] ~ /^(systemd|systemd\[)/) { kind = "sys"; line = "systemd: " line }
      orig = line
      sub(/^  /, "", line)                       # the server indents everything by two spaces
      if (intb && orig ~ /^ /) line = orig       # but a traceback keeps its own indentation
    } else if (line ~ /^-- /) { print D line N; fflush(); next }
    if (ts == "" && kind == "") sub(/^  /, "", line)
    if (line ~ /^Traceback/) intb = 1
    else if (intb && line !~ /^ /) { out(ts, line, kind); intb = 0; next }
    out(ts, line, kind)
  }'
}
up() { curl -fsS --max-time 2 "http://127.0.0.1:$(port)/api/health" >/dev/null 2>&1; }

# Without systemd or launchd the server runs as a background process this command manages itself.
bg_pid() { [[ -f $PIDFILE ]] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null && cat "$PIDFILE"; }
bg_start() {
  if bg_pid >/dev/null; then echo "Void OS is already running (pid $(bg_pid))."; return 0; fi
  mkdir -p "$(dirname "$PIDFILE")"
  ( cd "$VOIDOS_DIR" && PYTHONUNBUFFERED=1 nohup "$VPY" voidos.py >>"$LOGFILE" 2>&1 & echo $! >"$PIDFILE" )
}
bg_stop() {
  local p; p=$(bg_pid) || { echo "Void OS isn't running."; return 0; }
  kill "$p" 2>/dev/null; for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
  kill -9 "$p" 2>/dev/null; rm -f "$PIDFILE"
}

do_start() {
  if have_systemd; then as_root systemctl start "$SERVICE"
  elif [[ $OS == Darwin && -f $PLIST ]]; then launchctl load "$PLIST" 2>/dev/null || launchctl kickstart -k "gui/$(id -u)/com.voidos.server"
  else bg_start; fi
}
do_stop() {
  if have_systemd; then as_root systemctl stop "$SERVICE"
  elif [[ $OS == Darwin && -f $PLIST ]]; then launchctl unload "$PLIST" 2>/dev/null
  else bg_stop; fi
}
wait_up() {
  local n=0; printf '%s' "Waiting for Void OS"
  while ! up; do n=$((n+1)); [[ $n -gt 40 ]] && { printf ' %s\n' "${R}not answering yet${N} — see: voidos logs"; return 1; }; printf '.'; sleep 1; done
  printf ' %s\n' "${G}ready${N}"
}
urls() {
  local p; p=$(port)
  echo "  ${C}http://localhost:$p${N}"
  local ip; for ip in $(hostname -I 2>/dev/null); do [[ $ip == *:* ]] && continue; echo "  ${C}http://$ip:$p${N}"; done
}

usage() {
  cat <<EOF
${B}voidos${N} — control the Void OS server

  voidos start            start it
  voidos stop             stop it
  voidos restart          stop and start it (use after editing .env)
  voidos status           is it running, and where
  voidos logs             last 100 lines, then follow live (Ctrl+C to leave)
  voidos logs 500         the last 500 lines and exit
  voidos logs --since "1 hour ago"
  voidos logs --raw       the untouched journal (add a number or --since after it)
  voidos enable|disable   start automatically at boot, or not
  voidos config           edit the settings file (.env)
  voidos url              the addresses to open it at
  voidos update           re-run the installer (updates packages, keeps your data)
  voidos path             the folder Void OS lives in

EOF
}

case "${1:-help}" in
  start)   do_start && wait_up && urls ;;
  stop)    do_stop && echo "Void OS stopped." ;;
  restart) if have_systemd; then as_root systemctl restart "$SERVICE"; else do_stop; do_start; fi; wait_up && urls ;;
  status)
    if have_systemd; then systemctl status "$SERVICE" --no-pager -l -n 0 2>/dev/null | sed -n 1,12p; echo "${D}recent log: voidos logs 20${N}"; fi
    if up; then echo "${G}${B}Void OS is running${N}"; urls; else echo "${R}${B}Void OS is not answering${N}  ${D}(voidos start, or voidos logs)${N}"; exit 3; fi ;;
  logs)
    shift || true
    FILTER=pretty
    if [[ ${1:-} == --raw ]]; then FILTER=cat; shift; fi
    if have_systemd; then
      if [[ $# -eq 0 ]]; then journal -u "$SERVICE" -n 100 -f --no-pager | $FILTER
      elif [[ $1 =~ ^[0-9]+$ ]]; then journal -u "$SERVICE" -n "$1" --no-pager | $FILTER
      else journal -u "$SERVICE" --no-pager "$@" | $FILTER; fi
    else
      [[ -f $LOGFILE ]] || { echo "No log yet ($LOGFILE)."; exit 1; }
      if [[ $# -eq 0 ]]; then tail -n 100 -f "$LOGFILE" | $FILTER; elif [[ $1 =~ ^[0-9]+$ ]]; then tail -n "$1" "$LOGFILE" | $FILTER; else tail -n 100 -f "$LOGFILE" | $FILTER; fi
    fi ;;
  enable)
    if have_systemd; then as_root systemctl enable "$SERVICE" && echo "Void OS will start at boot."
    else echo "Start-at-boot is set up by the installer (bash \"$VOIDOS_DIR/first setup/install.sh\")."; fi ;;
  disable)
    if have_systemd; then as_root systemctl disable "$SERVICE" && echo "Void OS will no longer start at boot."
    else echo "No boot service is installed."; fi ;;
  config)  exec "${EDITOR:-$(command -v nano || command -v vi)}" "$VOIDOS_DIR/.env" ;;
  url|open) urls ;;
  path)    echo "$VOIDOS_DIR" ;;
  update)  exec bash "$VOIDOS_DIR/first setup/install.sh" "${@:2}" ;;
  help|-h|--help|"") usage ;;
  *) echo "${R}Unknown command: $1${N}"; usage; exit 2 ;;
esac
