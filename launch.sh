#!/usr/bin/env bash
# =============================================================================
# launch.sh — Lanzador de WiiFlow Manager
#
# Arranca el servidor backend (wii-manager-server.py) y abre la GUI
# en el navegador predeterminado del sistema.
#
# Uso:
#   ./launch.sh           # arranca normalmente
#   ./launch.sh --stop    # detiene el servidor si está corriendo
#   ./launch.sh --restart # reinicia el servidor
#
# Licencia: GNU GPL v3
# =============================================================================

set -euo pipefail

# ── Configuración ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_FILE="$SCRIPT_DIR/wii-manager-server.py"
CLIENT_URL="http://localhost:8765"
PID_FILE="$SCRIPT_DIR/.wii-manager.pid"
LOG_FILE="$SCRIPT_DIR/wii-manager.log"
PYTHON="${PYTHON:-python3}"          # sobrescribible: PYTHON=/usr/bin/python3.11 ./launch.sh
WAIT_SECONDS=2                       # segundos a esperar antes de abrir el navegador

# ── Colores ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}  ▸ $*${RESET}"; }
ok()      { echo -e "${GREEN}  ✓ $*${RESET}"; }
warn()    { echo -e "${YELLOW}  ⚠ $*${RESET}"; }
err()     { echo -e "${RED}  ✗ $*${RESET}" >&2; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }

# ── Funciones ─────────────────────────────────────────────────────────────────

# Comprueba si el servidor ya está corriendo leyendo el PID guardado
server_running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

# Devuelve el PID actual del servidor (desde el archivo) o vacío
server_pid() {
    [[ -f "$PID_FILE" ]] && cat "$PID_FILE" || echo ""
}

# Detiene el servidor enviando SIGTERM y espera a que termine
stop_server() {
    if server_running; then
        local pid; pid=$(server_pid)
        info "Deteniendo servidor (PID $pid)…"
        kill "$pid" 2>/dev/null || true
        # Esperar hasta 5s a que el proceso termine
        local i=0
        while kill -0 "$pid" 2>/dev/null && (( i < 10 )); do
            sleep 0.5
            (( i += 1 ))
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        rm -f "$PID_FILE"
        ok "Servidor detenido"
    else
        warn "El servidor no estaba corriendo"
    fi
}

# Abre la URL en el navegador predeterminado del sistema.
# En Ubuntu 24.04 con GNOME, gio open necesita las variables de sesión
# exportadas explícitamente cuando se ejecuta desde un script.
open_browser() {
    info "Abriendo $CLIENT_URL …"

    # Capturar variables de sesión del usuario actual
    local uid; uid=$(id -u)
    local display="${DISPLAY:-:1}"
    local dbus="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/${uid}/bus}"
    local xdg_runtime="${XDG_RUNTIME_DIR:-/run/user/${uid}}"

    if command -v gio &>/dev/null; then
        DISPLAY="$display" \
        DBUS_SESSION_BUS_ADDRESS="$dbus" \
        XDG_RUNTIME_DIR="$xdg_runtime" \
        gio open "$CLIENT_URL" >/dev/null 2>/dev/null &
        disown
        ok "Navegador abierto (gio open)"

    elif command -v xdg-open &>/dev/null; then
        DISPLAY="$display" \
        DBUS_SESSION_BUS_ADDRESS="$dbus" \
        XDG_RUNTIME_DIR="$xdg_runtime" \
        xdg-open "$CLIENT_URL" >/dev/null 2>/dev/null &
        disown
        ok "Navegador abierto (xdg-open)"

    elif [[ -x /snap/bin/firefox ]]; then
        DISPLAY="$display" \
        DBUS_SESSION_BUS_ADDRESS="$dbus" \
        XDG_RUNTIME_DIR="$xdg_runtime" \
        /snap/bin/firefox "$CLIENT_URL" >/dev/null 2>/dev/null &
        disown
        ok "Navegador abierto (Firefox snap)"

    else
        warn "No se encontró ningún navegador."
        warn "Abre manualmente: ${BOLD}$CLIENT_URL${RESET}"
    fi
}

