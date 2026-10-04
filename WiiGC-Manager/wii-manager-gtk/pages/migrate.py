"""
Página Migrar: copia todos los juegos de una unidad a otra (p. ej. al cambiar
a un disco de más capacidad), opcionalmente eliminándolos del origen al terminar.
"""
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

import migrate
from backend import core, run_async
from pages.library import remove_games
from widgets.operation_status import OperationStatus

GIB = 1024**3


def _name(game):
    """'Título [ID]', para los mensajes."""
    return f"{game.get('title', '?')} [{game.get('id', '?')}]"


def _count(n):
    """'1 juego' o 'N juegos'."""
    return '1 juego' if n == 1 else f'{n} juegos'


def run_migration(src, dst, delete_source, cancel, on_game):
    """
    Copia los juegos y, si se pide, elimina del origen los que se han copiado
    bien (bloqueante: llamar vía run_async). Se elimina solo al final, con la
    copia ya escrita en el disco de destino, y nunca tras una cancelación.
    Devuelve (copiados, [(game, error)], cancelado, [(game, error o None)] de la eliminación).
    """
    copied, failed, cancelled, _todo = migrate.migrate(src, dst, cancel, on_game)
    removed = remove_games(copied, src) if delete_source and copied and not cancelled else []
    return copied, failed, cancelled, removed


