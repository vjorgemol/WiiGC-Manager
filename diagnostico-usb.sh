#!/bin/bash
# Script de diagnóstico para problemas de detección de USB en WiiGC Manager
# Ejecuta: bash diagnostico-usb.sh

echo "========================================="
echo "Diagnóstico de detección USB - WiiGC Manager"
echo "========================================="
echo ""

echo "1. Información del sistema:"
echo "   - Distribución: $(lsb_release -ds 2>/dev/null || cat /etc/os-release | grep PRETTY_NAME | cut -d'"' -f2)"
echo "   - Kernel: $(uname -r)"
echo "   - Usuario: $USER"
echo ""

echo "2. Grupos del usuario actual:"
groups
echo ""

echo "3. ¿Está en el grupo 'disk'?"
if groups | grep -q '\bdisk\b'; then
    echo "   ✓ SÍ - El usuario está en el grupo 'disk'"
else
    echo "   ✗ NO - El usuario NO está en el grupo 'disk'"
    echo "   Para agregarlo ejecuta: sudo usermod -aG disk $USER"
    echo "   Luego cierra sesión y vuelve a entrar"
fi
echo ""

echo "4. Dispositivos de bloques detectados por lsblk:"
lsblk
echo ""

echo "5. Dispositivos removibles (USB/SD):"
lsblk | grep -E "1$" | awk '{print $1, $4, $7}'
echo ""

echo "6. Salida JSON de lsblk (mismo comando que usa WiiGC Manager):"
lsblk -J -b -o NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,RM,RO,MODEL,TRAN
echo ""

echo "7. Permisos de lectura en dispositivos de bloques:"
for dev in /dev/sd[a-z] /dev/sd[a-z][0-9]; do
    if [ -e "$dev" ]; then
        if [ -r "$dev" ]; then
            echo "   ✓ $dev - READABLE"
        else
            echo "   ✗ $dev - NOT READABLE (requiere permisos)"
        fi
    fi
done
echo ""

echo "8. Verificando instalación de WiiGC Manager:"
if command -v wiigc-manager >/dev/null 2>&1; then
    echo "   ✓ wiigc-manager está instalado en: $(which wiigc-manager)"
else
    echo "   ✗ wiigc-manager NO está instalado o no está en PATH"
fi
echo ""

echo "9. Verificando dependencias:"
for cmd in lsblk python3 wit wwt; do
    if command -v $cmd >/dev/null 2>&1; then
        echo "   ✓ $cmd: $(which $cmd)"
    else
        echo "   ✗ $cmd: NO ENCONTRADO"
    fi
done
echo ""

echo "========================================="
echo "Diagnóstico completado"
echo "========================================="
echo ""
echo "Si wiigc-manager no detecta tu USB, por favor:"
echo "1. Asegúrate de que el USB esté conectado"
echo "2. Ejecuta: sudo usermod -aG disk $USER"
echo "3. Cierra sesión y vuelve a entrar"
echo "4. Ejecuta wiigc-manager desde la terminal para ver los mensajes de debug"
echo ""
