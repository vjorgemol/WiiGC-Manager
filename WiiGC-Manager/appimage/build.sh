#!/usr/bin/env bash
# =============================================================================
# build.sh — Empaqueta WiiGC Manager como AppImage (WiiGC-Manager-x86_64.AppImage)
#
# A diferencia del Flatpak, que usa el runtime de GNOME, la AppImage lleva
# dentro todo lo que la app necesita: Python, PyGObject, GTK4, libadwaita y
# GStreamer. Se toman de Ubuntu 24.04 (glibc 2.39), dentro de un contenedor,
# así que el resultado funciona en Ubuntu 24.04 o posterior y en Fedora 40 o
# posterior. Del sistema solo usa glibc, los controladores gráficos, las
# fuentes y poco más (lista EXCLUDE), además de wit, wwt y demás herramientas.
#
# Uso (hace falta podman o docker, y conexión a internet):
#   ./build.sh            → deja WiiGC-Manager-x86_64.AppImage junto a este script
# Ejecutar el resultado:
#   ./WiiGC-Manager-x86_64.AppImage
# =============================================================================

set -euo pipefail

APP_ID=org.wiigcmanager.Gtk
BASE_IMAGE=docker.io/library/ubuntu:24.04
ARCH="$(uname -m)"
OUTPUT="WiiGC-Manager-$ARCH.AppImage"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(dirname "$HERE")"

# ── Fuera del contenedor: relanzarse dentro de Ubuntu ───────────────────────
if [[ "${1:-}" != --in-container ]]; then
    ENGINE="$(command -v podman || command -v docker)" || {
        echo "Hace falta podman o docker para construir la AppImage" >&2; exit 1; }
    # label=disable: leer el código en Fedora (SELinux) sin reetiquetarlo
    "$ENGINE" run --rm --security-opt label=disable \
        -e HOST_OWNER="$(id -u):$(id -g)" -e ENGINE="$(basename "$ENGINE")" \
        -v "$SRC:/src:ro" -v "$HERE:/out" \
        "$BASE_IMAGE" bash /src/appimage/build.sh --in-container
    echo "✓ $HERE/$OUTPUT"
    exit 0
fi

# ── Dentro del contenedor ───────────────────────────────────────────────────
SRC=/src
HERE=/src/appimage
SYSLIB="/usr/lib/$ARCH-linux-gnu"
APPDIR=/tmp/AppDir
USR="$APPDIR/usr"
LIB="$USR/lib"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -qq -y --no-install-recommends \
    python3 python3-gi python3-gi-cairo \
    gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-alsa \
    librsvg2-common adwaita-icon-theme dconf-gsettings-backend libglib2.0-bin \
    ca-certificates curl file desktop-file-utils >/dev/null

PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
VERSION="$(python3 -c 'import sys, xml.etree.ElementTree as ET
print(ET.parse(sys.argv[1]).getroot().find("releases/release").get("version"))' \
    "$SRC/flatpak/$APP_ID.metainfo.xml")"

# La app, con la misma disposición que en el Flatpak (about.py busca así el metainfo)
install -Dm755 "$HERE/AppRun" "$APPDIR/AppRun"
install -Dm644 "$SRC/wii-manager-server.py" "$USR/share/wiigc-manager/wii-manager-server.py"
(cd "$SRC" && find wii-manager-gtk -name '*.py' -exec install -Dm644 {} "$USR/share/wiigc-manager/{}" \;)
install -Dm644 "$SRC/flatpak/$APP_ID.desktop" "$USR/share/applications/$APP_ID.desktop"
install -Dm644 "$SRC/flatpak/$APP_ID.svg" "$USR/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -Dm644 "$SRC/flatpak/$APP_ID.metainfo.xml" "$USR/share/metainfo/$APP_ID.metainfo.xml"
ln -s "usr/share/applications/$APP_ID.desktop" "$APPDIR/$APP_ID.desktop"
ln -s "usr/share/icons/hicolor/scalable/apps/$APP_ID.svg" "$APPDIR/$APP_ID.svg"
ln -s "$APP_ID.svg" "$APPDIR/.DirIcon"
mkdir -p "$USR/bin" && ln -s ../../AppRun "$USR/bin/wiigc-manager"

# Python: intérprete, biblioteca estándar (sin lo que una app gráfica no usa) y PyGObject
install -Dm755 "/usr/bin/python$PYVER" "$USR/bin/python3"
mkdir -p "$LIB" && cp -a "/usr/lib/python$PYVER" "$LIB/"
(cd "$LIB/python$PYVER" && rm -rf test idlelib tkinter turtledemo ensurepip lib2to3 pydoc_data \
    config-* dist-packages EXTERNALLY-MANAGED sitecustomize.py \
    lib-dynload/readline.* lib-dynload/_curses*)
