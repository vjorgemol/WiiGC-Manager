#!/usr/bin/env bash
# =============================================================================
# build.sh — Empaqueta WiiGC Manager como Flatpak (WiiGC-Manager.flatpak)
#
# La app es Python puro (GTK4/libadwaita vienen en el runtime de GNOME), así
# que no hace falta flatpak-builder ni el SDK: basta con el propio flatpak.
#
# Uso:
#   ./build.sh            → deja WiiGC-Manager.flatpak junto a este script
# Instalar el resultado:
#   flatpak install --user WiiGC-Manager.flatpak
# =============================================================================

set -euo pipefail

APP_ID=org.wiigcmanager.Gtk
RUNTIME=org.gnome.Platform
RUNTIME_VERSION=51
BRANCH=stable

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(dirname "$HERE")"
WORK="$(mktemp -d "${TMPDIR:-/var/tmp}/wiigc-flatpak.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

BUILD="$WORK/build"
# Sin SDK instalado: no se compila nada, así que el runtime hace también de SDK
flatpak build-init "$BUILD" "$APP_ID" "$RUNTIME" "$RUNTIME" "$RUNTIME_VERSION"

APP="$BUILD/files"
install -Dm755 "$HERE/wiigc-manager" "$APP/bin/wiigc-manager"
install -Dm644 "$SRC/wii-manager-server.py" "$APP/share/wiigc-manager/wii-manager-server.py"
(cd "$SRC" && find wii-manager-gtk -name '*.py' -exec install -Dm644 {} "$APP/share/wiigc-manager/{}" \;)
install -Dm644 "$HERE/$APP_ID.desktop" "$APP/share/applications/$APP_ID.desktop"
install -Dm644 "$HERE/$APP_ID.svg" "$APP/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -Dm644 "$HERE/$APP_ID.metainfo.xml" "$APP/share/metainfo/$APP_ID.metainfo.xml"

# Catálogo AppStream e iconos en share/app-info: es de donde GNOME Software saca
# el nombre, el icono, el autor, la versión y las capturas de un .flatpak. Lo
# genera normalmente flatpak-builder (appstreamcli compose); aquí se hace a mano.
mkdir -p "$APP/share/app-info/xmls"
python3 - "$HERE/$APP_ID.metainfo.xml" "$APP/share/app-info/xmls/$APP_ID.xml.gz" <<PY
import gzip, os, sys
import xml.etree.ElementTree as ET

import gi
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf

component = ET.parse(sys.argv[1]).getroot()
for size in (64, 128):
    icon_dir = '$APP/share/app-info/icons/flatpak/%dx%d' % (size, size)
    os.makedirs(icon_dir)
    GdkPixbuf.Pixbuf.new_from_file_at_size('$HERE/$APP_ID.svg', size, size).savev(
        os.path.join(icon_dir, '$APP_ID.png'), 'png', [], [])
    icon = ET.SubElement(component, 'icon', type='cached', width=str(size), height=str(size))
    icon.text = '$APP_ID.png'
bundle = ET.SubElement(component, 'bundle', type='flatpak',
                       runtime='$RUNTIME/$(flatpak --default-arch)/$RUNTIME_VERSION')
bundle.text = 'app/$APP_ID/$(flatpak --default-arch)/$BRANCH'
catalog = ET.Element('components', version='0.16', origin='flatpak')
catalog.append(component)
with gzip.open(sys.argv[2], 'wb') as f:
    f.write(ET.tostring(catalog, encoding='utf-8', xml_declaration=True))
PY

# Permisos:
#  - host + /run/media…: leer imágenes y escribir en las unidades USB/SD
#  - org.freedesktop.Flatpak: ejecutar en el sistema wit, wwt, lsblk, mkfs, pkexec, dolphin-tool…
#  - network: carátulas y base de datos de GameTDB, descarga de cargadores
#  - pulseaudio: sonido del banner del juego seleccionado
flatpak build-finish "$BUILD" \
    --command=wiigc-manager \
    --share=ipc --share=network \
    --socket=wayland --socket=fallback-x11 --socket=pulseaudio --device=dri \
    --filesystem=host --filesystem=/run/media --filesystem=/media --filesystem=/mnt --filesystem=/var/tmp \
    --talk-name=org.freedesktop.Flatpak

flatpak build-export "$WORK/repo" "$BUILD" "$BRANCH"
flatpak build-bundle "$WORK/repo" "$HERE/WiiGC-Manager.flatpak" "$APP_ID" "$BRANCH" \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo

echo "✓ $HERE/WiiGC-Manager.flatpak"
