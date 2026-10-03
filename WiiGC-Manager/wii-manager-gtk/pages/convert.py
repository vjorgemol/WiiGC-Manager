"""
Página Convertir: 3 pestañas (ISO→WBFS, WBFS→ISO, Lote). Equivalente a
view-convert + convertIsoToWfs()/convertWfsToIso()/runBatch() del frontend web.
"""
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio

import dialogs
from backend import core, run_async
from widgets.operation_status import OperationStatus
from widgets.path_row import PathRow

SAME_DIR = 'Mismo directorio y nombre que la imagen de origen'
IMAGE_SUFFIXES = {'.iso', '.wbfs', '.wdf', '.ciso', '.wia', '.gcm'}


def with_image_suffix(path, suffix):
    """La ruta con la extensión del formato de salida: sustituye la de imagen que tuviera, o la añade."""
    p = Path(path)
    return str(p.with_suffix(suffix) if p.suffix.lower() in IMAGE_SUFFIXES else p.with_name(p.name + suffix))


def convert_batch(src_dir, dst):
    """
    Convierte a WBFS todos los ISOs de src_dir, cada uno con el nombre de su
    ISO: en la carpeta dst o, sin destino, junto al propio ISO.
    Devuelve { rc (nº de fallos), stdout, stderr } como el resto de operaciones.
    """
    isos = sorted(f for f in Path(src_dir).iterdir() if f.is_file() and f.suffix.lower() == '.iso')
    if not isos:
        return {'error': f'No hay archivos .iso en {src_dir}'}
    done, failed = [], []
    for iso in isos:
        dest = Path(dst or iso.parent) / f'{iso.stem}.wbfs'
        if dest.exists():
            data = {'error': f'ya existe {dest}'}
        else:
            data = core.api_extract({'id': str(iso), 'dest': str(dest), 'opts': '--wbfs'})
        if data.get('rc') == 0:
            done.append(f'✓ {iso.name}')
        else:
            reason = (data.get('error') or data.get('stderr') or '').strip().splitlines()
            failed.append(f"✗ {iso.name}: {reason[-1].strip() if reason else 'error'}")
    return {'rc': len(failed), 'stdout': '\n'.join(done), 'stderr': '\n'.join(failed)}


