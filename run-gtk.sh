#!/usr/bin/env bash
# =============================================================================
# run-gtk.sh — Lanzador de WiiGC Manager (versión nativa GTK4 + libadwaita)
#
# A diferencia de launch.sh (que arranca un servidor HTTP y abre el navegador),
# esta versión es un único proceso GTK4 sin servidor ni navegador de por medio.
#
# Uso:
#   ./run-gtk.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

exec "$PYTHON" "$SCRIPT_DIR/wii-manager-gtk/main.py" "$@"
