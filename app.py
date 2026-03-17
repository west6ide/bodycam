import hashlib
import json
import logging
import os
import queue
import atexit
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import psutil
import requests
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = 'Bodycam Uploader'
BASE_DIR = Path.home() / 'BodycamUploader'
BASE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = BASE_DIR / 'config.json'
DB_PATH = BASE_DIR / 'state.db'
LOG_PATH = BASE_DIR / 'logs'
STAGING_DIR = BASE_DIR / 'staging'
ARCHIVE_DIR = BASE_DIR / 'archive'
LOCK_PATH = BASE_DIR / 'app.lock'

for p in [LOG_PATH, STAGING_DIR, ARCHIVE_DIR]:
    p.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    'server_url': 'https://backend.paraqlab.kz/tracker-api/api/communication/jobs',
    'api_token': '',
    'camera_label_keywords': ['BODYCAM', 'CAMERA', 'BWC', 'RECORDER'],
    'camera_folder_names': ['100VOICE', 'DCIM', 'AUDIO', 'Audio', 'audio', 'RECORDINGS', 'Recordings', 'records'],
    'audio_extensions': ['.wav', '.mp3', '.m4a', '.aac', '.ogg', '.wma'],
    'scan_interval_seconds': 5,
    'auto_start_upload': True,
    'delete_from_camera_after_upload': True,
    'retain_local_archive_days': 7,
    'verify_ssl': True,
    'device_poll_enabled': True,
    'employee_mode': 'computer_name',
    'employee_name': '',
    'store_name': 'Store01',
    'camera_id_from_drive_label': True,
    'max_parallel_uploads': 1,
    'request_timeout_seconds': 60,
    'microphone_device_name': 'Camera1',
    'language_hint': 'ru',
    'output_language': 'ru',
    'server_metadata_headers': {
        'accept': 'application/json'
    }
}


def ensure_config() -> Dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding='utf-8')
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)
    if merged.get('server_url') == 'http://127.0.0.1:5001/upload':
        merged['server_url'] = DEFAULT_CONFIG['server_url']
    if merged.get('server_metadata_headers') == {'X-Store-Name': 'Store01'}:
        merged['server_metadata_headers'] = DEFAULT_CONFIG['server_metadata_headers'].copy()
    if not merged.get('microphone_device_name'):
        merged['microphone_device_name'] = DEFAULT_CONFIG['microphone_device_name']
    if merged != cfg:
        CONFIG_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding='utf-8')
    return merged


def setup_logging() -> None:
    log_file = LOG_PATH / f"app_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )


class SingleInstance:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.acquired = False

    def acquire(self) -> bool:
        try:
            if self.lock_path.exists():
                try:
                    pid = int(self.lock_path.read_text(encoding='utf-8').strip())
                except ValueError:
                    pid = 0
                if pid and psutil.pid_exists(pid):
                    return False
                self.lock_path.unlink(missing_ok=True)
            self.lock_path.write_text(str(os.getpid()), encoding='utf-8')
            self.acquired = True
            atexit.register(self.release)
            return True
        except OSError:
            return False

    def release(self):
        if not self.acquired:
            return
        try:
            if self.lock_path.exists():
                current = self.lock_path.read_text(encoding='utf-8').strip()
                if current == str(os.getpid()):
                    self.lock_path.unlink()
        except OSError:
            pass
        self.acquired = False


class StateDB:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute(
                '''CREATE TABLE IF NOT EXISTS uploaded_files (
                    sha256 TEXT PRIMARY KEY,
                    original_path TEXT,
                    camera_id TEXT,
                    uploaded_at TEXT,
                    remote_id TEXT
                )'''
            )

    def is_uploaded(self, sha256: str) -> bool:
        with self.lock:
            cur = self.conn.execute('SELECT 1 FROM uploaded_files WHERE sha256 = ?', (sha256,))
            return cur.fetchone() is not None

    def mark_uploaded(self, sha256: str, original_path: str, camera_id: str, remote_id: str = ''):
        with self.lock:
            with self.conn:
                self.conn.execute(
                    'INSERT OR REPLACE INTO uploaded_files (sha256, original_path, camera_id, uploaded_at, remote_id) VALUES (?, ?, ?, ?, ?)',
                    (sha256, original_path, camera_id, datetime.utcnow().isoformat(), remote_id)
                )


@dataclass
class CameraFile:
    src_path: Path
    rel_path: str
    size: int
    sha256: str = ''
    staging_path: Optional[Path] = None


@dataclass
class CameraDevice:
    mountpoint: Path
    label: str
    camera_id: str
    audio_root: Path


