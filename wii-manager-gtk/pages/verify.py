"""
Página Verificar: comprobación de integridad de un juego o de toda la
partición. Equivalente a view-verify + verifyGame()/verifyAll() del
frontend web.
"""
import re

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from pathlib import Path

import dialogs
import gc_verify
from backend import core, run_async
from widgets.operation_status import OperationStatus
from widgets.path_row import PathRow

# En la salida de «verificar todos», las líneas con un ID de juego son las de
# resultado; de ellas, las que mencionan un error son las de los juegos dañados
_GAME_ID_RE = re.compile(r'[A-Z0-9]{4,6}')
_ERROR_RE = re.compile(r'error|bad|fail', re.IGNORECASE)
# Última opción del desplegable: verificar un archivo suelto en vez de un juego de la unidad
_OTHER_FILE = 'Otro archivo (ISO / WBFS / GCM)…'
_GC_EXTENSIONS = ('.iso', '.gcm', '.ciso')


# Las funciones verify_* devuelven (filas, salidas): filas = [(ok, texto)], con
# ok True/False/None (None: no se ha podido comprobar); salidas = respuestas de
# core.api_verify, para volcar el comando y su salida en el terminal.

def _wii_rows(data, name):
    """Filas de resultado a partir de la respuesta de core.api_verify."""
    if data.get('error'):
        return [(False, f"{name}: {data['error']}")]
    if data.get('rc') == 0:
        return [(True, name)]
    stderr = data.get('stderr') or ''
    if 'WRONG FILE TYPE' in stderr or 'No valid source' in stderr:
        return [(False, f'{name}: no es una imagen de Wii ni de GameCube; no se puede verificar')]
    return [(False, f'{name}: la verificación ha encontrado errores')]


def verify_file(path):
    """Verifica un archivo suelto: GameCube contra GameTDB, Wii con wit."""
    name = Path(path).name
    if not Path(path).is_file():
        return [(False, f'{name}: el archivo no existe')], []
    if gc_verify.is_gamecube_image(path):
        result = gc_verify.verify(path)
        return [(result['ok'], result['message'])], []
    data = core.api_verify({'src': path})
    return _wii_rows(data, name), [data]


def verify_gc_game(game):
    """Verifica los discos de un juego GameCube de la unidad (game.iso y, si existe, disc2.iso)."""
    name = f"{game.get('title', '?')} [{game.get('id', '?')}]"
    files = []
    folder = Path(game.get('folder') or '')
    if game.get('folder') and folder.is_dir():
        for stem in ('game', 'disc2'):
            files += sorted(f for f in folder.iterdir()
                            if f.stem.lower() == stem and f.suffix.lower() in _GC_EXTENSIONS)
    if not files and game.get('path'):
        files = [Path(game['path'])]
    rows = []
    for f in files:
        label = name + (' (disco 2)' if f.stem.lower() == 'disc2' else '')
        result = gc_verify.verify(str(f), label)
        rows.append((result['ok'], result['message']))
    return (rows or [(False, f'{name}: no se encontró la imagen del juego')]), []


def verify_wii_game(game, part):
    """Verifica un juego de Wii de la unidad explorada (part) por su ID."""
    name = f"{game.get('title', '?')} [{game['id']}]"
    data = core.api_verify({'part': part, 'id': game['id']})
    return _wii_rows(data, name), [data]


def verify_all(games, part):
    """Verifica todos los juegos de la unidad: los de Wii con wit/wwt y los de GameCube contra GameTDB."""
    rows, outputs = [], []
    gc_games = [g for g in games if g.get('platform') == 'gc']
    if len(gc_games) < len(games) or not games:
        data = core.api_verify({'part': part})
        outputs.append(data)
        # Líneas de resultado por juego; se descarta la cabecera de versión de wit/wwt ("***** wit: …")
        lines = [l for l in (data.get('stdout') or '').splitlines()
                 if _GAME_ID_RE.search(l) and not l.lstrip().startswith('*')]
        if lines:
            rows += [(not _ERROR_RE.search(l), l.strip()) for l in lines]
        else:
            rows += _wii_rows(data, 'Juegos de Wii')
    for game in gc_games:
        rows += verify_gc_game(game)[0]
    return rows, outputs