class MigratePage(Gtk.Box):
    """
    get_device_path() da la unidad explorada (el origen por defecto); on_migrated()
    se llama al terminar, para recargar la Videoteca.
    """

    def __init__(self, get_device_path, log, on_migrated=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(16)
        self.set_margin_end(16)
        self._get_device_path = get_device_path
        self._log = log
        self._on_migrated = on_migrated
        self._volumes = []      # [{ path, name }], en el mismo orden que los desplegables
        self._updating = False  # se están rellenando los desplegables: no reaccionar a sus cambios
        self._plan_serial = 0   # para descartar comparaciones de una selección anterior
        self._cancel = None     # threading.Event de la migración en curso

        group = self._group = Adw.PreferencesGroup(
            title='Migrar juegos a otra unidad',
            description='Copia todos los juegos de Wii y GameCube de una unidad a otra, por ejemplo a un '
                        'disco de más capacidad. Los que ya estén en el destino se omiten.')
        refresh_btn = Gtk.Button(icon_name='view-refresh-symbolic', css_classes=['flat'],
                                 valign=Gtk.Align.CENTER, tooltip_text='Volver a buscar unidades')
        refresh_btn.connect('clicked', lambda *_: self.refresh())
        group.set_header_suffix(refresh_btn)

        self._src_row = Adw.ComboRow(title='Unidad de origen')
        self._dst_row = Adw.ComboRow(title='Unidad de destino')
        for row in (self._src_row, self._dst_row):
            row.connect('notify::selected', lambda *_: self._on_selection_changed())
            group.add(row)

        self._delete_row = Adw.SwitchRow(
            title='Eliminar del origen los juegos migrados',
            subtitle='Solo los copiados correctamente, y una vez terminada toda la copia')
        group.add(self._delete_row)
        self.append(group)

        self._summary = Gtk.Label(xalign=0, wrap=True)
        self.append(self._summary)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)
        self._migrate_btn = Gtk.Button(label='Migrar', css_classes=['suggested-action'], sensitive=False)
        self._migrate_btn.connect('clicked', lambda *_: self._confirm())
        self._cancel_btn = Gtk.Button(label='Cancelar', visible=False)
        self._cancel_btn.connect('clicked', lambda *_: self._on_cancel())
        btn_row.append(self._migrate_btn)
        btn_row.append(self._cancel_btn)
        self.append(btn_row)

        self._status = OperationStatus()
        self.append(self._status)

    def is_busy(self):
        """True mientras dura una migración (no conviene cerrar la app)."""
        return self._status.busy

    def abort(self):
        """Al cerrar la app en plena migración: que la copia no siga por su cuenta."""
        if self._cancel:
            self._cancel.set()
        migrate.kill_running()

    # ── Unidades ─────────────────────────────────────────────────
    def refresh(self):
        """Vuelve a buscar las unidades conectadas y a comparar las elegidas."""
        run_async(core.api_devices, {}, on_done=lambda data: self.set_devices(data.get('devices', [])),
                  on_error=lambda e: self._set_summary(f'✗ {e}', 'error'))

    def set_devices(self, devices):
        """Rellena los dos desplegables con las unidades conectadas, conservando la selección si sigue siendo válida."""
        if self._status.busy:
            return
        selected_src, selected_dst = self._selected(self._src_row), self._selected(self._dst_row)
        vols = migrate.volumes(devices)
        # La unidad explorada puede ser una carpeta o una imagen WBFS que no sale en la lista
        current = self._get_device_path()
        if current and current not in [v['path'] for v in vols]:
            vols.append({'path': current, 'name': f'Unidad explorada — {current}'})
        paths = [v['path'] for v in vols]
        # Por defecto: de la unidad explorada a la primera de las otras
        src = selected_src if selected_src in paths else current if current in paths else (paths or [''])[0]
        dst = selected_dst if selected_dst in paths and selected_dst != src else next(
            (p for p in paths if p != src), src)

        self._updating = True
        self._volumes = vols
        for row, path in ((self._src_row, src), (self._dst_row, dst)):
            row.set_model(Gtk.StringList.new([v['name'] for v in vols]))
            if path in paths:
                row.set_selected(paths.index(path))
        self._updating = False
        self._on_selection_changed()

    def _selected(self, row):
        """Ruta de la unidad elegida en un desplegable ('' si no hay ninguna)."""
        pos = row.get_selected()
        return self._volumes[pos]['path'] if pos < len(self._volumes) else ''

    def _on_selection_changed(self):
        """Al cambiar el origen o el destino: recalcula en un hilo qué hay que copiar (migrate.plan)."""
        if self._updating:
            return
        self._plan_serial += 1
        self._migrate_btn.set_sensitive(False)
        src, dst = self._selected(self._src_row), self._selected(self._dst_row)
        if len(self._volumes) < 2:
            self._set_summary('Conecta la unidad de origen y la de destino. Las unidades FAT32/NTFS '
                              'tienen que estar montadas.')
        elif src == dst:
            self._set_summary('Elige dos unidades distintas.')
        else:
            self._set_summary('Comparando las dos unidades…')
            serial = self._plan_serial
            run_async(migrate.plan, src, dst,
                      on_done=lambda todo: self._on_planned(serial, todo),
                      on_error=lambda e: self._on_planned(serial, None, e))

    # ── Resumen de lo que se va a copiar ─────────────────────────
    def _on_planned(self, serial, todo, error=None):
        """Muestra el resumen del plan y habilita «Migrar» solo si hay juegos por copiar y caben."""
        if serial != self._plan_serial or self._status.busy:
            return
        if error:
            self._set_summary(f'✗ {error}', 'error')
            return
        games, free = todo['games'], todo['free_bytes']
        lines = []
        if games:
            lines.append(f"{_count(len(games))} por copiar ({todo['total_bytes'] / GIB:.2f} GB).")
        else:
            lines.append('No hay nada que migrar.')
        if todo['present']:
            lines.append(f"{_count(len(todo['present']))} ya en el destino: "
                         + ('se omite.' if len(todo['present']) == 1 else 'se omiten.'))
        if todo['unsupported']:
            lines.append(f"{_count(len(todo['unsupported']))} de GameCube no se pueden copiar: "
                         'el destino es una partición WBFS, que solo admite juegos de Wii.')
        if free is not None:
            lines.append(f'Espacio libre en el destino: {free / GIB:.2f} GB.')
        fits = free is None or todo['total_bytes'] <= free
        if games and not fits:
            lines.append(f"✗ No caben: faltan {(todo['total_bytes'] - free) / GIB:.2f} GB en el destino.")
        self._set_summary('\n'.join(lines), None if fits else 'error')
        self._migrate_btn.set_sensitive(bool(games) and fits)

    def _set_summary(self, text, css_class=None):
        self._summary.set_label(text)
        self._summary.remove_css_class('error')
        self._summary.remove_css_class('dim-label')
        self._summary.add_css_class(css_class or 'dim-label')

    # ── Migración ────────────────────────────────────────────────
    def _confirm(self):
        """Si se van a eliminar los juegos del origen, pide confirmación antes de empezar."""
        src, dst = self._selected(self._src_row), self._selected(self._dst_row)
        if not self._delete_row.get_active():
            self._start(src, dst, False)
            return
        dialog = Adw.AlertDialog(
            heading='¿Migrar y eliminar del origen?',
            body=f'Cuando termine la copia, los juegos migrados se eliminarán de {src}. '
                 'Esta acción no se puede deshacer.')
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('migrate', 'Migrar y eliminar')
        dialog.set_response_appearance('migrate', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda _d, response: self._start(src, dst, True) if response == 'migrate' else None)
        dialog.present(self.get_root())

    def _start(self, src, dst, delete_source):
        """Lanza la migración en un hilo y cambia «Migrar» por «Cancelar»."""
        if self._status.busy:
            return
        text = f'Migrando juegos de {src} a {dst}… Puede tardar mucho, no desconectes las unidades.'
        self._log.append(text, 'info')
        self._group.set_sensitive(False)
        self._migrate_btn.set_visible(False)
        self._cancel_btn.set_sensitive(True)
        self._cancel_btn.set_visible(True)
        self._status.start(text)
        self._cancel = threading.Event()

        def on_game(n, total, game):  # llega desde el hilo de la copia
            GLib.idle_add(self._on_game, f'Copiando {_name(game)} ({n + 1} de {total})…')

        run_async(run_migration, src, dst, delete_source, self._cancel, on_game,
                  on_done=self._on_done, on_error=lambda e: self._finish(f'✗ {e}', False))

    def _on_game(self, text):
        if self._status.busy:
            self._log.append(text, 'info')
            self._status.set_text(text)
        return GLib.SOURCE_REMOVE

    def _on_cancel(self):
        self._cancel.set()
        self._cancel_btn.set_sensitive(False)
        self._status.set_text('Cancelando… Se descarta el juego que se estaba copiando.')

    def _on_done(self, result):
        """Resultado de run_migration(): detalle al terminal y resumen a la fila de estado."""
        copied, failed, cancelled, removed = result
        for game in copied:
            self._log.append(f'✓ Copiado {_name(game)}', 'ok')
        for game, error in failed:
            self._log.append(f'✗ No se pudo copiar {_name(game)}: {error}', 'err')
        not_removed = [(game, error) for game, error in removed if error]
        for game, error in not_removed:
            self._log.append(f'✗ No se pudo eliminar del origen {_name(game)}: {error}', 'err')

        parts = ['1 juego migrado' if len(copied) == 1 else f'{len(copied)} juegos migrados']
        if removed:
            parts.append(f'{len(removed) - len(not_removed)} eliminados del origen')
        problems = failed + not_removed
        if problems:
            names = ', '.join(_name(game) for game, _e in problems[:5]) + ('…' if len(problems) > 5 else '')
            parts.append(f'{len(problems)} con errores ({names}); detalles en el terminal')
        text = ' · '.join(parts)
        if cancelled:
            self._finish(f'Migración cancelada: {text}', False if problems else None)
        else:
            self._finish(('✗ ' if problems else '✓ ') + text, not problems)

    def _finish(self, text, ok):
        """Restaura la página, avisa a la ventana y vuelve a comparar las unidades."""
        self._log.append(text, 'ok' if ok else 'err' if ok is False else 'info')
        self._group.set_sensitive(True)
        self._migrate_btn.set_visible(True)
        self._cancel_btn.set_visible(False)
        self._status.finish(text, ok)
        if self._on_migrated:
            self._on_migrated()
        self.refresh()