class Uploader:
    def __init__(self, config: Dict, db: StateDB, ui_callback: Callable[[str, Dict], None]):
        self.config = config
        self.db = db
        self.ui_callback = ui_callback
        self.active = False
        self.cancel_requested = False

    def _emit(self, event: str, data: Dict):
        self.ui_callback(event, data)

    def hash_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()

    def _build_storage_name(self, camera_file: CameraFile) -> str:
        original_name = Path(camera_file.rel_path).name
        return f'{camera_file.sha256[:12]}_{original_name}'

    def _resolve_microphone_device_name(self, device: CameraDevice) -> str:
        return self.config.get('microphone_device_name') or device.label or device.camera_id

    def _resolve_report_date(self, camera_file: CameraFile) -> str:
        try:
            return datetime.fromtimestamp(camera_file.src_path.stat().st_mtime).strftime('%Y-%m-%d')
        except OSError:
            return datetime.now().strftime('%Y-%m-%d')

    def discover_camera(self) -> Optional[CameraDevice]:
        keywords = [k.lower() for k in self.config['camera_label_keywords']]
        folder_names = self.config['camera_folder_names']
        for part in psutil.disk_partitions(all=False):
            mount = Path(part.mountpoint)
            opts = (part.opts or '').lower()
            label = self._guess_volume_label(mount)
            # Accept actual removable drives, or drives whose volume label explicitly
            # matches the configured camera keywords.
            removable_hint = 'removable' in opts
            label_match = any(k in label.lower() for k in keywords)
            if not removable_hint and not label_match:
                continue
            audio_root = self._find_audio_root(mount, folder_names)
            if audio_root:
                camera_id = label if self.config.get('camera_id_from_drive_label', True) and label else mount.drive.replace(':', '')
                return CameraDevice(mount, label or mount.drive, camera_id, audio_root)
        return None

    def _find_audio_root(self, mount: Path, folder_names: List[str]) -> Optional[Path]:
        try:
            direct_audio = [p for p in mount.iterdir() if p.is_dir() and p.name in folder_names]
            if direct_audio:
                return direct_audio[0]
            # Search up to depth 3.
            for root, dirs, _files in os.walk(mount):
                rel_depth = len(Path(root).relative_to(mount).parts)
                if rel_depth > 3:
                    dirs[:] = []
                    continue
                for d in dirs:
                    if d in folder_names:
                        return Path(root) / d
        except Exception as e:
            logging.warning('Audio root search failed: %s', e)
        return None

    def _guess_volume_label(self, mount: Path) -> str:
        try:
            if os.name == 'nt':
                import ctypes
                volume_name_buffer = ctypes.create_unicode_buffer(1024)
                file_system_name_buffer = ctypes.create_unicode_buffer(1024)
                ctypes.windll.kernel32.GetVolumeInformationW(
                    ctypes.c_wchar_p(str(mount)),
                    volume_name_buffer,
                    ctypes.sizeof(volume_name_buffer),
                    None, None, None,
                    file_system_name_buffer,
                    ctypes.sizeof(file_system_name_buffer),
                )
                return volume_name_buffer.value
        except Exception:
            pass
        return mount.name or str(mount)

    def scan_files(self, device: CameraDevice) -> List[CameraFile]:
        files: List[CameraFile] = []
        audio_exts = {e.lower() for e in self.config['audio_extensions']}
        for root, _dirs, filenames in os.walk(device.audio_root):
            for name in filenames:
                ext = Path(name).suffix.lower()
                if ext not in audio_exts:
                    continue
                full = Path(root) / name
                rel = str(full.relative_to(device.mountpoint))
                try:
                    files.append(CameraFile(src_path=full, rel_path=rel, size=full.stat().st_size))
                except FileNotFoundError:
                    continue
        return sorted(files, key=lambda x: x.src_path.name)

    def upload_camera(self, device: CameraDevice):
        if self.active:
            return
        self.active = True
        self.cancel_requested = False
        try:
            files = self.scan_files(device)
            self._emit('files_found', {
                'count': len(files),
                'total_size': sum(f.size for f in files),
                'camera_id': device.camera_id,
                'label': device.label,
            })
            if not files:
                self._emit('status', {'message': 'Аудиофайлы не найдены.'})
                return

            uploaded_count = 0
            for idx, f in enumerate(files, start=1):
                if self.cancel_requested:
                    self._emit('status', {'message': 'Операция отменена.'})
                    break

                self._emit('progress', {'phase': 'hash', 'index': idx, 'total': len(files), 'file': f.rel_path, 'percent': 0})
                f.sha256 = self.hash_file(f.src_path)
                if self.db.is_uploaded(f.sha256):
                    self._emit('progress', {'phase': 'skip', 'index': idx, 'total': len(files), 'file': f.rel_path, 'percent': int((idx / len(files)) * 100)})
                    continue

                staging_dir = STAGING_DIR / device.camera_id / datetime.now().strftime('%Y-%m-%d')
                staging_dir.mkdir(parents=True, exist_ok=True)
                staging_path = staging_dir / self._build_storage_name(f)
                f.staging_path = staging_path

                self._copy_with_progress(f, staging_path, idx, len(files))
                remote_id = self._upload_with_progress(f, device, idx, len(files))
                self.db.mark_uploaded(f.sha256, str(f.src_path), device.camera_id, remote_id)
                self._archive_staged_file(f, device)
                if self.config.get('delete_from_camera_after_upload', True):
                    try:
                        f.src_path.unlink()
                    except Exception as e:
                        logging.exception('Failed deleting from camera: %s', e)
                        self._emit('warning', {'message': f'Не удалось удалить файл с камеры: {f.rel_path}'})
                uploaded_count += 1
                self._emit('progress', {'phase': 'done_file', 'index': idx, 'total': len(files), 'file': f.rel_path, 'percent': int((idx / len(files)) * 100)})

            self._cleanup_old_archive()
            self._emit('completed', {'uploaded_count': uploaded_count, 'camera_id': device.camera_id})
        except Exception as e:
            logging.exception('Upload failed: %s', e)
            self._emit('error', {'message': str(e)})
        finally:
            self.active = False

    def _copy_with_progress(self, camera_file: CameraFile, staging_path: Path, idx: int, total: int):
        chunk_size = 1024 * 1024
        copied = 0
        with open(camera_file.src_path, 'rb') as src, open(staging_path, 'wb') as dst:
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
                overall = ((idx - 1) / total) * 100
                file_pct = copied / max(camera_file.size, 1)
                percent = min(99, int(overall + (file_pct * (100 / total))))
                self._emit('progress', {
                    'phase': 'copy',
                    'index': idx,
                    'total': total,
                    'file': camera_file.rel_path,
                    'bytes_done': copied,
                    'bytes_total': camera_file.size,
                    'percent': percent,
                })

    def _upload_with_progress(self, camera_file: CameraFile, device: CameraDevice, idx: int, total: int) -> str:
        if camera_file.staging_path is None:
            raise RuntimeError('staging_path missing')
        url = self.config['server_url']
        token = self.config.get('api_token', '')
        timeout = self.config.get('request_timeout_seconds', 60)
        verify_ssl = self.config.get('verify_ssl', True)

        headers = {'Authorization': f'Bearer {token}'} if token else {}
        headers.update(self.config.get('server_metadata_headers', {}))

        self._emit('progress', {'phase': 'upload', 'index': idx, 'total': total, 'file': camera_file.rel_path, 'percent': int(((idx - 1) / total) * 100)})
        with open(camera_file.staging_path, 'rb') as f:
            files = {'audio_file': (Path(camera_file.rel_path).name, f, 'application/octet-stream')}
            data = {
                'microphone_device_name': self._resolve_microphone_device_name(device),
                'report_date': self._resolve_report_date(camera_file),
                'language_hint': self.config.get('language_hint', 'ru'),
                'output_language': self.config.get('output_language', 'ru'),
            }
            response = requests.post(url, headers=headers, files=files, data=data, timeout=timeout, verify=verify_ssl)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            return ''
        if payload.get('ok') is False or payload.get('success') is False:
            raise RuntimeError(payload.get('error') or payload.get('message') or 'Server rejected the audio file')
        return str(payload.get('remote_id') or payload.get('id') or payload.get('job_id') or '')

    def _resolve_employee_name(self) -> str:
        mode = self.config.get('employee_mode', 'computer_name')
        if mode == 'configured' and self.config.get('employee_name'):
            return self.config['employee_name']
        return os.environ.get('USERNAME') or os.environ.get('COMPUTERNAME') or 'unknown'

    def _archive_staged_file(self, camera_file: CameraFile, device: CameraDevice):
        if camera_file.staging_path is None:
            return
        archive_dir = ARCHIVE_DIR / device.camera_id / datetime.now().strftime('%Y-%m-%d')
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(camera_file.staging_path), archive_dir / camera_file.staging_path.name)

    def _cleanup_old_archive(self):
        keep_days = int(self.config.get('retain_local_archive_days', 7))
        now = time.time()
        max_age = keep_days * 86400
        for root_dir in [ARCHIVE_DIR, STAGING_DIR]:
            for path in root_dir.rglob('*'):
                if path.is_file() and now - path.stat().st_mtime > max_age:
                    try:
                        path.unlink()
                    except Exception:
                        pass