class VerifyPage(Gtk.Box):
    """get_games() devuelve los juegos de la última exploración de la Videoteca."""

    def __init__(self, get_device_path, log, get_games=lambda: []):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(16)
        self.set_margin_end(16)
        self._get_device_path = get_device_path
        self._log = log
        self._get_games = get_games
        self._games = []  # juegos del desplegable, en el mismo orden

        group = Adw.PreferencesGroup(title='Verificar integridad')
        self._game_row = Adw.ComboRow(title='Juego', model=Gtk.StringList.new([_OTHER_FILE]))
        self._game_row.connect('notify::selected', lambda *_: self._on_game_selected())
        group.add(self._game_row)

        self._path_row = PathRow('Ruta del archivo', lambda row: dialogs.pick_file(
            self.get_root(), row, [('Imágenes Wii / GameCube', ['*.iso', '*.wbfs', '*.wdf', '*.gcm', '*.ciso'])]),
            placeholder='Ningún archivo elegido', clearable=True)
        group.add(self._path_row)

        btn_row = self._btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                                               halign=Gtk.Align.CENTER)
        verify_btn = Gtk.Button(label='Verificar', css_classes=['suggested-action'])
        verify_btn.connect('clicked', lambda *_: self._verify_one())
        verify_all_btn = Gtk.Button(label='Verificar todos')
        verify_all_btn.connect('clicked', lambda *_: self._verify_all())
        btn_row.append(verify_btn)
        btn_row.append(verify_all_btn)
        btn_row.set_margin_top(12)
        group.add(btn_row)
        self.append(group)

        self._status = OperationStatus()
        self.append(self._status)

        self._results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.append(self._results_box)

    def refresh_games(self):
        """
        Rellena el desplegable con los juegos de la Videoteca. Los de Wii se
        verifican con wit/wwt (sus discos llevan sumas SHA1 propias); los de
        GameCube, comparando su SHA1 con los volcados conocidos de GameTDB.
        """
        selected = self._selected_game()
        self._games = [g for g in self._get_games() if g.get('id')]
        labels = [f"{g.get('title', '?')} [{g['id']}]" + (' · GameCube' if g.get('platform') == 'gc' else '')
                  for g in self._games]
        self._game_row.set_model(Gtk.StringList.new(labels + [_OTHER_FILE]))
        keys = [(g.get('platform'), g['id']) for g in self._games]
        if selected and (selected.get('platform'), selected['id']) in keys:
            self._game_row.set_selected(keys.index((selected.get('platform'), selected['id'])))
        self._game_row.set_subtitle('' if self._games else 'No hay juegos en la unidad explorada')
        self._on_game_selected()

    def _selected_game(self):
        """Juego elegido en el desplegable, o None si es «Otro archivo…»."""
        pos = self._game_row.get_selected()
        return self._games[pos] if pos < len(self._games) else None

    def _on_game_selected(self):
        self._path_row.set_visible(self._selected_game() is None)

    def _verify_one(self):
        """Verifica el juego elegido o, con «Otro archivo…», la imagen de la fila de ruta."""
        game = self._selected_game()
        if game:
            name = f"{game.get('title', '?')} [{game['id']}]"
            if game.get('platform') == 'gc':
                task = (verify_gc_game, game)
            else:
                task = (verify_wii_game, game, self._get_device_path())
        else:
            name = self._path_row.get_text().strip()
            if not name:
                self._log.append('✗ Selecciona un juego o indica la ruta de un archivo', 'err')
                return
            task = (verify_file, name)
        self._start(f'Verificando {name}…')
        run_async(*task, on_done=self._on_result, on_error=self._on_error)

    def _start(self, text):
        """Bloquea los botones, borra los resultados anteriores y muestra la operación en curso."""
        self._log.append(text, 'info')
        self._btn_row.set_sensitive(False)
        self._render_results([])
        self._status.start(text)

    def _finish(self, text, ok):
        self._btn_row.set_sensitive(True)
        self._status.finish(text, ok)

    def _on_error(self, error):
        self._log.append(f'✗ {error}', 'err')
        self._finish(f'✗ {error}', False)

    def _verify_all(self):
        path = self._get_device_path()
        self._start(f'Verificando todos los juegos en {path or "(auto)"}…')
        run_async(verify_all, self._get_games(), path, on_done=self._on_result, on_error=self._on_error)

    def _on_result(self, result):
        """Resultado de cualquiera de las verify_*: detalle al terminal y resumen a la página."""
        rows, outputs = result
        for data in outputs:
            self._log_result(data)
        for ok, text in rows:
            self._log.append(('✓ ' if ok else '✗ ' if ok is False else '? ') + text, 'ok' if ok else 'err')
        if any(ok is False for ok, _ in rows):
            self._finish('✗ La verificación ha encontrado problemas', False)
        elif any(ok is None for ok, _ in rows):
            self._finish('Verificación terminada: hay imágenes que no se han podido comprobar', None)
        else:
            self._finish('✓ Verificación correcta', True)
        self._render_results(rows)

    def _log_result(self, data):
        """Comando y salida de wit/wwt, al terminal."""
        if data.get('cmd'):
            self._log.append(f"$ {data['cmd']}", 'cmd')
        if data.get('stdout'):
            self._log.append(data['stdout'])
        if data.get('stderr'):
            self._log.append(data['stderr'], 'err')

    def _render_results(self, rows):
        """Una tarjeta por fila de resultado, con su etiqueta OK / ERR / N/D."""
        while child := self._results_box.get_first_child():
            self._results_box.remove(child)
        for ok, text in rows:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.add_css_class('card')
            row.set_margin_top(2)
            row.set_margin_bottom(2)
            # ok es None cuando la imagen no se ha podido comprobar (ni bien ni mal)
            tag = Gtk.Label(label='OK' if ok else 'ERR' if ok is False else 'N/D')
            tag.add_css_class('caption-heading')
            tag.add_css_class('success' if ok else 'error' if ok is False else 'warning')
            tag.set_margin_start(8)
            label = Gtk.Label(label=text, xalign=0, hexpand=True, wrap=True)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_end(8)
            row.append(tag)
            row.append(label)
            self._results_box.append(row)
