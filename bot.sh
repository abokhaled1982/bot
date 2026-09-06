#!/usr/bin/env bash
# bot.sh — startet/stoppt die WhatsApp-Bridge (Node) und den Copy-Trader
# (Python) gemeinsam; kuemmert sich um Port- und Prozess-Konflikte und legt
# separate Logs unter logs/ ab.
#
# Nutzung:
#   ./bot.sh start | stop | restart | status | logs [bridge|bot] | tail
set -euo pipefail

# ── Konfiguration ────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$REPO_ROOT/.venv/bin/python"
LOG_DIR="$REPO_ROOT/logs"
BRIDGE_DIR="$REPO_ROOT/whatsapp_bridge"

BRIDGE_PORT_DEFAULT=3000
CMD_PORT_DEFAULT=3100
BRIDGE_PORT=$BRIDGE_PORT_DEFAULT
CMD_PORT=$CMD_PORT_DEFAULT
BRIDGE_URL="http://127.0.0.1:${BRIDGE_PORT}"
WEBHOOK_URL="http://127.0.0.1:${CMD_PORT}/wa"

# Empfaenger fuer ausgehende WhatsApp-Nachrichten. Kann vor dem Aufruf per
# WHATSAPP_TO=... ./bot.sh start ueberschrieben werden.
WHATSAPP_TO="${WHATSAPP_TO:-120363429746414084@g.us}"

BOT_ARGS=(--status-interval 0)

BRIDGE_PID_FILE="$LOG_DIR/bridge.pid"
BOT_PID_FILE="$LOG_DIR/bot.pid"
BRIDGE_LOG="$LOG_DIR/bridge.log"
BOT_LOG="$LOG_DIR/bot.log"
PORTS_FILE="$LOG_DIR/ports.env"

mkdir -p "$LOG_DIR"