install -Dm644 "$HERE/sitecustomize.py" "$LIB/python$PYVER/sitecustomize.py"
mkdir -p "$LIB/python3/dist-packages"
cp -a /usr/lib/python3/dist-packages/{gi,cairo} "$LIB/python3/dist-packages/"

# Módulos que GTK, GIO, gdk-pixbuf y GStreamer cargan en tiempo de ejecución
mkdir -p "$LIB/gtk-4.0/4.0.0" "$LIB/gio/modules" "$LIB/gdk-pixbuf-2.0/loaders" "$LIB/gstreamer-1.0"
cp -a "$SYSLIB/gtk-4.0/4.0.0/printbackends" "$LIB/gtk-4.0/4.0.0/"
cp "$SYSLIB/gio/modules/libdconfsettings.so" "$LIB/gio/modules/"
cp "$SYSLIB"/gdk-pixbuf-2.0/2.10.0/loaders/*.so "$LIB/gdk-pixbuf-2.0/loaders/"
sed "s|$SYSLIB/gdk-pixbuf-2.0/2.10.0/loaders|@LIBDIR@/gdk-pixbuf-2.0/loaders|" \
    "$SYSLIB/gdk-pixbuf-2.0/2.10.0/loaders.cache" > "$LIB/gdk-pixbuf-2.0/loaders.cache.in"
# GStreamer solo reproduce el sonido de los banners: basta con los complementos de audio
for plugin in coreelements typefindfunctions playback audioconvert audioresample audiorate \
              volume autodetect pulseaudio alsa wavparse audioparsers mpg123 id3demux apetag \
              icydemux ogg vorbis opus flac; do
    cp "$SYSLIB/gstreamer-1.0/libgst$plugin.so" "$LIB/gstreamer-1.0/"
done

# Datos: iconos Adwaita (los cursores son los del sistema) y esquemas de GSettings
# de GTK (selector de archivos…); los demás esquemas se leen del sistema
mkdir -p "$USR/share/icons/hicolor" "$USR/share/glib-2.0/schemas"
cp -a /usr/share/icons/Adwaita "$USR/share/icons/"
rm -rf "$USR/share/icons/Adwaita/cursors"
cp /usr/share/icons/hicolor/index.theme "$USR/share/icons/hicolor/"
cp /usr/share/glib-2.0/schemas/org.gtk.gtk4.* "$USR/share/glib-2.0/schemas/"
glib-compile-schemas "$USR/share/glib-2.0/schemas"

# Typelibs de GObject Introspection y bibliotecas compartidas: se copian las que
# usa la app y todo aquello de lo que dependen, salvo lo que debe venir del sistema.
python3 - "$APPDIR" "$SYSLIB" <<'PY'
import os, shutil, subprocess, sys

import gi
gi.require_version('GIRepository', '2.0')
from gi.repository import GIRepository

appdir, syslib = sys.argv[1:]
libdir = os.path.join(appdir, 'usr/lib')

# Bibliotecas que NO se empaquetan. Son la lista de exclusión de AppImage
# (https://github.com/AppImageCommunity/pkg2appimage/blob/master/excludelist):
# glibc, y lo que tiene que casar con los controladores gráficos, las fuentes
# y el sonido del sistema…
EXCLUDE = set('''
    ld-linux.so.2 ld-linux-x86-64.so.2 libanl.so.1 libBrokenLocale.so.1 libcidn.so.1 libc.so.6
    libdl.so.2 libm.so.6 libmvec.so.1 libnss_compat.so.2 libnss_dns.so.2 libnss_files.so.2
    libnss_hesiod.so.2 libnss_nisplus.so.2 libnss_nis.so.2 libpthread.so.0 libresolv.so.2
    librt.so.1 libthread_db.so.1 libutil.so.1 libstdc++.so.6 libgcc_s.so.1
    libGL.so.1 libEGL.so.1 libGLdispatch.so.0 libGLX.so.0 libOpenGL.so.0 libdrm.so.2
    libglapi.so.0 libgbm.so.1 libxcb.so.1 libX11.so.6 libX11-xcb.so.1 libwayland-client.so.0
    libasound.so.2 libfontconfig.so.1 libfreetype.so.6 libharfbuzz.so.0 libfribidi.so.0
    libcom_err.so.2 libexpat.so.1 libgpg-error.so.0 libICE.so.6 libSM.so.6 libusb-1.0.so.0
    libuuid.so.1 libz.so.1 libjack.so.0 libpipewire-0.3.so.0 libxcb-dri3.so.0 libxcb-dri2.so.0
    libgmp.so.10 libp11-kit.so.0
'''.split())
# … más las que Mesa (que es la del sistema, y más moderna) carga en este mismo
# proceso: si encontrara aquí una versión anterior podrían faltarle símbolos.
EXCLUDE |= set('''
    libwayland-cursor.so.0 libwayland-egl.so.1 libwayland-server.so.0
    libxcb-present.so.0 libxcb-sync.so.1 libxcb-xfixes.so.0 libxcb-randr.so.0 libxcb-shm.so.0
    libxcb-glx.so.0 libxshmfence.so.1 libXext.so.6 libXfixes.so.3 libXxf86vm.so.1
    libzstd.so.1 libsystemd.so.0 libudev.so.1
'''.split())


def elf_files():
    for root, _dirs, files in os.walk(appdir):
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                with open(path, 'rb') as f:
                    if f.read(4) == b'\x7fELF':
                        yield path


def ldd(path, env=None):
    """Dependencias (también las indirectas) de un binario: {soname: ruta o None si falta}."""
    out = subprocess.run(['ldd', path], capture_output=True, text=True, env=env).stdout
    deps = {}
    for line in out.splitlines():
        parts = line.split()
        if '=>' in parts:
            deps[parts[0]] = parts[2] if parts[2].startswith('/') else None
    return deps


def bundle(path):
    dest = os.path.join(libdir, os.path.basename(path))
    if not os.path.exists(dest):
        shutil.copy(path, dest)


# Typelibs de los espacios de nombres que importa la app y de sus dependencias,
# y las bibliotecas que cada uno carga con dlopen (ldd no las ve)
repo = GIRepository.Repository.get_default()
pending, seen = ['Gtk-4.0', 'Adw-1', 'Gst-1.0'], set()
os.makedirs(os.path.join(libdir, 'girepository-1.0'))
while pending:
    name = pending.pop()
    if name in seen:
        continue
    seen.add(name)
    namespace, version = name.rsplit('-', 1)
    repo.require(namespace, version, 0)
    shutil.copy(repo.get_typelib_path(namespace), os.path.join(libdir, 'girepository-1.0'))
    for soname in filter(None, (repo.get_shared_library(namespace) or '').split(',')):
        if soname not in EXCLUDE:
            bundle(os.path.join(syslib, soname))
    pending += repo.get_immediate_dependencies(namespace)

# Dependencias de todo lo copiado hasta ahora (ldd ya las da de forma recursiva)
for path in list(elf_files()):
    for soname, dep in ldd(path).items():
        if soname not in EXCLUDE:
            if dep is None:
                sys.exit(f'{path}: falta {soname} en el contenedor')
            bundle(dep)

# Comprobación: con las bibliotecas del paquete por delante, todo se resuelve
# dentro de él salvo lo excluido a propósito
env = dict(os.environ, LD_LIBRARY_PATH=libdir)
host, errors = set(), []
for path in elf_files():
    for soname, dep in ldd(path, env).items():
        if soname in EXCLUDE:
            host.add(soname)
        elif dep is None or not dep.startswith(appdir):
            errors.append(f'{path}: {soname} → {dep}')
if errors:
    sys.exit('Bibliotecas sin empaquetar:\n' + '\n'.join(errors))
print('Bibliotecas que se usan del sistema:', ' '.join(sorted(host)))
PY

# Bytecode de la app (la AppImage es de solo lectura: no se puede generar al ejecutar)
"$USR/bin/python3" -I -m compileall -q "$USR/share/wiigc-manager"

desktop-file-validate "$USR/share/applications/$APP_ID.desktop"

curl -fsSL -o /tmp/appimagetool \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
chmod +x /tmp/appimagetool
# En el contenedor no hay FUSE: appimagetool (otra AppImage) se extrae para ejecutarse
ARCH="$ARCH" VERSION="$VERSION" /tmp/appimagetool --appimage-extract-and-run \
    "$APPDIR" "/tmp/$OUTPUT"
cp "/tmp/$OUTPUT" "/out/$OUTPUT"
# Con docker el contenedor escribe como root: devolver el fichero al usuario
[[ "$ENGINE" == docker ]] && chown "$HOST_OWNER" "/out/$OUTPUT"
exit 0