# Comprueba que los requisitos están disponibles
check_requirements() {
    local ok=true

    if ! command -v "$PYTHON" &>/dev/null; then
        err "Python 3 no encontrado. Instálalo con: sudo apt install python3"
        ok=false
    fi

    if [[ ! -f "$SERVER_FILE" ]]; then
        err "No se encuentra $SERVER_FILE"
        err "Asegúrate de ejecutar este script desde el directorio de WiiFlow Manager"
        ok=false
    fi

    $ok || exit 1

    # wit y wwt son opcionales aquí (el servidor los comprueba al arrancar)
    command -v wwt &>/dev/null || warn "wwt no encontrado en PATH (configúralo en Ajustes)"
    command -v wit  &>/dev/null || warn "wit no encontrado en PATH (configúralo en Ajustes)"
}

# Arranca el servidor en segundo plano y guarda su PID
start_server() {
    info "Arrancando servidor backend…"
    "$PYTHON" -u "$SERVER_FILE" >> "$LOG_FILE" 2>&1 &
    local pid=$!
    disown "$pid" 2>/dev/null || true
    echo "$pid" > "$PID_FILE"

    # Esperar a que el servidor acepte conexiones (máximo 15s)
    info "Esperando a que el servidor esté listo…"
    local i=0
    while (( i < 30 )); do
        # Intentar conectar al puerto 8765 con bash puro (sin curl)
        if (echo > /dev/tcp/127.0.0.1/8765) &>/dev/null; then
            ok "Servidor listo (PID $pid)"
            return 0
        fi
        sleep 0.5
        (( i += 1 ))
    done

    err "El servidor no respondió en 15 segundos"
    err "Revisa el log: $LOG_FILE"
    stop_server
    exit 1
}

# ── Manejo de señales: limpiar al salir con Ctrl+C ────────────────────────────
cleanup() {
    echo ""
    warn "Interrupción recibida"
    read -r -t 5 -p "  ¿Detener el servidor? [s/N] " resp || resp="n"
    if [[ "${resp,,}" == "s" ]]; then
        stop_server
    else
        info "El servidor sigue corriendo (PID $(server_pid))"
        info "Para detenerlo: ./launch.sh --stop"
    fi
    exit 0
}
trap cleanup INT TERM

# ── Punto de entrada ──────────────────────────────────────────────────────────

header "══════════════════════════════════════════"
header "   WiiFlow Manager"
header "══════════════════════════════════════════"
echo ""

case "${1:-}" in
    --stop)
        stop_server
        exit 0
        ;;
    --start)
        check_requirements
        if server_running; then
            ok "El servidor ya está corriendo (PID $(server_pid))"
        else
            start_server
        fi
        open_browser
        echo ""
        info "Log del servidor: $LOG_FILE"
        info "Para detener:     $0 --stop"
        exit 0
        ;;
    --restart)
        stop_server
        sleep 0.5
        check_requirements
        start_server
        open_browser
        ;;
    --status)
        if server_running; then
            ok "Servidor corriendo (PID $(server_pid))"
        else
            warn "Servidor detenido"
        fi
        exit 0
        ;;
    --log)
        [[ -f "$LOG_FILE" ]] && tail -f "$LOG_FILE" || warn "Sin log todavía"
        exit 0
        ;;
    "")
        check_requirements

        if server_running; then
            ok "El servidor ya está corriendo (PID $(server_pid))"
            open_browser
        else
            start_server
            open_browser
        fi
        ;;
    *)
        echo "Uso: $0 [--start | --stop | --restart | --status | --log]"
        exit 1
        ;;
esac

echo ""
info "Log del servidor: $LOG_FILE"
info "Para detener:     ./launch.sh --stop"
echo ""

# Mantener el script en primer plano para poder recibir Ctrl+C.
# Usamos un bucle en lugar de `wait` para no quedar bloqueados
# esperando al proceso del navegador (gio open).
while server_running; do
    sleep 2
done

warn "El servidor se ha detenido inesperadamente"
info "Revisa el log: $LOG_FILE"