class ConvertPage(Gtk.Box):
    def __init__(self, get_device_path, log):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(16)
        self.set_margin_end(16)
        self._get_device_path = get_device_path
        self._log = log

        view_stack = Adw.ViewStack()
        view_stack.add_titled_with_icon(self._build_iso2wfs_tab(), 'iso2wfs', 'ISO → WBFS', 'drive-harddisk-symbolic')
        view_stack.add_titled_with_icon(self._build_wfs2iso_tab(), 'wfs2iso', 'WBFS → ISO', 'media-optical-symbolic')
        view_stack.add_titled_with_icon(self._build_batch_tab(), 'batch', 'Lote', 'folder-symbolic')

        switcher = Adw.ViewSwitcher(stack=view_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.append(switcher)
        self.append(view_stack)
        self._view_stack = view_stack

        self._status = OperationStatus()
        self.append(self._status)

    def _start(self, text):
        """Arranca una conversión: una a la vez, con su barra de progreso."""
        if self._status.busy:
            return False
        self._log.append(text, 'info')
        self._view_stack.set_sensitive(False)
        self._status.start(text)
        return True

    def _finish(self, text, ok):
        self._log.append(text, 'ok' if ok else 'err')
        self._view_stack.set_sensitive(True)
        self._status.finish(text, ok)

    # ── Tab 1: ISO → WBFS ────────────────────────────────────────
    def _build_iso2wfs_tab(self):
        group = Adw.PreferencesGroup(title='Convertir ISO → WBFS/WFS')

        src_row = PathRow('Archivo ISO origen', lambda row: dialogs.pick_file(
            self.get_root(), row, [('Imágenes Wii', ['*.iso', '*.wbfs', '*.wdf'])]))
        group.add(src_row)

        dst_row = PathRow('Archivo WBFS destino', lambda row: self._pick_save_file(row, src_row, '.wbfs'),
                          placeholder=SAME_DIR, clearable=True)
        group.add(dst_row)

        opts_row = Adw.ComboRow(title='Opciones')
        opts_model = Gtk.StringList.new(['Sin compresión', 'Preserve scrub', 'Split en varios archivos'])
        opts_row.set_model(opts_model)
        group.add(opts_row)
        opts_values = ['', '--preserve-scrub', '--split']

        convert_btn = Gtk.Button(label='Convertir', css_classes=['suggested-action'])
        convert_btn.connect('clicked', lambda *_: self._convert_iso_to_wfs(
            src_row.get_text(), dst_row.get_text(), opts_values[opts_row.get_selected()]))
        convert_btn.set_margin_top(12)
        group.add(convert_btn)
        return group

    def _convert_iso_to_wfs(self, src, dst, opts):
        if not src:
            self._log.append('✗ Elige la imagen de origen', 'err')
            return
        dest = dst or self._beside_source(src, '.wbfs')
        if not dest:
            return
        split = ' --split' if opts == '--split' else ''
        task = (core.api_extract, {'id': src, 'dest': dest, 'opts': '--wbfs' + split})
        text = f'Convirtiendo {Path(src).name} → {dest}'
        if not self._start(f'{text}, puede tardar varios minutos…'):
            return
        run_async(*task, on_done=self._on_convert_done('ISO convertida correctamente'),
                  on_error=lambda e: self._finish(f'✗ {e}', False))

    # ── Tab 2: WBFS → ISO ────────────────────────────────────────
    def _build_wfs2iso_tab(self):
        group = Adw.PreferencesGroup(title='Convertir WBFS → ISO')

        src_row = PathRow('Imagen WBFS origen', lambda row: dialogs.pick_file(
            self.get_root(), row, [('Imágenes Wii', ['*.iso', '*.wbfs', '*.wdf'])]))
        group.add(src_row)

        opts_values = ['', '--wbfs', '--split']
        suffix = lambda: '.wbfs' if opts_values[opts_row.get_selected()] == '--wbfs' else '.iso'
        dst_row = PathRow('Archivo destino', lambda row: self._pick_save_file(row, src_row, suffix()),
                          placeholder=SAME_DIR, clearable=True)
        group.add(dst_row)

        opts_row = Adw.ComboRow(title='Opciones')
        opts_model = Gtk.StringList.new(['ISO estándar', 'WBFS en lugar de ISO', 'ISO partida (FAT32)'])
        opts_row.set_model(opts_model)
        # La extensión del destino ya elegido sigue al formato de salida
        opts_row.connect('notify::selected', lambda *_: self._set_destination(dst_row, dst_row.get_text(), suffix()))
        group.add(opts_row)

        convert_btn = Gtk.Button(label='Convertir', css_classes=['suggested-action'])
        convert_btn.connect('clicked', lambda *_: self._convert_wfs_to_iso(
            src_row.get_text(), dst_row.get_text(), opts_values[opts_row.get_selected()]))
        convert_btn.set_margin_top(12)
        group.add(convert_btn)
        return group

    def _convert_wfs_to_iso(self, src, dst, opts):
        if not src:
            self._log.append('✗ Elige la imagen de origen', 'err')
            return
        dst = dst or self._beside_source(src, '.wbfs' if opts == '--wbfs' else '.iso')
        if not dst:
            return
        if not self._start(f'Convirtiendo {Path(src).name} → {dst}…'):
            return
        run_async(core.api_extract, {'part': self._get_device_path(), 'id': src, 'dest': dst, 'opts': opts},
                  on_done=self._on_convert_done('ISO exportada correctamente'),
                  on_error=lambda e: self._finish(f'✗ {e}', False))

    # ── Tab 3: Lote ──────────────────────────────────────────────
    def _build_batch_tab(self):
        group = Adw.PreferencesGroup(
            title='Conversión en lote',
            description='Convierte todos los ISOs de un directorio a WBFS de una vez.')

        src_row = PathRow('Directorio con ISOs', self._pick_folder)
        group.add(src_row)

        dst_row = PathRow('Carpeta destino', self._pick_folder,
                          placeholder='Mismo directorio que los ISOs', clearable=True)
        group.add(dst_row)

        run_btn = Gtk.Button(label='Ejecutar lote', css_classes=['suggested-action'])
        run_btn.connect('clicked', lambda *_: self._run_batch(src_row.get_text(), dst_row.get_text()))
        run_btn.set_margin_top(12)
        group.add(run_btn)
        return group

    def _run_batch(self, src, dst):
        if not src:
            self._log.append('✗ Elige el directorio con los ISOs', 'err')
            return
        if not self._start('Ejecutando lote, puede tardar varios minutos…'):
            return
        run_async(convert_batch, src, dst,
                  on_done=self._on_convert_done('Lote completado'),
                  on_error=lambda e: self._finish(f'✗ {e}', False))

    # ── Helpers ──────────────────────────────────────────────────
    def _on_convert_done(self, ok_message):
        def handler(data):
            if data.get('error'):
                self._finish(f"✗ {data['error']}", False)
            elif data.get('rc') == 0:
                self._finish(f'✓ {ok_message}', True)
            else:
                reason = (data.get('stderr') or '').strip().splitlines()
                self._finish(f"✗ Código de error {data.get('rc')}" + (f': {reason[-1].strip()}' if reason else ''), False)
            if data.get('stdout'):
                self._log.append(data['stdout'])
            if data.get('stderr'):
                self._log.append(data['stderr'], 'err')
        return handler

    def _beside_source(self, src, suffix):
        """Destino por defecto: junto a la imagen de origen. None (y aviso) si ya existe."""
        dest = Path(src).with_suffix(suffix)
        if dest.exists():
            self._log.append(f'✗ Ya existe {dest}: elige otro destino', 'err')
            return None
        return str(dest)

    def _pick_folder(self, entry_row):
        dialog = Gtk.FileDialog()

        def on_response(dlg, result):
            try:
                f = dlg.select_folder_finish(result)
            except Exception:
                return
            if f:
                entry_row.set_text(f.get_path())

        dialog.select_folder(self.get_root(), None, on_response)

    def _set_destination(self, row, path, suffix):
        """Pone en la fila el destino elegido, con la extensión del formato de salida."""
        if not path:
            return
        dest = with_image_suffix(path, suffix)
        # El selector de archivos solo ha confirmado sobrescribir el nombre tal como se eligió
        if dest != path and Path(dest).exists():
            self._log.append(f'✗ Ya existe {dest}: elige otro destino', 'err')
            dest = ''
        row.set_text(dest)

    def _pick_save_file(self, row, src_row, suffix):
        """Guardar como…, proponiendo el nombre (y la carpeta) de la imagen de origen."""
        src = src_row.get_text()
        dialog = Gtk.FileDialog()
        if src:
            dialog.set_initial_name(Path(src).stem + suffix)
            dialog.set_initial_folder(Gio.File.new_for_path(str(Path(src).parent)))

        def on_response(dlg, result):
            try:
                f = dlg.save_finish(result)
            except Exception:
                return
            if f:
                self._set_destination(row, f.get_path(), suffix)

        dialog.save(self.get_root(), None, on_response)
