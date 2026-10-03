"""
"Añadir juego de Wii" y "Añadir juego GameCube" de la página Videoteca
(se elige la imagen en el explorador de archivos y se añade directamente a
la unidad explorada), y el selector de archivos compartido por las demás páginas.
"""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio

from pathlib import Path

import gametdb
from backend import core, run_async


def pick_file(root, entry_row, filters=None, on_picked=None, title=None):
    """Abre Gtk.FileDialog nativo y escribe la ruta elegida en entry_row (si se pasa uno)."""
    dialog = Gtk.FileDialog(title=title) if title else Gtk.FileDialog()
    if filters:
        name, patterns = filters[0]
        gtk_filter = Gtk.FileFilter()
        gtk_filter.set_name(name)
        for p in patterns:
            gtk_filter.add_pattern(p)
        filter_store = Gio.ListStore.new(Gtk.FileFilter)
        filter_store.append(gtk_filter)
        dialog.set_filters(filter_store)
        dialog.set_default_filter(gtk_filter)

    def on_response(dlg, result):
        try:
            f = dlg.open_finish(result)
        except Exception:
            return
        if f:
            path = f.get_path()
            if entry_row:
                entry_row.set_text(path)
            if on_picked:
                on_picked(path)

    dialog.open(root, None, on_response)


def _disc_text(meta):
    """'Disco 1 de 2', 'Disco único'… a partir de la cabecera de la imagen y de GameTDB."""
    disc, total = meta.get('disc'), gametdb.disc_count(meta.get('id'))
    if not disc:
        return f'Multidisco ({total} discos)' if total > 1 else ''
    if total > 1 or disc > 1:
        return f'Disco {disc} de {max(total, disc)}'
    return 'Disco único'


def _preview_text(meta):
    size = meta.get('size_gb')
    parts = [meta.get('title', '—'), f"ID: {meta.get('id', '—')}", f"Región: {meta.get('region', '—')}",
             f'{size:.2f} GB' if size else '—', _disc_text(meta)]
    return '  ·  '.join(p for p in parts if p)


def _pick_image(root, current_path, log, title, filters, inspect, add):
    """
    Elige una imagen con el explorador de archivos, la inspecciona y, si es
    válida, llama a add(src, meta). El destino es siempre la unidad explorada.
    """
    if not current_path:
        log.append('✗ Explora primero la unidad a la que quieres añadir el juego', 'err')
        return

    def on_picked(src):
        def on_inspected(meta):
            if not meta.get('valid'):
                log.append(f"✗ {Path(src).name}: {meta.get('error', 'Imagen no válida')}", 'err')
                return
            log.append(f'Añadiendo {_preview_text(meta)}… Puede tardar varios minutos, '
                       'no desconectes la unidad.', 'info')
            add(src, meta)

        log.append(f'Comprobando {Path(src).name}…', 'info')
        run_async(inspect, src, on_done=on_inspected, on_error=lambda e: log.append(f'✗ {e}', 'err'))

    pick_file(root, None, filters, on_picked=on_picked, title=title)


# ── Añadir juego Wii ─────────────────────────────────────────────────

def open_add_game_dialog(root, current_path, log, on_added=None):
    def add(src, _meta):
        def on_done(data):
            if data.get('rc') == 0:
                log.append(f'✓ Juego añadido: {Path(src).name}', 'ok')
                if on_added:
                    on_added()
            else:
                log.append(f"✗ No se pudo añadir el juego (código {data.get('rc')}): "
                           f"{data.get('error') or data.get('stderr', '')}", 'err')

        run_async(core.api_add, {'part': current_path, 'src': src}, on_done=on_done,
                  on_error=lambda e: log.append(f'✗ {e}', 'err'))

    _pick_image(root, current_path, log, 'Añadir juego de Wii',
                [('Imágenes Wii', ['*.iso', '*.wbfs', '*.wdf', '*.rvz', '*.gcz'])], core.inspect_wii_file, add)


# ── Añadir juego GameCube ────────────────────────────────────────────

def open_add_gc_dialog(root, current_path, log, on_added=None):
    def add(src, meta):
        # Número de disco leído de la propia imagen
        disc = meta['disc'] if meta.get('disc') in (1, 2) else 1

        def on_done(data):
            if data.get('success'):
                disc = data.get('disc') or 1  # el de verdad: en RVZ/GCZ no se sabe hasta convertirla
                total = gametdb.disc_count(data.get('id'))
                # Avisar del otro disco solo si aún no está en la carpeta del juego
                other_stem = 'disc2' if disc == 1 else 'game'
                try:
                    has_other = any(f.stem.lower() == other_stem for f in Path(data.get('dest_file', '')).parent.iterdir())
                except OSError:
                    has_other = False
                other = (f' Este juego tiene {total} discos: añade también el disco {2 if disc == 1 else 1}.'
                         if total > 1 and not has_other else '')
                log.append(f"✓ \"{data.get('title')}\" [{data.get('id')}]"
                           + (f' (disco {disc})' if total > 1 or disc > 1 else '') + f' añadido.{other}', 'ok')
                if on_added:
                    on_added()
            else:
                log.append(f"✗ {data.get('error', 'Error desconocido')}", 'err')

        run_async(core.api_gc_add, {'src': src, 'dest': current_path, 'disc': disc, 'id': meta.get('id', '')},
                  on_done=on_done, on_error=lambda e: log.append(f'✗ {e}', 'err'))

    _pick_image(root, current_path, log, 'Añadir juego GameCube',
                [('Imágenes GameCube', ['*.iso', '*.gcm', '*.ciso', '*.rvz', '*.gcz'])], core.inspect_gamecube_file, add)
