import hashlib
import json
import logging
import os
import queue
import atexit
import shutil
import sqlite3
import struct
import threading
import time
import zipfile
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
        self.session_uploaded_shas: set[str] = set()
        self.session_device_key = ''

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

    def _build_archive_name(self, device: CameraDevice) -> str:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return f'{device.camera_id}_{timestamp}.zip'

    def _format_bytes(self, size: int) -> str:
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f'{value:.1f} {unit}'
            value /= 1024
        return f'{size} B'

    def _wav_format_name(self, format_code: int) -> str:
        known = {
            0x0001: 'PCM',
            0x0002: 'ADPCM',
            0x0003: 'IEEE_FLOAT',
            0x0006: 'ALAW',
            0x0007: 'MULAW',
            0x0011: 'IMA_ADPCM',
            0x0031: 'GSM610',
            0x0050: 'MPEG',
            0x0055: 'MP3',
            0x00FF: 'AAC',
        }
        return known.get(format_code, f'format_0x{format_code:04X}')

    def _probe_wav_format(self, path: Path) -> Optional[Dict[str, int]]:
        try:
            with open(path, 'rb') as f:
                header = f.read(65536)
        except OSError:
            return None

        if len(header) < 12 or header[:4] != b'RIFF' or header[8:12] != b'WAVE':
            return None

        offset = 12
        while offset + 8 <= len(header):
            chunk_id = header[offset:offset + 4]
            chunk_size = struct.unpack_from('<I', header, offset + 4)[0]
            chunk_data_offset = offset + 8
            if chunk_id == b'fmt ' and chunk_size >= 16 and chunk_data_offset + chunk_size <= len(header):
                format_code, channels, sample_rate = struct.unpack_from('<HHI', header, chunk_data_offset)
                bits_per_sample = struct.unpack_from('<H', header, chunk_data_offset + 14)[0]
                return {
                    'format_code': format_code,
                    'channels': channels,
                    'sample_rate': sample_rate,
                    'bits_per_sample': bits_per_sample,
                }
            offset = chunk_data_offset + chunk_size + (chunk_size % 2)
        return None

    def _build_wav_codec_summary(self, files: List[CameraFile]) -> Optional[str]:
        wav_files = [camera_file for camera_file in files if camera_file.src_path.suffix.lower() == '.wav']
        if not wav_files:
            return None

        codec_counts: Dict[str, int] = {}
        sample_details: List[str] = []
        unknown_count = 0
        for camera_file in wav_files[:10]:
            info = self._probe_wav_format(camera_file.src_path)
            if not info:
                unknown_count += 1
                continue
            codec_name = self._wav_format_name(info['format_code'])
            codec_counts[codec_name] = codec_counts.get(codec_name, 0) + 1
            if len(sample_details) < 3:
                sample_details.append(
                    f"{Path(camera_file.rel_path).name}: {codec_name}, "
                    f"{info['channels']}ch, {info['sample_rate']}Hz, {info['bits_per_sample']}bit"
                )

        parts = [f'WAV files: {len(wav_files)}']
        if codec_counts:
            codecs_text = ', '.join(f'{name}={count}' for name, count in sorted(codec_counts.items()))
            parts.append(f'codecs: {codecs_text}')
        if unknown_count:
            parts.append(f'unparsed={unknown_count}')
        if sample_details:
            parts.append('samples: ' + '; '.join(sample_details))
        return ' | '.join(parts)

    def _log_zip_diagnostics(self, files: List[CameraFile], zip_path: Path):
        total_size = sum(f.size for f in files)
        try:
            zip_size = zip_path.stat().st_size
        except OSError:
            logging.warning('ZIP created but size could not be read: %s', zip_path)
            return

        saved_bytes = total_size - zip_size
        ratio = (zip_size / total_size * 100) if total_size else 0.0
        saved_ratio = (saved_bytes / total_size * 100) if total_size else 0.0
        summary = (
            f"ZIP diagnostics: source={self._format_bytes(total_size)} ({total_size} bytes), "
            f"zip={self._format_bytes(zip_size)} ({zip_size} bytes), "
            f"ratio={ratio:.1f}%, saved={self._format_bytes(saved_bytes)} ({saved_ratio:.1f}%)"
        )
        logging.info(summary)
        self._emit('log', {'message': summary})

        wav_summary = self._build_wav_codec_summary(files)
        if wav_summary:
            logging.info(wav_summary)
            self._emit('log', {'message': wav_summary})

    def start_device_session(self, device: CameraDevice):
        device_key = f'{device.mountpoint}|{device.camera_id}'
        if self.session_device_key != device_key:
            self.session_device_key = device_key
            self.session_uploaded_shas.clear()

    def end_device_session(self):
        self.session_device_key = ''
        self.session_uploaded_shas.clear()

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
        self.start_device_session(device)
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

            pending_files: List[CameraFile] = []
            for idx, f in enumerate(files, start=1):
                if self.cancel_requested:
                    self._emit('status', {'message': 'Операция отменена.'})
                    break

                self._emit('progress', {'phase': 'hash', 'index': idx, 'total': len(files), 'file': f.rel_path, 'size': f.size, 'percent': 0})
                f.sha256 = self.hash_file(f.src_path)
                if f.sha256 in self.session_uploaded_shas:
                    self._emit('progress', {'phase': 'skip', 'index': idx, 'total': len(files), 'file': f.rel_path, 'size': f.size, 'percent': int((idx / len(files)) * 100)})
                    continue

                pending_files.append(f)
                continue
                if self.config.get('delete_from_camera_after_upload', True):
                    try:
                        f.src_path.unlink()
                    except Exception as e:
                        logging.exception('Failed deleting from camera: %s', e)
                        self._emit('warning', {'message': f'Не удалось удалить файл с камеры: {f.rel_path}'})
                uploaded_count += 1
                self._emit('progress', {'phase': 'done_file', 'index': idx, 'total': len(files), 'file': f.rel_path, 'size': f.size, 'percent': int((idx / len(files)) * 100)})

            if self.cancel_requested:
                return

            if not pending_files:
                self._emit('status', {'message': 'РќРѕРІС‹С… С„Р°Р№Р»РѕРІ РґР»СЏ РІС‹РіСЂСѓР·РєРё РЅРµС‚.'})
                return

            staging_dir = STAGING_DIR / device.camera_id / datetime.now().strftime('%Y-%m-%d')
            staging_dir.mkdir(parents=True, exist_ok=True)
            zip_path = staging_dir / self._build_archive_name(device)

            self._create_zip_with_progress(pending_files, zip_path)
            remote_id = self._upload_archive_with_progress(zip_path, device, pending_files)
            self._archive_zip_file(zip_path, device)

            for idx, f in enumerate(pending_files, start=1):
                self.db.mark_uploaded(f.sha256, str(f.src_path), device.camera_id, remote_id)
                self.session_uploaded_shas.add(f.sha256)
                if self.config.get('delete_from_camera_after_upload', True):
                    try:
                        f.src_path.unlink()
                    except Exception as e:
                        logging.exception('Failed deleting from camera: %s', e)
                        self._emit('warning', {'message': f'РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ С„Р°Р№Р» СЃ РєР°РјРµСЂС‹: {f.rel_path}'})
                self._emit('progress', {'phase': 'done_file', 'index': idx, 'total': len(pending_files), 'file': f.rel_path, 'size': f.size, 'percent': int((idx / len(pending_files)) * 100)})

            self._cleanup_old_archive()
            self._emit('completed', {'uploaded_count': len(pending_files), 'camera_id': device.camera_id})
        except Exception as e:
            logging.exception('Upload failed: %s', e)
            self._emit('error', {'message': str(e)})
        finally:
            self.active = False

    def _create_zip_with_progress(self, files: List[CameraFile], zip_path: Path):
        total_size = sum(f.size for f in files)
        processed_size = 0
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for idx, camera_file in enumerate(files, start=1):
                archive.write(camera_file.src_path, arcname=camera_file.rel_path)
                processed_size += camera_file.size
                percent = min(99, int((processed_size / max(total_size, 1)) * 100))
                self._emit('progress', {
                    'phase': 'zip',
                    'index': idx,
                    'total': len(files),
                    'file': camera_file.rel_path,
                    'size': camera_file.size,
                    'bytes_done': processed_size,
                    'bytes_total': total_size,
                    'percent': percent,
                })
        self._log_zip_diagnostics(files, zip_path)

    def _upload_archive_with_progress(self, zip_path: Path, device: CameraDevice, files: List[CameraFile]) -> str:
        url = self.config['server_url']
        token = self.config.get('api_token', '')
        verify_ssl = self.config.get('verify_ssl', True)

        headers = {'Authorization': f'Bearer {token}'} if token else {}
        headers.update(self.config.get('server_metadata_headers', {}))

        self._emit('progress', {'phase': 'upload', 'index': 1, 'total': 1, 'file': zip_path.name, 'size': zip_path.stat().st_size, 'percent': 99})
        with open(zip_path, 'rb') as f:
            multipart_files = {'audio_file': (zip_path.name, f, 'application/zip')}
            data = {
                'microphone_device_name': self._resolve_microphone_device_name(device),
                'report_date': self._resolve_report_date(files[0]),
                'language_hint': self.config.get('language_hint', 'ru'),
                'output_language': self.config.get('output_language', 'ru'),
                'camera_id': device.camera_id,
                'file_count': str(len(files)),
                'file_names': json.dumps([camera_file.rel_path for camera_file in files], ensure_ascii=False),
            }
            response = requests.post(url, headers=headers, files=multipart_files, data=data, verify=verify_ssl)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            return ''
        if payload.get('ok') is False or payload.get('success') is False:
            raise RuntimeError(payload.get('error') or payload.get('message') or 'Server rejected the archive')
        return str(payload.get('remote_id') or payload.get('id') or payload.get('job_id') or '')

    def _resolve_employee_name(self) -> str:
        mode = self.config.get('employee_mode', 'computer_name')
        if mode == 'configured' and self.config.get('employee_name'):
            return self.config['employee_name']
        return os.environ.get('USERNAME') or os.environ.get('COMPUTERNAME') or 'unknown'

    def _archive_zip_file(self, zip_path: Path, device: CameraDevice):
        archive_dir = ARCHIVE_DIR / device.camera_id / datetime.now().strftime('%Y-%m-%d')
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(zip_path), archive_dir / zip_path.name)

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
                'zip': 'Упаковка ZIP',
                'upload': 'Отправка на сервер',
                'done_file': 'Файл обработан',
                'skip': 'Файл уже был выгружен'
            }
            phase_text = phase_map.get(data.get('phase', ''), data.get('phase', ''))
            self.progress_label.set(f"{phase_text}: {pct}%")
            self.status_var.set(f"{phase_text}: {data.get('file', '')}")
            self._upsert_tree(data.get('file', ''), data.get('size', data.get('bytes_total', None)), phase_text)
        elif event == 'completed':
            self.progress['value'] = 100
            self.progress_label.set('Прогресс: 100%')
            self.status_var.set('Выгрузка завершена. Камеру можно отключить.')
            self.log(f"Выгрузка завершена. Загружено файлов: {data['uploaded_count']}")
            messagebox.showinfo(APP_NAME, 'Все файлы успешно выгружены. Камеру можно отключить.')
        elif event == 'status':
            self.status_var.set(data['message'])
            self.log(data['message'])
        elif event == 'log':
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
            if changed:
                self.uploader.start_device_session(device)
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
                self.uploader.end_device_session()
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
