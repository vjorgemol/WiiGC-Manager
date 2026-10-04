"""
Carga wii-manager-server.py como módulo Python sin arrancar su servidor HTTP,
y expone run_async() para ejecutar sus funciones bloqueantes (subprocess, I/O,
descargas) en un hilo aparte sin congelar la ventana GTK.

wii-manager-server.py ya expone su lógica como funciones puras api_xxx(params)
-> dict (no dependen del objeto HTTP request), así que se reutilizan tal cual.
No se usan ni Handler ni main() de ese fichero (arrancan el servidor, no hace
falta: aquí llamamos a las funciones directamente, en el mismo proceso).
"""
import importlib.util
import threading
from pathlib import Path

from gi.repository import GLib

_SERVER_PATH = Path(__file__).resolve().parent.parent / 'wii-manager-server.py'

# El nombre del fichero lleva guiones y no se puede importar con «import»: se
# carga por su ruta. El resto de la app lo usa como core.api_xxx(params).
_spec = importlib.util.spec_from_file_location('wii_manager_core', _SERVER_PATH)
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)


def run_async(blocking_fn, *args, on_done=None, on_error=None, **kwargs):
    """
    Ejecuta blocking_fn(*args, **kwargs) en un hilo. El resultado (o la
    excepción) se entrega de vuelta en el hilo principal de GTK vía
    GLib.idle_add, que es el único hilo desde el que se puede tocar la UI.
    Equivalente a los await apiGet()/apiPost() del frontend web.
    """
    def worker():
        try:
            result = blocking_fn(*args, **kwargs)
        except Exception as e:
            if on_error:
                GLib.idle_add(on_error, e)
            return
        if on_done:
            GLib.idle_add(on_done, result)

    threading.Thread(target=worker, daemon=True).start()