class App:
    def __init__(self):
        self.config = ensure_config()
        setup_logging()
        self.db = StateDB(DB_PATH)
        self.ui_queue: queue.Queue = queue.Queue()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry('780x520')
        self.root.minsize(700, 480)
        self.root.protocol('WM_DELETE_WINDOW', self._handle_close_attempt)
        self.current_device: Optional[CameraDevice] = None
        self.window_hidden = False
        self.uploader = Uploader(self.config, self.db, self.enqueue_ui_event)
        self._build_ui()
        self.root.after(500, self._poll_ui_events)
        self.root.after(1000, self._device_watch_tick)

    def _build_ui(self):
        pad = {'padx': 10, 'pady': 8}
        top = ttk.Frame(self.root)
        top.pack(fill='x', **pad)

        self.status_var = tk.StringVar(value='Ожидание подключения камеры...')
        self.camera_var = tk.StringVar(value='Камера: не подключена')
        self.files_var = tk.StringVar(value='Файлы: 0')

        ttk.Label(top, textvariable=self.status_var, font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(top, textvariable=self.camera_var).pack(anchor='w')
        ttk.Label(top, textvariable=self.files_var).pack(anchor='w')

        progress_frame = ttk.Frame(self.root)
        progress_frame.pack(fill='x', **pad)
        self.progress = ttk.Progressbar(progress_frame, orient='horizontal', mode='determinate', maximum=100)
        self.progress.pack(fill='x')
        self.progress_label = tk.StringVar(value='Прогресс: 0%')
        ttk.Label(progress_frame, textvariable=self.progress_label).pack(anchor='w', pady=(4, 0))

        buttons = ttk.Frame(self.root)
        buttons.pack(fill='x', **pad)
        self.start_btn = ttk.Button(buttons, text='Начать выгрузку', command=self.start_upload)
        self.start_btn.pack(side='left')
        self.rescan_btn = ttk.Button(buttons, text='Проверить камеру', command=self.manual_scan)
        self.rescan_btn.pack(side='left', padx=(8, 0))
        self.open_cfg_btn = ttk.Button(buttons, text='Открыть config.json', command=self.open_config)
        self.open_cfg_btn.pack(side='left', padx=(8, 0))

        cols = ('file', 'size', 'status')
        self.tree = ttk.Treeview(self.root, columns=cols, show='headings', height=15)
        self.tree.heading('file', text='Файл')
        self.tree.heading('size', text='Размер')
        self.tree.heading('status', text='Статус')
        self.tree.column('file', width=480)
        self.tree.column('size', width=120, anchor='e')
        self.tree.column('status', width=140, anchor='center')
        self.tree.pack(fill='both', expand=True, **pad)

        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill='both', expand=False, **pad)
        ttk.Label(log_frame, text='События').pack(anchor='w')
        self.log_text = tk.Text(log_frame, height=7, wrap='word')
        self.log_text.pack(fill='both', expand=True)
        self.log('Приложение запущено. Если config.json еще не настроен — откройте его и укажите сервер.')

    def log(self, msg: str):
        ts = datetime.now().strftime('%H:%M:%S')
        self.log_text.insert('end', f'[{ts}] {msg}\n')
        self.log_text.see('end')
        logging.info(msg)

    def enqueue_ui_event(self, event: str, data: Dict):
        self.ui_queue.put((event, data))

    def _poll_ui_events(self):
        try:
            while True:
                event, data = self.ui_queue.get_nowait()
                self.handle_ui_event(event, data)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_ui_events)

    def handle_ui_event(self, event: str, data: Dict):
        if event == 'files_found':
            count = data['count']
            self.files_var.set(f"Файлы: {count}")
            self.log(f"Найдено файлов: {count}. Камера: {data['camera_id']}")
        elif event == 'progress':
            pct = int(data.get('percent', 0))
            self.progress['value'] = pct
            phase_map = {
                'hash': 'Проверка файла',
                'copy': 'Копирование на ноутбук',
                'upload': 'Отправка на сервер',
                'done_file': 'Файл обработан',
                'skip': 'Файл уже был выгружен'
            }
            phase_text = phase_map.get(data.get('phase', ''), data.get('phase', ''))
            self.progress_label.set(f"{phase_text}: {pct}%")
            self.status_var.set(f"{phase_text}: {data.get('file', '')}")
            self._upsert_tree(data.get('file', ''), data.get('bytes_total', None), phase_text)
        elif event == 'completed':
            self.progress['value'] = 100
            self.progress_label.set('Прогресс: 100%')
            self.status_var.set('Выгрузка завершена. Камеру можно отключить.')
            self.log(f"Выгрузка завершена. Загружено файлов: {data['uploaded_count']}")
            messagebox.showinfo(APP_NAME, 'Все файлы успешно выгружены. Камеру можно отключить.')
        elif event == 'status':
            self.status_var.set(data['message'])
            self.log(data['message'])
        elif event == 'warning':
            self.log('Предупреждение: ' + data['message'])
        elif event == 'error':
            self.status_var.set('Ошибка выгрузки')
            self.log('Ошибка: ' + data['message'])
            messagebox.showerror(APP_NAME, data['message'])

    def _upsert_tree(self, file_name: str, size: Optional[int], status: str):
        if not file_name:
            return
        size_str = self._fmt_size(size) if size else ''
        for item in self.tree.get_children(''):
            vals = self.tree.item(item, 'values')
            if vals and vals[0] == file_name:
                self.tree.item(item, values=(file_name, size_str or vals[1], status))
                return
        self.tree.insert('', 'end', values=(file_name, size_str, status))

    def _fmt_size(self, size: Optional[int]) -> str:
        if not size:
            return ''
        units = ['B', 'KB', 'MB', 'GB']
        s = float(size)
        for u in units:
            if s < 1024 or u == units[-1]:
                return f'{s:.1f} {u}'
            s /= 1024
        return f'{size} B'

    def manual_scan(self):
        self.detect_camera(update_list=True)

    def detect_camera(self, update_list: bool = False):
        device = self.uploader.discover_camera()
        if device:
            changed = not self.current_device or self.current_device.mountpoint != device.mountpoint
            self.current_device = device
            if self.window_hidden:
                self._show_window_for_camera()
            self.camera_var.set(f'Камера: {device.camera_id} ({device.mountpoint})')
            self.status_var.set('Камера подключена. Готово к выгрузке.')
            if changed or update_list:
                self.tree.delete(*self.tree.get_children())
                files = self.uploader.scan_files(device)
                self.files_var.set(f'Файлы: {len(files)}')
                for f in files:
                    self.tree.insert('', 'end', values=(f.rel_path, self._fmt_size(f.size), 'Ожидает'))
                self.log(f'Камера обнаружена: {device.camera_id}, файлов: {len(files)}')
                if self.config.get('auto_start_upload', True) and changed and files and not self.uploader.active:
                    self.root.after(800, self.start_upload)
        else:
            if self.current_device is not None:
                self.log('Камера отключена.')
            self.current_device = None
            self.camera_var.set('Камера: не подключена')
            self.status_var.set('Ожидание подключения камеры...')
            self.files_var.set('Файлы: 0')

    def start_upload(self):
        if self.uploader.active:
            return
        if not self.current_device:
            messagebox.showwarning(APP_NAME, 'Сначала подключите камеру.')
            return
        self.progress['value'] = 0
        self.progress_label.set('Прогресс: 0%')
        thread = threading.Thread(target=self.uploader.upload_camera, args=(self.current_device,), daemon=True)
        thread.start()
        self.log('Запущена выгрузка файлов...')

    def open_config(self):
        import subprocess
        if not CONFIG_PATH.exists():
            ensure_config()
        try:
            if os.name == 'nt':
                os.startfile(CONFIG_PATH)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(['xdg-open', str(CONFIG_PATH)])
        except Exception:
            messagebox.showinfo(APP_NAME, str(CONFIG_PATH))

    def _device_watch_tick(self):
        if self.config.get('device_poll_enabled', True) and not self.uploader.active:
            self.detect_camera(update_list=False)
        self.root.after(max(1000, int(self.config.get('scan_interval_seconds', 5) * 1000)), self._device_watch_tick)

    def _handle_close_attempt(self):
        self.log('???? ??????. ?????????? ?????????? ???????? ? ???? ? ????? ???????? ??? ??????????? ??????.')
        self.status_var.set('?????????? ???????? ? ????.')
        self.window_hidden = True
        self.root.withdraw()

    def _show_window_for_camera(self):
        self.window_hidden = False
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.log('?????? ??????????. ???? ?????????? ???????? ?????????????.')
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    instance = SingleInstance(LOCK_PATH)
    if not instance.acquire():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, 'Приложение уже запущено.')
        root.destroy()
    else:
        App().run()
