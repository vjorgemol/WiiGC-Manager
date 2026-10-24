#!/bin/bash
# Wrapper script para WiiGC Manager

# Fuerza el renderizado por software en GTK4 para evitar problemas con OpenGL
# en sistemas sin drivers gráficos apropiados o en entornos virtualizados
export GSK_RENDERER=cairo

exec python3 /usr/share/wiigc-manager/main.py "$@"
