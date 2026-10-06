"""
Entorno de la AppImage. Python importa este módulo al arrancar, antes que la
app: apunta GTK, GObject Introspection, GStreamer y OpenSSL a los ficheros que
van dentro del paquete, y hace que los programas que lanza la app (wit, wwt,
lsblk, mkfs, pkexec, Dolphin…) reciban el entorno original del sistema, no el
del paquete: con las bibliotecas de la AppImage por delante podrían no arrancar.
"""
import atexit
import os
import subprocess
import tempfile
from pathlib import Path

_USR = Path(__file__).resolve().parents[2]      # <AppDir>/usr
_LIB = _USR / 'lib'

# Variables cambiadas para este proceso → su valor original (None: no existía)
_original = {}


def _set(name, value):
    _original.setdefault(name, os.environ.get(name))
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = str(value)


def _host_env():
    """El entorno de este proceso con las variables del paquete devueltas a su valor original."""
    env = dict(os.environ)
    for name, value in _original.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _pixbuf_loaders_cache():
    """
    gdk-pixbuf lee la lista de cargadores (el de SVG, para los iconos) de un
    fichero con rutas absolutas, y la AppImage se monta cada vez en un sitio
    distinto: se genera al arrancar a partir de la plantilla del paquete.
    """
    template = (_LIB / 'gdk-pixbuf-2.0' / 'loaders.cache.in').read_text()
    fd, path = tempfile.mkstemp(prefix='wiigc-manager-loaders-', suffix='.cache',
                                dir=os.environ.get('XDG_RUNTIME_DIR') or None)
    with os.fdopen(fd, 'w') as f:
        f.write(template.replace('@LIBDIR@', str(_LIB)))
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))
    return path


def _ca_bundle():
    """Certificados raíz del sistema: el OpenSSL del paquete solo conoce la ruta de Ubuntu."""
    for path in ('/etc/ssl/certs/ca-certificates.crt',      # Debian, Ubuntu
                 '/etc/pki/tls/certs/ca-bundle.crt',        # Fedora, RHEL
                 '/etc/ssl/ca-bundle.pem',                  # openSUSE
                 '/etc/ssl/cert.pem'):                      # Arch, Alpine
        if os.path.isfile(path):
            return path
    return None


def _has_gles():
    """Si el sistema tiene libGLESv2 (en Fedora va en libglvnd-gles, que no siempre está instalado)."""
    return any(os.path.exists(os.path.join(d, 'libGLESv2.so.2'))
               for d in ('/usr/lib64', '/usr/lib/%s-linux-gnu' % os.uname().machine, '/usr/lib', '/lib64'))


def _setup():
    # AppRun puso delante las bibliotecas del paquete. El enlazador dinámico ya
    # leyó LD_LIBRARY_PATH al arrancar y la sigue usando en este proceso aunque
    # se quite del entorno, así que los hijos ya no la heredan.
    host_ld = os.environ.pop('WIIGC_ORIG_LD_LIBRARY_PATH', '')
    if host_ld:
        os.environ['LD_LIBRARY_PATH'] = host_ld
    else:
        os.environ.pop('LD_LIBRARY_PATH', None)

    _set('GI_TYPELIB_PATH', _LIB / 'girepository-1.0')
    # Módulos de GIO y de GTK (impresión): solo los del paquete, nunca los del
    # sistema, que están compilados contra otra versión de GLib/GTK
    _set('GIO_MODULE_DIR', _LIB / 'gio' / 'modules')
    _set('GIO_EXTRA_MODULES', None)
    _set('GTK_EXE_PREFIX', _USR)
    _set('GTK_PATH', None)
    _set('GDK_PIXBUF_MODULE_FILE', _pixbuf_loaders_cache())
    # Iconos Adwaita y esquemas de GSettings de GTK; detrás, los datos del sistema
    _set('XDG_DATA_DIRS', '%s:%s' % (_USR / 'share',
                                     os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share'))

    # Sin libGLESv2 en el sistema, GTK no debe intentar usar OpenGL ES: libepoxy
    # aborta el programa al no encontrarla. Se queda con OpenGL o con Cairo.
    if not _has_gles():
        _set('GDK_DEBUG', ','.join(filter(None, [os.environ.get('GDK_DEBUG'), 'gl-disable-gles'])))

    _set('GST_PLUGIN_SYSTEM_PATH_1_0', _LIB / 'gstreamer-1.0')
    _set('GST_PLUGIN_PATH_1_0', None)
    _set('GST_PLUGIN_PATH', None)
    # Sin gst-plugin-scanner: sería un proceso hijo, que no ve las bibliotecas del paquete
    _set('GST_REGISTRY_FORK', 'no')
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    _set('GST_REGISTRY_1_0', cache / 'wii-manager-gtk' / 'gst-registry-appimage.bin')

    if 'SSL_CERT_FILE' not in os.environ and _ca_bundle():
        _set('SSL_CERT_FILE', _ca_bundle())

    popen_init = subprocess.Popen.__init__

    def init(self, *args, **kwargs):
        if kwargs.get('env') is None:
            kwargs['env'] = _host_env()
        popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = init


_setup()