# ── Farben / Helper ──────────────────────────────────────────────────────────
if [[ -t 1 ]]; then C_G=$'\e[32m'; C_R=$'\e[31m'; C_Y=$'\e[33m'; C_B=$'\e[36m'; C_0=$'\e[0m'
else C_G=""; C_R=""; C_Y=""; C_B=""; C_0=""; fi
info() { printf '%s[i]%s %s\n' "$C_B" "$C_0" "$*"; }
ok()   { printf '%s[+]%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_Y" "$C_0" "$*"; }
err()  { printf '%s[x]%s %s\n' "$C_R" "$C_0" "$*" >&2; }

pid_alive() { local pid=$1; [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; }

read_pid() { local f=$1; [[ -f "$f" ]] && cat "$f" 2>/dev/null || true; }

port_in_use() {
    ss -ltn "sport = :$1" 2>/dev/null | awk 'NR>1' | grep -q .
}

pids_on_port() {
    # PIDs der Prozesse, die auf $1 lauschen — ohne fuser/lsof
    ss -ltnpH "sport = :$1" 2>/dev/null \
        | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

pick_free_port() {
    # Ab $1 aufwaerts den ersten freien Port suchen (max. 20 Versuche).
    local start=$1 tries=${2:-20} i p
    for i in $(seq 0 $((tries-1))); do
        p=$((start+i))
        if ! port_in_use "$p"; then
            printf '%s' "$p"; return 0
        fi
    done
    return 1
}

kill_pid_tree() {
    local pid=$1
    pid_alive "$pid" || return 0
    # Alle Kinder mit einsammeln
    local pids
    pids=$(pgrep -P "$pid" 2>/dev/null || true)
    kill -TERM "$pid" 2>/dev/null || true
    for c in $pids; do kill -TERM "$c" 2>/dev/null || true; done
    for _ in 1 2 3 4 5 6 7 8; do
        pid_alive "$pid" || return 0
        sleep 0.25
    done
    warn "PID $pid reagiert nicht — SIGKILL"
    kill -KILL "$pid" 2>/dev/null || true
    for c in $pids; do kill -KILL "$c" 2>/dev/null || true; done
}

free_port() {
    local port=$1 label=$2
    port_in_use "$port" || return 0
    warn "Port $port ($label) belegt — beende Prozess(e)…"
    for pid in $(pids_on_port "$port"); do
        info "  -> kill $pid"
        kill_pid_tree "$pid"
    done
    sleep 0.3
    if port_in_use "$port"; then
        err "Port $port immer noch belegt"
        return 1
    fi
    ok "Port $port frei"
}

kill_stale() {
    # Sicherheitsnetz: alte Bridge-/Bot-Prozesse einsammeln, die keinen
    # aktuellen PID-File-Eintrag haben.
    local pat=$1 label=$2 keep=${3:-}
    local pids
    pids=$(pgrep -f "$pat" 2>/dev/null || true)
    for pid in $pids; do
        [[ -n "$keep" && "$pid" == "$keep" ]] && continue
        warn "Verwaister $label-Prozess PID $pid — kill"
        kill_pid_tree "$pid"
    done
}

save_ports() {
    printf 'BRIDGE_PORT=%s\nCMD_PORT=%s\n' "$BRIDGE_PORT" "$CMD_PORT" > "$PORTS_FILE"
}

load_ports() {
    [[ -f "$PORTS_FILE" ]] || return 0
    # shellcheck disable=SC1090
    source "$PORTS_FILE"
    BRIDGE_URL="http://127.0.0.1:${BRIDGE_PORT}"
    WEBHOOK_URL="http://127.0.0.1:${CMD_PORT}/wa"
}

choose_port() {
    local want=$1 label=$2
    if port_in_use "$want"; then
        local free
        if free=$(pick_free_port "$((want+1))"); then
            warn "Port $want ($label) belegt (fremder Prozess) — nutze stattdessen $free"
            printf '%s' "$free"; return 0
        fi
        err "Kein freier Port ab $want fuer $label gefunden"
        return 1
    fi
    printf '%s' "$want"
}

rotate_log() {
    local f=$1
    [[ -f "$f" && -s "$f" ]] || return 0
    local ts
    ts=$(date +%Y%m%d-%H%M%S)
    mv "$f" "${f%.log}.${ts}.log"
    # Nur die letzten 5 rotierten Files behalten
    ls -1t "${f%.log}".*.log 2>/dev/null | tail -n +6 | xargs -r rm -f
}

wait_for_url() {
    local url=$1 label=$2 tries=${3:-40}
    for _ in $(seq 1 "$tries"); do
        if curl -sf -m 1 "$url" >/dev/null 2>&1; then
            ok "$label erreichbar ($url)"
            return 0
        fi
        sleep 0.25
    done
    err "$label nicht erreichbar nach ~$((tries/4))s: $url"
    return 1
}

wait_for_ready_line() {
    local log=$1 needle=$2 label=$3 tries=${4:-80}
    for _ in $(seq 1 "$tries"); do
        [[ -f "$log" ]] && grep -Fq "$needle" "$log" && { ok "$label ready"; return 0; }
        sleep 0.25
    done
    err "$label nicht ready (kein '$needle' in $log)"
    return 1
}

# ── Voraussetzungen pruefen ──────────────────────────────────────────────────
preflight() {
    [[ -x "$VENV_PY" ]] || { err "$VENV_PY fehlt — venv nicht angelegt?"; exit 1; }
    command -v node >/dev/null || { err "node nicht im PATH"; exit 1; }
    [[ -f "$BRIDGE_DIR/server.js" ]] || { err "$BRIDGE_DIR/server.js fehlt"; exit 1; }
    [[ -f "$REPO_ROOT/live_copytrader.py" ]] || { err "live_copytrader.py fehlt"; exit 1; }
    [[ -f "$REPO_ROOT/.env" ]] || warn ".env fehlt — Bot laeuft evtl. ohne Keys"
    command -v curl >/dev/null || { err "curl fehlt"; exit 1; }
    command -v ss   >/dev/null || { err "ss (iproute2) fehlt"; exit 1; }
}

# ── Aktionen ─────────────────────────────────────────────────────────────────
start_bridge() {
    local old
    old=$(read_pid "$BRIDGE_PID_FILE")
    if pid_alive "$old"; then
        info "Bridge laeuft bereits (PID $old) — skip"
        return 0
    fi
    kill_stale "node .*server\\.js" "Bridge"
    BRIDGE_PORT=$(choose_port "$BRIDGE_PORT_DEFAULT" "Bridge") || return 1
    BRIDGE_URL="http://127.0.0.1:${BRIDGE_PORT}"
    save_ports
    rotate_log "$BRIDGE_LOG"
    info "Starte Bridge auf Port $BRIDGE_PORT → Log: $BRIDGE_LOG"
    (
        cd "$BRIDGE_DIR"
        BRIDGE_PORT="$BRIDGE_PORT" WEBHOOK_URL="$WEBHOOK_URL" \
            nohup node server.js >>"$BRIDGE_LOG" 2>&1 &
        echo $! > "$BRIDGE_PID_FILE"
    )
    local pid
    pid=$(read_pid "$BRIDGE_PID_FILE")
    ok "Bridge PID $pid"
    wait_for_ready_line "$BRIDGE_LOG" "[BRIDGE] ready" "Bridge" 120 \
        || wait_for_url "$BRIDGE_URL/health" "Bridge-Health" 60
}

start_bot() {
    local old
    old=$(read_pid "$BOT_PID_FILE")
    if pid_alive "$old"; then
        info "Bot laeuft bereits (PID $old) — skip"
        return 0
    fi
    kill_stale "python.* live_copytrader\\.py" "Bot"
    CMD_PORT=$(choose_port "$CMD_PORT_DEFAULT" "Cmd-Webhook") || return 1
    WEBHOOK_URL="http://127.0.0.1:${CMD_PORT}/wa"
    save_ports
    rotate_log "$BOT_LOG"
    info "Starte Bot (Cmd-Port $CMD_PORT) → Log: $BOT_LOG (WHATSAPP_TO=$WHATSAPP_TO)"
    (
        cd "$REPO_ROOT"
        WHATSAPP_BRIDGE_URL="$BRIDGE_URL" \
        WHATSAPP_TO="$WHATSAPP_TO" \
        nohup "$VENV_PY" -u live_copytrader.py \
            --webhook-port "$CMD_PORT" "${BOT_ARGS[@]}" \
            >>"$BOT_LOG" 2>&1 &
        echo $! > "$BOT_PID_FILE"
    )
    local pid
    pid=$(read_pid "$BOT_PID_FILE")
    ok "Bot PID $pid"
    wait_for_ready_line "$BOT_LOG" "[CMD-WH] listening" "Bot" 120 || true
}

stop_one() {
    local pid_file=$1 label=$2 pat=$3
    local pid
    pid=$(read_pid "$pid_file")
    if pid_alive "$pid"; then
        info "Stoppe $label (PID $pid)"
        kill_pid_tree "$pid"
    else
        info "$label PID-File leer/tot — pruefe verwaiste Prozesse"
    fi
    kill_stale "$pat" "$label"
    rm -f "$pid_file"
    ok "$label gestoppt"
}

cmd_start() {
    preflight
    start_bridge
    start_bot
    cmd_status
}

cmd_stop() {
    load_ports
    stop_one "$BOT_PID_FILE"    "Bot"    "python.* live_copytrader\\.py"
    stop_one "$BRIDGE_PID_FILE" "Bridge" "node .*server\\.js"
    rm -f "$PORTS_FILE"
}

cmd_restart() { cmd_stop; sleep 0.5; cmd_start; }

cmd_status() {
    load_ports
    local bp
    bp=$(read_pid "$BRIDGE_PID_FILE")
    if pid_alive "$bp"; then ok "Bridge: laeuft (PID $bp, Port $BRIDGE_PORT)"
    else                     warn "Bridge: aus"; fi

    local otp
    otp=$(read_pid "$BOT_PID_FILE")
    if pid_alive "$otp"; then ok "Bot:    laeuft (PID $otp, Cmd-Port $CMD_PORT)"
    else                      warn "Bot:    aus"; fi

    printf '  Ports: '
    port_in_use "$BRIDGE_PORT" && printf '%s ' "${BRIDGE_PORT}=belegt" || printf '%s ' "${BRIDGE_PORT}=frei"
    port_in_use "$CMD_PORT"    && printf '%s\n' "${CMD_PORT}=belegt" || printf '%s\n' "${CMD_PORT}=frei"

    if [[ -n "$bp" ]] && pid_alive "$bp"; then
        local h
        h=$(curl -sf -m 2 "$BRIDGE_URL/health" 2>/dev/null || echo '{}')
        info "Bridge /health: $h"
    fi
    info "Logs: $BRIDGE_LOG | $BOT_LOG"
}

cmd_logs() {
    local which=${1:-both}
    case "$which" in
        bridge) tail -n 200 -F "$BRIDGE_LOG" ;;
        bot)    tail -n 200 -F "$BOT_LOG"    ;;
        both|*) tail -n 100 -F "$BRIDGE_LOG" "$BOT_LOG" ;;
    esac
}

case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    logs|tail) shift; cmd_logs "${1:-}" ;;
    *)
        cat <<EOF
bot.sh — WhatsApp-Bridge + Copy-Trader Prozessmanager

  ./bot.sh start                 Beide Prozesse sauber hochfahren
  ./bot.sh stop                  Beide Prozesse beenden + Ports freigeben
  ./bot.sh restart               stop + start
  ./bot.sh status                Status + Ports + Bridge-Health
  ./bot.sh logs [bridge|bot]     Live-Logs (Default: beide)

  Ueberschreibbare Env-Variablen (vor dem Aufruf setzen):
    WHATSAPP_TO   Empfaenger-ID (Default: $WHATSAPP_TO)

  Logs: $BRIDGE_LOG , $BOT_LOG
EOF
        exit 1
        ;;
esac
