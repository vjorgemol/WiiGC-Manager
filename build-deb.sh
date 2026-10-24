#!/bin/bash
# =============================================================================
# build-deb.sh — Construye el paquete .deb de WiiGC Manager para Debian 13
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colores para mensajes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  WiiGC Manager - Construcción de .deb${NC}"
echo -e "${GREEN}========================================${NC}"

# Verificar que estamos en el directorio correcto
if [ ! -f "README.md" ] || [ ! -d "wii-manager-gtk" ]; then
    echo -e "${RED}Error: Este script debe ejecutarse desde el directorio raíz del proyecto${NC}"
    exit 1
fi

# Verificar dependencias de construcción
echo -e "\n${YELLOW}[1/4] Verificando dependencias de construcción...${NC}"
MISSING_DEPS=()

for dep in debhelper dh-python python3-all python3-gi; do
    if ! dpkg -s "$dep" >/dev/null 2>&1; then
        MISSING_DEPS+=("$dep")
    fi
done

if [ ${#MISSING_DEPS[@]} -ne 0 ]; then
    echo -e "${RED}Faltan las siguientes dependencias de construcción:${NC}"
    printf '%s\n' "${MISSING_DEPS[@]}"
    echo ""
    echo -e "${YELLOW}Instálalas con:${NC}"
    echo "sudo apt install ${MISSING_DEPS[*]}"
    exit 1
fi

echo -e "${GREEN}✓ Todas las dependencias están instaladas${NC}"

# Limpiar construcciones anteriores
echo -e "\n${YELLOW}[2/4] Limpiando construcciones anteriores...${NC}"
if [ -d "debian/wiigc-manager" ]; then
    rm -rf debian/wiigc-manager
fi
if [ -f "../wiigc-manager_"*.deb ]; then
    rm -f ../wiigc-manager_*.deb
fi
echo -e "${GREEN}✓ Limpieza completada${NC}"

# Construir el paquete
echo -e "\n${YELLOW}[3/4] Construyendo el paquete .deb...${NC}"
dpkg-buildpackage -us -uc -b

# Verificar que se creó el paquete
echo -e "\n${YELLOW}[4/4] Verificando el paquete generado...${NC}"
DEB_FILE=$(ls -t ../wiigc-manager_*.deb 2>/dev/null | head -1)

if [ -z "$DEB_FILE" ]; then
    echo -e "${RED}Error: No se pudo encontrar el paquete .deb generado${NC}"
    exit 1
fi

# Mostrar información del paquete
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  ✓ Paquete construido exitosamente${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "Archivo: ${GREEN}$DEB_FILE${NC}"
echo ""
dpkg-deb -I "$DEB_FILE" | grep -E "Package:|Version:|Architecture:|Depends:"
echo ""

# Mostrar contenido del paquete
echo -e "${YELLOW}Contenido del paquete:${NC}"
dpkg-deb -c "$DEB_FILE" | grep -v "/$" | awk '{print "  " $6}'
echo ""

# Instrucciones de instalación
echo -e "${YELLOW}Para instalar el paquete:${NC}"
echo -e "  sudo dpkg -i $DEB_FILE"
echo -e "  sudo apt install -f  ${NC}# Si hay dependencias faltantes${NC}"
echo ""
echo -e "${YELLOW}Para desinstalar:${NC}"
echo -e "  sudo apt remove wiigc-manager"
echo ""
