import os
import sys
from ctypes import CDLL

# --- MPV preload ---
script_dir = os.path.dirname(os.path.abspath(__file__))

mpv_candidates = [
    os.path.join(script_dir, "MPV"),
    os.path.join(script_dir, "mpv"),
    os.path.join(script_dir, "MPV-2"),
]
mpv_path = next((p for p in mpv_candidates if os.path.isdir(p)), mpv_candidates[0])

os.environ["PATH"] = mpv_path + os.pathsep + os.environ.get("PATH", "")

if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    try:
        os.add_dll_directory(mpv_path)
    except Exception:
        pass

mpv_dll_loaded = False
for dll_name in ["libmpv-2.dll", "libmpv-1.dll", "mpv-2.dll", "mpv-1.dll"]:
    dll_path = os.path.join(mpv_path, dll_name)
    if os.path.isfile(dll_path):
        try:
            CDLL(dll_path)
            print(f"✅ {dll_name} loaded from: {mpv_path}")
            mpv_dll_loaded = True
            break
        except Exception as e:
            print(f"⚠️ Не удалось загрузить {dll_path}: {e}")
            continue

if not mpv_dll_loaded:
    print(f"❌ MPV DLL не найдена в {mpv_path}, пробуем системный PATH")

import re
import json
import gzip
import sqlite3
import socket
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime
from xml.etree import ElementTree
import time
import traceback

from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtCore import (
    QObject, Signal, Slot, Property, QThread,
    QAbstractListModel, qInstallMessageHandler, QTimer,
)


def qt_message_handler(msg_type, context, message):
    if "QQuickImage" in message:
        return
    sys.stderr.write(f"{message}\n")


qInstallMessageHandler(qt_message_handler)

try:
    import requests
except ImportError:
    raise SystemExit("❌ Модуль requests не установлен: pip install requests")

try:
    import mpv
    HAS_MPV = True
    print("✅ MPV imported")
except ImportError as e:
    HAS_MPV = False
    print(f"❌ MPV import: {e}")

try:
    import locale
    locale.setlocale(locale.LC_NUMERIC, 'C')
except Exception:
    pass

# Глобальная HTTP-сессия (keep-alive + переиспользование соединений)
http_session = requests.Session()


# ============================================================
#  КОНФИГУРАЦИЯ — ЕДИНЫЙ ИСТОЧНИК ПРАВДЫ
# ============================================================

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-site',
}


class CacheConfig:
    """
    Строгие лимиты кэша — единый источник правды (ТЗ: 200 МБ / 60 секунд).
    ВАЖНО: это ПОТОЛОК буфера, он НЕ урезает качество. Пользователь сам
    выбирает разрешение через setQuality; эти значения лишь не дают плееру
    бесконечно разрастаться в оперативной памяти.
    """
    MAX_BYTES = "200MiB"          # максимум 200 МБ вперёд (ТЗ)
    MAX_BACK_BYTES = "10MiB"      # назад только 10 МБ (перемотка)
    CACHE_SECS = "60"             # максимум 60 секунд видео вперёд
    READAHEAD_SECS = "60"         # readahead 60 с
    HYSTERESIS_SECS = "10"        # стоп докачки, пока в кэше есть >=10 с
    NETWORK_TIMEOUT = "20"        # таймаут сети


# Профили качества: потолок буфера ПОД ПРОФИЛЬ.
# demuxer-max-bytes НЕ может превышать CacheConfig.MAX_BYTES ("200МБ").
QUALITY_PROFILES = {
    "ultra":   {"readahead": "10", "max_bytes": "200MiB", "hls_bitrate": "max"},
    "high":    {"readahead": "15", "max_bytes": "200MiB", "hls_bitrate": "8000000"},
    "medium":  {"readahead": "30", "max_bytes": "150MiB", "hls_bitrate": "3000000"},
    "low":     {"readahead": "60", "max_bytes": "100MiB", "hls_bitrate": "1200000"},
    "minimal": {"readahead": "60", "max_bytes": "50MiB",  "hls_bitrate": "min"},
    "auto":    {"readahead": CacheConfig.READAHEAD_SECS,
                "max_bytes": "150MiB", "hls_bitrate": "no"},
}

# Карта целевых высот для программного масштабирования
QUALITY_HEIGHTS = {"ultra": 2160, "high": 1080, "medium": 720, "low": 480, "minimal": 360}

# Домены, требующие проксирования вложенных ресурсов (длинные токены → 414)
PROXY_DOMAINS = ['televizor-24', 'streaming.', 'online-television']


# ============================================================
#  ПОТОКОБЕЗОПАСНАЯ БАЗА ДАННЫХ
# ============================================================

class Database:
    """
    Потокобезопасная обёртка над ОДНИМ соединением sqlite3.
    Паттерн «serialized access to a single connection»: один reentrant-блокировщик
    на все операции. Это даёт два гарантированных свойства, которых раньше не было:
      1. Невозможен «database is locked» — соединение в любой момент трогает
         максимум один поток (RLock сериализует доступ).
      2. Данные не теряются и не разрушаются при параллельной записи из фоновых
         потоков (recordChannelClick, predictNextChannel, prefetch).
    RLock (а не Lock) — ради безопасных вложенных вызовов внутри одного потока.
    """

    def __init__(self, path="premium.db"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA synchronous=NORMAL")

    def execute(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql, params_seq):
        with self._lock:
            return self._conn.executemany(sql, params_seq)

    def fetchall(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def fetchone(self, sql, params=()):
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def commit(self):
        with self._lock:
            self._conn.commit()

    def init_schema(self):
        with self._lock:
            c = self._conn
            c.execute("""CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY, name TEXT, proto TEXT, host TEXT, epg TEXT,
                user TEXT, pwd TEXT, mac TEXT, channels TEXT, epg_db TEXT,
                movies TEXT, series TEXT, xtream_host TEXT, xtream_user TEXT, xtream_pwd TEXT)""")
            # Миграция: добавляем колонки к старым БД
            cur = c.execute("PRAGMA table_info(playlists)")
            existing = {row[1] for row in cur.fetchall()}
            for col, typ in [("movies", "TEXT"), ("series", "TEXT"),
                             ("xtream_host", "TEXT"), ("xtream_user", "TEXT"), ("xtream_pwd", "TEXT")]:
                if col not in existing:
                    c.execute(f"ALTER TABLE playlists ADD COLUMN {col} {typ}")
            c.execute("""CREATE TABLE IF NOT EXISTS favorites (
                playlist_id INTEGER, channel_id TEXT,
                PRIMARY KEY (playlist_id, channel_id))""")
            c.execute("""CREATE TABLE IF NOT EXISTS click_history (
                id INTEGER PRIMARY KEY, ts INTEGER, channel_id TEXT,
                channel_name TEXT, category TEXT, playlist_id INTEGER,
                hour INTEGER, weekday INTEGER)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_history_cid ON click_history(channel_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_history_hour ON click_history(hour)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_history_wday ON click_history(weekday)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_history_ts ON click_history(ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_history_cat ON click_history(category)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_fav_pl ON favorites(playlist_id)")
            c.commit()


# ============================================================
#  ОПТИМИЗАТОР ПОТОКА
# ============================================================

class StreamOptimizer:
    """
    По умолчанию НЕ урезает качество — уважает выбор пользователя
    (4K остаётся 4K, 1080p остаётся 1080p).
    Жёсткую экономию трафика можно включить вручную из меню 💰 на OSD.
    """

    def __init__(self):
        self.quality_level = "auto"
        self.bandwidth = 0.0
        self.reconnect_count = 0
        self.max_reconnects = 5
        # === РЕЖИМ ЭКОНОМИИ ТРАФИКА (по умолчанию ВЫКЛЮЧЕН) ===
        self.force_lowest_variant = False
        # Тест пропускной способности сам по себе жрёт ~512 KiB впустую → по умолчанию выкл.
        self.bandwidth_probe_kib = 0

    def detect_bandwidth(self, url, proxy_url=None):
        if self.bandwidth_probe_kib <= 0:
            return 0.0
        try:
            proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
            start = time.time()
            r = http_session.get(url, headers=BROWSER_HEADERS, proxies=proxies, timeout=10, stream=True)
            data = b''
            limit_bytes = self.bandwidth_probe_kib * 1024
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    data += chunk
                    if len(data) > limit_bytes:
                        break
            duration = time.time() - start
            if duration > 0:
                self.bandwidth = (len(data) / 1024) / duration
                print(f"[Optimizer] Bandwidth: {self.bandwidth:.1f} KB/s ({self.bandwidth * 8:.0f} Kbps)")
                return self.bandwidth
        except Exception as e:
            print(f"[Optimizer] Bandwidth test failed: {e}")
        return 0.0

    def get_quality_for_bandwidth(self, bandwidth):
        kbps = bandwidth * 8
        if kbps > 20000:
            return "ultra"
        elif kbps > 8000:
            return "high"
        elif kbps > 3000:
            return "medium"
        elif kbps > 1000:
            return "low"
        else:
            return "minimal"

    def should_reconnect(self):
        return self.reconnect_count < self.max_reconnects

    def increase_reconnect(self):
        self.reconnect_count += 1

    def reset_reconnect(self):
        self.reconnect_count = 0


stream_optimizer = StreamOptimizer()


# ============================================================
#  HLS КЭШ + ПРОКСИ
# ============================================================

class HLSCache:
    """Умный кэш m3u8: мастер-плейлисты 60 с, медиа-плейлисты 3.5 с."""

    _global_cache = {}
    _MAX_ENTRIES = 256

    def __init__(self, proxy_url=None, quality="auto"):
        self.proxy_url = proxy_url
        self.quality = quality

    @classmethod
    def _evict_if_needed(cls):
        # Лёгкая защита от бесконечного роста: при превышении лимита удаляем
        # самые старые записи. TTL всё равно добьёт оставшиеся при чтении.
        if len(cls._global_cache) > cls._MAX_ENTRIES:
            sorted_items = sorted(cls._global_cache.items(), key=lambda kv: kv[1][1])
            for k, _ in sorted_items[: cls._MAX_ENTRIES // 4]:
                cls._global_cache.pop(k, None)

    @classmethod
    def clear(cls):
        cls._global_cache.clear()

    def _get_quality_variant(self, content, bandwidth):
        """Выбирает вариант потока под выбранное качество / пропускную способность."""
        lines = content.split('\n')
        variants = []

        for i, line in enumerate(lines):
            if '#EXT-X-STREAM-INF' in line:
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                if bw_match:
                    bw = int(bw_match.group(1))
                    res = res_match.group(1) if res_match else "unknown"
                    uri = ""
                    for j in range(i + 1, len(lines)):
                        cand = lines[j].strip()
                        if cand and not cand.startswith('#'):
                            uri = cand
                            break
                    if uri:
                        variants.append({'bw': bw, 'bw_kbps': bw / 1000,
                                         'resolution': res, 'line_index': i, 'uri': uri})

        if not variants:
            return content

        variants.sort(key=lambda x: x['bw_kbps'])

        # === ЖЁСТКАЯ ЭКОНОМИЯ: всегда самый дешёвый вариант ===
        if stream_optimizer.force_lowest_variant:
            chosen = variants[0]
            print(f"[Optimizer] 💰 TRAFFIC SAVER: forced LOWEST variant "
                  f"{chosen['resolution']} ({chosen['bw_kbps']:.0f} Kbps)")
        else:
            quality_level = stream_optimizer.quality_level
            if quality_level == "auto":
                target_bw_kbps = bandwidth * 8 * 0.85 if bandwidth > 0 else float('inf')
            else:
                target_bw_kbps = QUALITY_PROFILES.get(quality_level, {}).get('hls_bitrate', 'inf')
                try:
                    target_bw_kbps = float(target_bw_kbps)
                except (TypeError, ValueError):
                    target_bw_kbps = float('inf')  # 'max' → берём максимальный

            chosen = None
            for v in variants:
                if v['bw_kbps'] <= target_bw_kbps:
                    chosen = v
                else:
                    break
            if not chosen:
                chosen = variants[0]

            print(f"[Optimizer] Selected variant: {chosen['resolution']} "
                  f"({chosen['bw_kbps']:.0f} Kbps) for target {target_bw_kbps:.0f} Kbps (level: {quality_level})")

        # Перезаписываем m3u8 только выбранным вариантом
        result_lines = []
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            if '#EXT-X-STREAM-INF' in line:
                is_chosen = any(v['line_index'] == i and v is chosen for v in variants)
                if is_chosen:
                    result_lines.append(line)
                else:
                    # Пропускаем stream-inf + его URI
                    i += 1
                    while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith('#')):
                        i += 1
                    i += 1
                    continue
            else:
                result_lines.append(line)
            i += 1

        return '\n'.join(result_lines)

    def fetch_with_headers(self, url, referer=None):
        if url in HLSCache._global_cache:
            cached_content, cached_time, cached_is_master = HLSCache._global_cache[url]
            ttl = 60 if cached_is_master else 3.5
            if time.time() - cached_time < ttl:
                return cached_content, None

        headers = BROWSER_HEADERS.copy()
        if referer:
            headers['Referer'] = referer
            parsed = urllib.parse.urlparse(referer)
            headers['Origin'] = f"{parsed.scheme}://{parsed.netloc}"
        else:
            parsed = urllib.parse.urlparse(url)
            headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
            headers['Origin'] = f"{parsed.scheme}://{parsed.netloc}"

        proxies = {'http': self.proxy_url, 'https': self.proxy_url} if self.proxy_url else None

        try:
            response = http_session.get(url, headers=headers, timeout=20, proxies=proxies)
            if response.status_code == 200:
                content = response.content
                try:
                    if response.headers.get('Content-Encoding') == 'gzip':
                        content = gzip.decompress(content)
                except Exception:
                    pass
                content = content.decode('utf-8', errors='ignore')
                is_master = '#EXT-X-STREAM-INF' in content

                if stream_optimizer.bandwidth > 0 or stream_optimizer.quality_level != "auto":
                    content = self._get_quality_variant(content, stream_optimizer.bandwidth)

                HLSCache._evict_if_needed()
                HLSCache._global_cache[url] = (content, time.time(), is_master)
                return content, headers
        except Exception as e:
            print(f"❌ Cache fetch error: {e}")

        return None, headers


class HLSProxyHandler(BaseHTTPRequestHandler):
    """Оптимизированный HLS Proxy с буферизацией и переподключением."""

    proxy_url = None
    port = 8899
    core_ref = None
    last_base_url = None

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        try:
            if self.path.startswith('/hls/'):
                original_url = urllib.parse.unquote(self.path[5:])
            elif self.path.startswith('/stream'):
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                original_url = params.get('url', [''])[0]
            else:
                self.send_error(400)
                return

            if not original_url:
                self.send_error(400)
                return

            # Глубокая очистка double-encoded &amp;
            while '&amp;' in original_url:
                original_url = original_url.replace('&amp;', '&')

            if not original_url.startswith('http'):
                if HLSProxyHandler.last_base_url:
                    if original_url.startswith('/'):
                        p = urllib.parse.urlparse(HLSProxyHandler.last_base_url)
                        original_url = f"{p.scheme}://{p.netloc}{original_url}"
                    else:
                        original_url = HLSProxyHandler.last_base_url + original_url
                    print(f"⚠️ [HLS Proxy] Restored relative URL to absolute: {original_url[:60]}...")
                else:
                    self.send_error(400)
                    return

            lower = original_url.lower()
            if '.m3u8' in lower:
                self._handle_m3u8(original_url)
            elif '.ts' in lower or '.mp4' in lower:
                self._handle_segment(original_url)
            else:
                self._handle_generic(original_url)

        except Exception as e:
            print(f"❌ HLS Proxy error: {e}")
            try:
                self.send_error(500)
            except Exception:
                pass

    def _handle_m3u8(self, url):
        while '&amp;' in url:
            url = url.replace('&amp;', '&')

        print(f"📡 [HLS] Fetching: {url[:60]}...")

        if stream_optimizer.bandwidth == 0:
            stream_optimizer.detect_bandwidth(url, self.proxy_url)

        cache = HLSCache(self.proxy_url, stream_optimizer.quality_level)
        content, _ = cache.fetch_with_headers(url)

        if not content:
            print("❌ [HLS] Failed to fetch manifest")
            self.send_error(502)
            return

        print("✅ [HLS] Manifest loaded, quality optimized")

        base_url = url.rsplit('/', 1)[0] + '/'
        HLSProxyHandler.last_base_url = base_url

        proxy_sub = any(d in url.lower() for d in PROXY_DOMAINS)

        lines = []
        for line in content.split('\n'):
            line = line.rstrip()
            while '&amp;' in line:
                line = line.replace('&amp;', '&')

            if not line:
                lines.append(line)
                continue

            if line.startswith('#'):
                if 'URI=' in line:
                    match = re.search(r'URI="([^"]*)"', line)
                    if match:
                        uri_val = match.group(1)
                        while '&amp;' in uri_val:
                            uri_val = uri_val.replace('&amp;', '&')
                        uri_val = make_absolute(uri_val, base_url, url)
                        proxied = (f"http://127.0.0.1:{self.port}/hls/"
                                   f"{urllib.parse.quote(uri_val, safe='')}") if proxy_sub else uri_val
                        line = line.replace(f'URI="{match.group(1)}"', f'URI="{proxied}"')
                lines.append(line)
            else:
                absolute = make_absolute(line, base_url, url)
                if proxy_sub:
                    lines.append(f"http://127.0.0.1:{self.port}/hls/{urllib.parse.quote(absolute, safe='')}")
                else:
                    lines.append(absolute)

        result = '\n'.join(lines)

        self.send_response(200)
        self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
        self.send_header('Content-Length', len(result))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()
        self.wfile.write(result.encode('utf-8'))
        print(f"✅ [HLS] Sent {len(lines)} lines")

    def _handle_segment(self, url):
        max_retries, retry_delay = 3, 1
        for attempt in range(max_retries):
            try:
                seg_headers = BROWSER_HEADERS.copy()
                parsed = urllib.parse.urlparse(url)
                seg_headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
                seg_headers['Origin'] = f"{parsed.scheme}://{parsed.netloc}"
                seg_headers['Accept-Encoding'] = 'identity'

                proxies = {'http': self.proxy_url, 'https': self.proxy_url} if self.proxy_url else None
                response = http_session.get(url, headers=seg_headers, timeout=30, stream=True, proxies=proxies)

                if response.status_code == 200:
                    self.send_response(200)
                    for key, value in response.headers.items():
                        if key.lower() not in ['transfer-encoding', 'content-encoding', 'connection']:
                            self.send_header(key, value)
                    self.send_header('Connection', 'keep-alive')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()

                    start_time = time.time()
                    bytes_downloaded = 0
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            self.wfile.write(chunk)
                            bytes_downloaded += len(chunk)

                    duration = time.time() - start_time
                    if duration > 0.05 and bytes_downloaded > 1024:
                        speed_kb = (bytes_downloaded / 1024) / duration
                        if stream_optimizer.bandwidth == 0:
                            stream_optimizer.bandwidth = speed_kb
                        else:
                            stream_optimizer.bandwidth = stream_optimizer.bandwidth * 0.7 + speed_kb * 0.3
                        if HLSProxyHandler.core_ref:
                            try:
                                HLSProxyHandler.core_ref.segmentDownloaded.emit(speed_kb)
                            except Exception:
                                pass
                    return
                elif response.status_code in [403, 404]:
                    print(f"⚠️ [HLS] Segment {response.status_code}, retry {attempt + 1}/{max_retries}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
            except Exception as e:
                print(f"⚠️ [HLS] Segment error: {e}, retry {attempt + 1}/{max_retries}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue

        try:
            self.send_error(500)
        except Exception:
            pass

    def _handle_generic(self, url):
        try:
            proxies = {'http': self.proxy_url, 'https': self.proxy_url} if self.proxy_url else None
            response = http_session.get(url, headers=BROWSER_HEADERS, timeout=30, proxies=proxies)
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in ['transfer-encoding', 'content-encoding']:
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.content)
        except Exception:
            try:
                self.send_error(500)
            except Exception:
                pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()


def make_absolute(candidate, base_url, full_url):
    """Превращает относительный путь манифеста в абсолютный URL (один источник правды)."""
    if candidate.startswith('http://') or candidate.startswith('https://'):
        return candidate
    while '&amp;' in candidate:
        candidate = candidate.replace('&amp;', '&')
    if candidate.startswith('/'):
        p = urllib.parse.urlparse(full_url)
        return f"{p.scheme}://{p.netloc}{candidate}"
    return base_url + candidate


class HLSProxyServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


_hls_server = None


def start_hls_proxy(proxy_url=None, port=8899, core=None):
    global _hls_server
    if _hls_server:
        if core:
            HLSProxyHandler.core_ref = core
        return f"http://127.0.0.1:{port}"
    try:
        HLSProxyHandler.proxy_url = proxy_url
        HLSProxyHandler.port = port
        HLSProxyHandler.core_ref = core
        _hls_server = HLSProxyServer(('127.0.0.1', port), HLSProxyHandler)
        threading.Thread(target=_hls_server.serve_forever, daemon=True).start()
        print(f"✅ [HLS Proxy] Started on port {port}")
        return f"http://127.0.0.1:{port}"
    except Exception as e:
        print(f"❌ [HLS Proxy] Start error: {e}")
        return None


def stop_hls_proxy():
    global _hls_server
    if _hls_server:
        _hls_server.shutdown()
        _hls_server = None


def get_proxied_url(url, port=8899):
    if not url:
        return url
    return f"http://127.0.0.1:{port}/hls/{urllib.parse.quote(url, safe='')}"


# Проверенные рабочие резервные потоки
VERIFIED_WORKING_STREAMS = {
    "360": "https://cdn-evacoder-tv.facecast.io/evacoder_hls_hi/CkxfR1xNUAJwTgtXTBZTAJli/index.m3u8",
    "mma": "https://streams2.sofast.tv/vglive-sk-462904/playlist.m3u8",
    "pro100": "https://sirius.greenhosting.ru/Pro100tvRu/video.m3u8",
    "drive": "https://stream8.cinerama.uz/1421/tracks-v1a1/mono.m3u8",
}


def get_fallback_url(channel_name):
    for url in VERIFIED_WORKING_STREAMS.values():
        if url:
            return url
    return None


# ============================================================
#  ГЕО / СТРАНЫ
# ============================================================

COUNTRY_BY_KEYWORD = {
    "france": ("FR", "Франция"), "french": ("FR", "Франция"),
    "usa": ("US", "США"), "america": ("US", "США"),
    "uk": ("GB", "Великобритания"), "bbc": ("GB", "Великобритания"),
    "germany": ("DE", "Германия"), "deutsch": ("DE", "Германия"),
    "italy": ("IT", "Италия"), "italian": ("IT", "Италия"),
    "spain": ("ES", "Испания"), "spanish": ("ES", "Испания"),
    "brazil": ("BR", "Бразилия"), "globo": ("BR", "Бразилия"),
    "japan": ("JP", "Япония"), "japanese": ("JP", "Япония"),
    "russia": ("RU", "Россия"), "россия": ("RU", "Россия"),
    "матч": ("RU", "Россия"), "российский": ("RU", "Россия"),
    "turkey": ("TR", "Турция"), "турция": ("TR", "Турция"),
}

RU_NAMES = {
    "RU": "Россия", "US": "США", "GB": "Великобритания", "DE": "Германия",
    "FR": "Франция", "IT": "Италия", "ES": "Испания", "TR": "Турция",
    "UA": "Украина", "BY": "Беларусь", "KZ": "Казахстан", "NL": "Нидерланды",
}


def detect_country(cat, name):
    text = (str(cat) + " " + str(name)).lower()
    for kw, (code, cn) in COUNTRY_BY_KEYWORD.items():
        if kw in text:
            return code, cn
    return "ALL", "Глобальный"


def get_ip_country(ip):
    try:
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=5).json()
        code = r.get("country_code", "ALL")
        name = r.get("country_name", "Глобальный")
        return code, RU_NAMES.get(code, name)
    except Exception:
        return "ALL", "Глобальный"


# ============================================================
#  IPTV WORKER (ПАРСЕРЫ)
# ============================================================

class IPTVWorker(QThread):
    finished = Signal(list, dict, str)
    error = Signal(str)

    def __init__(self, proto, host, epg, user, pwd, mac):
        super().__init__()
        self.proto = proto
        self.host = host
        self.epg = epg
        self.user = user
        self.pwd = pwd
        self.mac = mac

    def run(self):
        try:
            ch, epg_db = [], {}
            headers = BROWSER_HEADERS.copy()

            proto_l = (self.proto or "").lower().strip()
            host_l = (self.host or "").lower().strip()

            if proto_l.startswith("m3u"):
                if host_l.startswith("http://") or host_l.startswith("https://"):
                    ch = self._parse_m3u(self.host, headers)
                else:
                    ch = self._parse_m3u_file(self.host)
            elif proto_l.startswith("xtream"):
                ch = self._parse_xtream(headers)
            elif proto_l.startswith("stalker"):
                ch = self._parse_stalker(headers)
            else:
                raise ValueError(f"Неизвестный протокол: '{self.proto}'")

            if self.epg and self.epg.strip():
                epg_db = self._load_epg(self.epg, headers)

            self.finished.emit(ch, epg_db, "Успешно загружено")
        except Exception as e:
            traceback.print_exc()
            err_msg = f"{type(e).__name__}: {e}"
            print(f"❌ [Worker] ОШИБКА ЗАГРУЗКИ: {err_msg}")
            self.error.emit(err_msg)

    # --- M3U ---
    def _parse_m3u(self, url, h):
        r = requests.get(url, headers=h, timeout=20)
        return self._parse_m3u_text(r.text)

    def _parse_m3u_file(self, path):
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return self._parse_m3u_text(f.read())

    def _parse_m3u_text(self, text):
        ch = []
        current = {}
        for line in text.split('\n'):
            line = line.strip()
            if line.startswith('#EXTINF:'):
                name_match = re.search(r',([^,]*)$', line)
                name = name_match.group(1).strip() if name_match else "Канал без названия"
                attrs = {}
                for m in re.finditer(r'([a-zA-Z0-9_-]+)="([^"]*)"', line):
                    attrs[m.group(1).lower()] = m.group(2)
                current = {
                    "id": attrs.get("tvg-id") or attrs.get("tvg-name") or name,
                    "logo": attrs.get("tvg-logo") or "",
                    "group": attrs.get("group-title") or "Общие",
                    "name": name, "url": ""
                }
            elif line.startswith('http') and current:
                current["url"] = line
                ch.append(current)
                current = {}
        return ch

    # --- Xtream: каналы + фильмы (VOD) + сериалы (ПОСЛЕДОВАТЕЛЬНО с ретраями) ---
    def _parse_xtream(self, h):
        """Загружает все 3 раздела Xtream ПОСЛЕДОВАТЕЛЬНО с ретраями.

        Почему не параллельно: большинство Xtream-серверов ограничивают
        max_connections (часто = 1). Параллельные запросы ломают лимит →
        сервер режектит часть ответов → пустой JSON → ошибка.
        Последовательный подход с keep-alive и ретраями — надёжный и быстрый."""
        base = self.host.rstrip('/')
        api_base = f"{base}/player_api.php?username={self.user}&password={self.pwd}"
        results = {}

        def fetch(action, retries=4):
            """Запрос с ретраями + умная распаковка Xtream-обёрток.
            Сервер может вернуть:
              - [...список...]               → норма
              - {"js": [...список...]}       → стандартная Xtream-обёртка, распаковать
              - {"user_info":...}            → auth-ответ (нет данных) → []
            """
            url = f"{api_base}&action={action}"
            for attempt in range(retries):
                try:
                    r = requests.get(url, headers={**h, "Connection": "close"}, timeout=45)
                    txt = r.text.strip()
                    if not txt:
                        raise ValueError("пустой ответ")
                    data = json.loads(txt)
                    # Распаковка Xtream-обёртки {"js": [...]}
                    if isinstance(data, dict):
                        if "js" in data:
                            inner = data["js"]
                            return inner if isinstance(inner, list) else []
                        # Нет ключа js — это auth/error-ответ без данных
                        if "user_info" in data:
                            raise ValueError("auth-сброс (повтор)")
                        raise ValueError(f"dict без данных: {list(data.keys())}")
                    if isinstance(data, list):
                        return data
                    raise ValueError(f"неожиданный тип: {type(data).__name__}")
                except Exception as e:
                    if attempt < retries - 1:
                        time.sleep(1.5)
                        continue
                    print(f"⚠️ Xtream {action}: {e}")
                    return []
            return []

        # ПОСЛЕДОВАТЕЛЬНО: live → VOD → series (keep-alive переиспользует соединение)
        for action in ["get_live_categories", "get_live_streams",
                       "get_vod_categories", "get_vod_streams",
                       "get_series_categories", "get_series"]:
            results[action] = fetch(action)

        def catmap(action):
            """Безопасное извлечение категорий: сервер Xtream иногда отдаёт
            строку-ошибку или None вместо списка словарей. Пропускаем мусор."""
            m = {}
            raw = results.get(action)
            if not isinstance(raw, list):
                return m
            for c in raw:
                if isinstance(c, dict):
                    cid = c.get('category_id')
                    if cid is not None:
                        m[str(cid)] = c.get('category_name', '')
            return m

        def safe_list(action):
            """Возвращает только словари из ответа сервера (фильтрует строки/None)."""
            raw = results.get(action)
            if not isinstance(raw, list):
                print(f"⚠️ Xtream {action}: сервер вернул {type(raw).__name__}, пропускаю")
                return []
            return [i for i in raw if isinstance(i, dict)]

        live_cats = catmap("get_live_categories")
        ch = []
        for i in safe_list("get_live_streams"):
            cid = str(i.get('category_id', ''))
            sid = i.get('stream_id')
            if sid is None:
                continue
            ch.append({
                "id": str(sid),
                "name": i.get('name', '') or "Без названия",
                "logo": i.get('stream_icon', '') or "",
                "group": live_cats.get(cid) or "Общие",
                "url": f"{base}/live/{self.user}/{self.pwd}/{sid}.m3u8"
            })

        vod_cats = catmap("get_vod_categories")
        movies = []
        for i in safe_list("get_vod_streams"):
            cid = str(i.get('category_id', ''))
            sid = i.get('stream_id')
            if sid is None:
                continue
            ext = i.get('container_extension') or 'mp4'
            movies.append({
                "id": "vod_" + str(sid),
                "name": i.get('name', '') or "Без названия",
                "logo": i.get('stream_icon', '') or "",
                "group": vod_cats.get(cid) or "Фильмы",
                "url": f"{base}/movie/{self.user}/{self.pwd}/{sid}.{ext}",
                "rating": i.get('rating_5based', 0) or 0,
                "plot": i.get('plot', '') or ""
            })

        series_cats = catmap("get_series_categories")
        series = []
        for i in safe_list("get_series"):
            cid = str(i.get('category_id', ''))
            sid = i.get('series_id')
            if sid is None:
                continue
            series.append({
                "id": "series_" + str(sid),
                "series_id": str(sid),
                "name": i.get('name', '') or "Без названия",
                "logo": i.get('cover', '') or "",
                "group": series_cats.get(cid) or "Сериалы",
                "plot": i.get('plot', '') or "",
                "rating": i.get('rating', 0) or 0
            })

        self.movies = movies
        self.series = series
        print(f"[Xtream] live={len(ch)}, movies={len(movies)}, series={len(series)}")
        return ch

    # --- Stalker ---
    def _parse_stalker(self, h):
        base = self.host.rstrip('/')
        h["X-User-MAC"], h["Cookie"] = self.mac, f"mac={self.mac}"
        hs = requests.get(f"{base}/server/load.php?type=stb&action=handshake", headers=h, timeout=15).json()
        tk = hs['js']['token']
        res = requests.get(
            f"{base}/server/load.php?type=itv&action=get_all_channels&token={tk}",
            headers=h, timeout=20).json()
        return [{"id": str(c.get('tvg_id', c.get('name'))), "name": c['name'], "logo": "",
                 "group": "Stalker", "url": c['url'].split(' ')[-1]} for c in res.get('js', [])]

    # --- EPG ---
    def _load_epg(self, url, h):
        epg_db = {}
        try:
            r = requests.get(url, headers=h, timeout=25)
            content = r.content
            if url.endswith('.gz') or content[:2] == b'\x1f\x8b':
                content = gzip.decompress(content)

            root = ElementTree.fromstring(content)

            for prog in root.findall('programme'):
                channel_id = prog.get('channel')
                title_node = prog.find('title')
                desc_node = prog.find('desc')
                if channel_id and title_node is not None:
                    start_time = prog.get('start', '').split(' ')[0]
                    try:
                        t = start_time[8:10] + ":" + start_time[10:12]
                    except Exception:
                        t = "00:00"
                    title = title_node.text
                    desc = desc_node.text if desc_node is not None else ""
                    epg_db.setdefault(channel_id, []).append({
                        "title": title, "time": t, "desc": desc, "start": start_time
                    })

            for cid in epg_db:
                epg_db[cid].sort(key=lambda x: x.get('start', ''))
        except Exception as e:
            print(f"⚠️ EPG load error: {e}")
        return epg_db


class EPGModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self._i = []

    def rowCount(self, p=None):
        return len(self._i)

    def data(self, index, role):
        if not index.isValid():
            return None
        v = self._i[index.row()]
        return {201: v.get("title"), 202: v.get("time"),
                203: v.get("desc"), 204: v.get("start")}.get(role)

    def roleNames(self):
        return {201: b"displayTitle", 202: b"displayTime", 203: b"desc", 204: b"startRaw"}

    def set_data(self, d):
        self.beginResetModel()
        self._i = d
        self.endResetModel()


# ============================================================
#  IPTV CORE
# ============================================================

class IPTVCore(QObject):
    statusChanged = Signal()
    playlistsChanged = Signal()
    channelsChanged = Signal()
    loadFinished = Signal()
    loadFailed = Signal(str)
    playingChanged = Signal(bool)
    volumeChanged = Signal()
    durationChanged = Signal()
    positionChanged = Signal()
    playbackStateChanged = Signal()
    qualityChanged = Signal(str)
    connectionQualityChanged = Signal(str)
    bufferingChanged = Signal()
    bufferingProgressChanged = Signal()
    segmentDownloaded = Signal(float)
    availableQualitiesChanged = Signal()
    bandwidthSaverChanged = Signal()
    contentModeChanged = Signal()
    seriesInfoReady = Signal(str)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self._s = "Ready"
        self._ch = []
        self._ed = {}
        self._fav_ids = set()
        self.current_playlist_id = None
        self._current_playlist_name = ""
        self._last_url = ""
        self._last_channel_name = ""
        self._last_category = ""
        self._target_code = "ALL"
        self._target_name = "Глобальный"
        self._em = EPGModel()
        self.player = None
        self._init = False
        self.w = None
        self._retry_count = 0
        self._is_buffering = False
        self._buffering_progress = 100
        self._connection_quality = "unknown"
        self._current_quality = "auto"
        self._available_qualities = ["auto", "ultra", "high", "medium", "low", "minimal"]
        self._qualities_analyzed = False

        # === КОНТЕНТ-МОДЕЛИ: каналы / фильмы / сериалы ===
        self._movies = []
        self._series = []
        self._content_mode = "live"
        self._xtream = {"host": "", "user": "", "pwd": ""}
        self._series_info_cache = {}
        self._current_proto = ""

        # === ИНДЕКСЫ ДЛЯ МГНОВЕННОЙ ФИЛЬТРАЦИИ (работает с любым объёмом) ===
        self._cat_cache = {}            # mode -> [sorted category names]
        self._group_index = {}          # mode -> {group: [channels]}
        self._epg_starts_cache = {}     # cid -> (sorted_starts_list, epg_list)

        # === ФЛАГИ ЭКОНОМИИ ТРАФИКА (по умолчанию ВЫКЛЮЧЕНЫ) ===
        self._disable_logos = False
        self._skip_country_detect = False

        # Потокобезопасная БД
        self.db = Database("premium.db")
        self.db.init_schema()

        # thread-safe сигнал уровня соединения
        self.segmentDownloaded.connect(self.update_connection_quality_from_speed)

        if HAS_MPV:
            self._init_mpv()

    # --------------------------------------------------------
    #  MPV
    # --------------------------------------------------------
    def _init_mpv(self):
        try:
            self.player = mpv.MPV(
                vo='gpu', hwdec='auto-copy', ytdl=False, osc=False,
                input_default_bindings=False, input_vo_keyboard=True,

                keep_open='yes', keep_open_pause='no', hr_seek='yes',
                network_timeout=CacheConfig.NETWORK_TIMEOUT,

                # === СТРОГИЙ ЛИМИТ КЭША (ТЗ: 200 МБ / 60 секунд) ===
                # Единый источник правды — CacheConfig. Больше нигде не переопределяем
                # «волшебными числами»: 200 МБ есть 200 МБ во всех профилях качества.
                cache='yes',
                cache_secs=CacheConfig.CACHE_SECS,
                demuxer_max_bytes=CacheConfig.MAX_BYTES,        # 200 MiB (ТЗ)
                demuxer_max_back_bytes=CacheConfig.MAX_BACK_BYTES,
                demuxer_readahead_secs=CacheConfig.READAHEAD_SECS,
                demuxer_hysteresis_secs=CacheConfig.HYSTERESIS_SECS,

                # Устойчивость к сети / live
                demuxer_lavf_o='reconnect=1,reconnect_streamed=1,reconnect_delay_max=3,bufsize=64KiB',
                force_seekable='yes',
                # hls-bitrate / audio / subs — НЕ задаём здесь: управляются через setQuality и меню.
            )
            self.player['user-agent'] = BROWSER_HEADERS['User-Agent']

            self._install_mpv_observers()
            self._init = True
            print("✅ MPV initialized "
                  f"(cache capped {CacheConfig.MAX_BYTES}/{CacheConfig.CACHE_SECS}s, "
                  f"hysteresis={CacheConfig.HYSTERESIS_SECS}s)")
        except Exception as e:
            print(f"❌ MPV init: {e}")

    def _install_mpv_observers(self):
        p = self.player

        @p.property_observer('time-pos')
        def on_time(_n, v):
            self.positionChanged.emit()
            if v is not None and v > 0:
                self.playingChanged.emit(True)
                self._retry_count = 0
                # Видео реально играет → поток работает отлично.
                # НЕ ставим poor от кратковременной буферизации при старте.
                if not self._is_buffering:
                    self._set_connection_quality("excellent")
                if not self._qualities_analyzed:
                    self._qualities_analyzed = True
                    QTimer.singleShot(0, self._update_available_qualities_from_tracks)

        @p.property_observer('duration')
        def on_dur(_n, v):
            self.durationChanged.emit()

        @p.property_observer('pause')
        def on_pause(_n, v):
            self.playbackStateChanged.emit()
            self.playingChanged.emit(not (v if v is not None else True))

        @p.property_observer('volume')
        def on_vol(_n, v):
            self.volumeChanged.emit()

        @p.property_observer('avsync')
        def on_avsync(_n, v):
            if v is not None and abs(v) > 1.0:
                print(f"⚠️ AV desync detected: {v}")

        try:
            @p.property_observer('paused-for-cache')
            def on_paused_for_cache(_n, v):
                self._is_buffering = bool(v) if v is not None else False
                self.bufferingChanged.emit()
                if self._is_buffering:
                    self._set_status("Буферизация...")
                    # Не ставим poor при кратковременной буферизации —
                    # подождём, видео может продолжиться через секунду.
                    # Quality останется как было (good/excellent от time-pos).
                else:
                    self._retry_count = 0
                    self._set_status("Воспроизведение...")
                    self._set_connection_quality("excellent")
        except Exception as e:
            print(f"⚠️ Failed to observe paused-for-cache: {e}")

        try:
            @p.property_observer('cache-buffering-state')
            def on_cache_buffering_state(_n, v):
                self._buffering_progress = int(v) if v is not None else 100
                self.bufferingProgressChanged.emit()
        except Exception as e:
            print(f"⚠️ Failed to observe cache-buffering-state: {e}")

        try:
            @p.property_observer('video-params')
            def on_video_params(_n, v):
                if v:
                    QTimer.singleShot(0, self._update_available_qualities_from_tracks)
        except Exception as e:
            print(f"⚠️ Failed to observe video-params: {e}")

        @p.event_callback('end-file')
        def on_end(event):
            try:
                if event.data:
                    reason = event.data.reason
                    err = event.data.error
                    print(f"🎬 end-file: reason={reason}, error={err}")
                    if reason == 0:
                        self._handle_playback_end()
                    elif reason == 4:
                        self._handle_playback_error(err)
            except Exception as e:
                print(f"⚠️ end-file handler: {e}")

        @p.event_callback('log-message')
        def on_log(event):
            if event.text:
                t = event.text.lower()
                if 'buffering' in t or 'underrun' in t:
                    self._set_connection_quality("poor")
                    print("📶 Connection: POOR (buffering)")
                elif 'cache' in t and 'full' in t:
                    self._set_connection_quality("good")

        @p.event_callback('seek')
        def on_seek(event):
            print("[Player] Seeking...")

    # --------------------------------------------------------
    #  ХЕЛПЕРЫ СОСТОЯНИЯ
    # --------------------------------------------------------
    def _set_status(self, msg):
        self._s = msg
        self.statusChanged.emit()

    def _set_connection_quality(self, q):
        if self._connection_quality != q:
            self._connection_quality = q
            self.connectionQualityChanged.emit(q)

    # --------------------------------------------------------
    #  ПЕРЕПОДКЛЮЧЕНИЕ
    # --------------------------------------------------------
    def _schedule_reconnect(self):
        if self._retry_count < 5:
            self._retry_count += 1
            delay = 2 ** self._retry_count  # 2, 4, 8, 16, 32 сек
            print(f"[Player] Reconnecting in {delay}s... (attempt {self._retry_count}/5)")
            self._set_status(f"Переподключение (попытка {self._retry_count}/5 через {delay}с)...")
            self._set_connection_quality("poor")
            QTimer.singleShot(delay * 1000, self._do_reconnect)
        else:
            print("❌ Reconnect failed: max attempts reached.")
            self._set_status("Ошибка: потеряно соединение")
            self._retry_count = 0

    def _needs_proxy(self, url):
        ul = url.lower()
        return any(d in ul for d in PROXY_DOMAINS) or 'iframe' in ul

    def _play_url(self, url):
        """Проигрывает URL через прямой доступ или прокси — единая точка запуска."""
        if self._needs_proxy(url):
            start_hls_proxy(None, core=self)
            self.player.play(get_proxied_url(url))
            print(f"📡 Using optimized proxy: {get_proxied_url(url)[:60]}...")
        else:
            print("🎬 Playing directly...")
            self.player.play(url)

    def _do_reconnect(self):
        if not self._last_url:
            return
        print(f"[Player] Reconnecting attempt {self._retry_count}/5...")
        try:
            self._play_url(self._last_url)
            self._apply_stream_optimizations()
        except Exception as e:
            print(f"[Player] Reconnect error: {e}")
            self._schedule_reconnect()

    def _handle_playback_end(self):
        try:
            is_live = (self.duration == 0.0)
            if is_live and self._last_url:
                print("📺 Live stream EOF, treating as disconnect...")
                self._schedule_reconnect()
            else:
                self._retry_count = 0
                self._set_status("Конец воспроизведения")
        except Exception as e:
            print(f"⚠️ handle_playback_end error: {e}")

    def _handle_playback_error(self, err):
        print(f"⚠️ Playback error: {err}")
        self._schedule_reconnect()

    def _test_connection_quality(self, url):
        """Тест соединения через GET (а не HEAD — Xtream-серверы режектят HEAD).
        Если тест не удался — НЕ ставим 'poor', оставляем 'unknown'
        и ждём обновления от скорости скачивания сегментов."""
        try:
            start = time.time()
            # GET со stream=True — открываем и сразу закрываем, не качая тело
            r = requests.get(url, headers={**BROWSER_HEADERS, "Connection": "close"},
                             timeout=10, stream=True, allow_redirects=True)
            r.close()
            duration = time.time() - start
            if r.status_code in (200, 301, 302):
                if duration < 0.5:
                    q = "excellent"
                elif duration < 1.5:
                    q = "good"
                elif duration < 3:
                    q = "fair"
                else:
                    q = "poor"
                self._set_connection_quality(q)
                print(f"📶 Connection quality: {q.upper()}")
                return q
        except Exception:
            pass
        # НЕ ставим 'poor' — пусть обновится от скорости сегментов
        print("📶 Connection test skipped, waiting for segment speed...")
        return "unknown"

    @Slot(float)
    def update_connection_quality_from_speed(self, speed_kb):
        # Не ставим poor просто из-за кратковременной буферизации при старте —
        # оцениваем реальную скорость скачивания сегментов
        if speed_kb > 2500:
            quality = "excellent"
        elif speed_kb > 1000:
            quality = "good"
        elif speed_kb > 300:
            quality = "fair"
        else:
            quality = "poor"
        self._set_connection_quality(quality)
        if quality != self._connection_quality:
            print(f"📶 [Connection] Speed updated: {speed_kb:.1f} KB/s -> {quality}")

    # --------------------------------------------------------
    #  QML PROPERTIES
    # --------------------------------------------------------
    @Property(str, notify=statusChanged)
    def status(self): return self._s

    @Property(list, notify=playlistsChanged)
    def playlists(self):
        return self.db.fetchall(
            "SELECT id, name, proto, host, epg, user, pwd, mac FROM playlists ORDER BY id DESC")

    @Property(str, notify=channelsChanged)
    def current_playlist_name(self): return self._current_playlist_name

    @Property(list, notify=channelsChanged)
    def categories(self):
        # O(1): берём готовый кэш, построенный при loadPlaylist.
        cats = self._cat_cache.get(self._content_mode)
        if cats is None:
            self._rebuild_indexes()
            cats = self._cat_cache.get(self._content_mode, [])
        return ["Все", "★ Избранные"] + cats

    @Property(str, notify=contentModeChanged)
    def contentMode(self): return self._content_mode

    @Property(bool, notify=channelsChanged)
    def hasVod(self): return len(self._movies) > 0

    @Property(bool, notify=channelsChanged)
    def hasSeries(self): return len(self._series) > 0

    @Slot(str)
    def setContentMode(self, mode):
        if mode in ("live", "movies", "series") and mode != self._content_mode:
            self._content_mode = mode
            self._filtered_cache = None
            self.contentModeChanged.emit()
            self.channelsChanged.emit()

    # Потолок выдачи за один вызов: QML-ListView виртуальный, но сам list
    # из Python в QML копируется целиком — поэтому режем, чтобы при любом
    # объёме (хоть 99 трлн) фильтрация оставалась мгновенной. Остальное
    # подгружается через getMoreFiltered при прокрутке к концу.
    _MAX_RESULT = 800
    _filtered_cache = None
    _filtered_state = None

    @Slot(str, str, result=list)
    def getFilteredItems(self, cat, query):
        """Универсальный фильтр. Использует индекс по группам (O(1) для выбора
        категории) и лимит выдачи. Поиск выполняется только внутри выбранной
        группы — а не по всему массиву."""
        src = self._items_for_mode()
        q = query.lower().strip()
        if not q:
            pool = self._group_pool(cat, src)
        else:
            pool = self._group_pool(cat, src)
            pool = [c for c in pool if q in (c.get("name", "") or "").lower()]
        self._filtered_cache = pool
        self._filtered_state = (cat, q)
        return pool[:self._MAX_RESULT] if len(pool) > self._MAX_RESULT else pool

    def _items_for_mode(self):
        if self._content_mode == "movies":
            return self._movies
        if self._content_mode == "series":
            return self._series
        return self._ch

    def _group_pool(self, cat, full_src):
        """Возвращает исходный массив для фильтрации по категории — мгновенно,
        через готовый индекс _group_index, без полного обхода."""
        if cat in ("Все", "Все каналы"):
            return full_src
        if cat == "★ Избранные":
            return [c for c in full_src if c.get("id") in self._fav_ids]
        gi = self._group_index.get(self._content_mode, {})
        return gi.get(cat, [])

    @Slot(result=list)
    def getMoreFiltered(self):
        """Пагинация: следующую порцию каналов для подгрузки при прокрутке."""
        pool = self._filtered_cache or []
        start = getattr(self, "_filtered_offset", self._MAX_RESULT)
        chunk = pool[start:start + self._MAX_RESULT]
        self._filtered_offset = start + self._MAX_RESULT
        return chunk

    @Property(str, notify=connectionQualityChanged)
    def connectionQuality(self): return self._connection_quality

    @Property(str, notify=qualityChanged)
    def currentQuality(self): return self._current_quality

    @Property(list, notify=availableQualitiesChanged)
    def availableQualities(self): return self._available_qualities

    @Property(bool, notify=availableQualitiesChanged)
    def ultraAvailable(self): return "ultra" in self._available_qualities

    @Property(bool, notify=availableQualitiesChanged)
    def highAvailable(self): return "high" in self._available_qualities

    @Property(bool, notify=availableQualitiesChanged)
    def mediumAvailable(self): return "medium" in self._available_qualities

    @Property(bool, notify=availableQualitiesChanged)
    def lowAvailable(self): return "low" in self._available_qualities

    @Property(bool, notify=availableQualitiesChanged)
    def minimalAvailable(self): return "minimal" in self._available_qualities

    # --- Флаги экономии трафика ---
    @Property(bool, notify=bandwidthSaverChanged)
    def disableLogos(self): return self._disable_logos

    @disableLogos.setter
    def disableLogos(self, val):
        if self._disable_logos != bool(val):
            self._disable_logos = bool(val)
            self.bandwidthSaverChanged.emit()
            print(f"[SAVER] Логотипы {'ВЫКЛЮЧЕНЫ' if self._disable_logos else 'ВКЛЮЧЕНЫ'}")

    @Property(bool, notify=bandwidthSaverChanged)
    def skipCountryDetect(self): return self._skip_country_detect

    @skipCountryDetect.setter
    def skipCountryDetect(self, val):
        if self._skip_country_detect != bool(val):
            self._skip_country_detect = bool(val)
            self.bandwidthSaverChanged.emit()
            print(f"[SAVER] Детект страны {'ВЫКЛЮЧЕН' if self._skip_country_detect else 'ВКЛЮЧЕН'}")

    @Property(bool, notify=bandwidthSaverChanged)
    def forceLowestVariant(self): return stream_optimizer.force_lowest_variant

    @forceLowestVariant.setter
    def forceLowestVariant(self, val):
        new = bool(val)
        if stream_optimizer.force_lowest_variant != new:
            stream_optimizer.force_lowest_variant = new
            HLSCache.clear()  # немедленное применение нового выбора варианта
            self.bandwidthSaverChanged.emit()
            print(f"[SAVER] Принудительный lowest ABR variant: {'ВКЛ' if new else 'ВЫКЛ'}")

    # --- Playback ---
    @Property(bool, notify=bufferingChanged)
    def isBuffering(self): return self._is_buffering

    @Property(int, notify=bufferingProgressChanged)
    def bufferingProgress(self): return self._buffering_progress

    @Property(float, notify=positionChanged)
    def position(self):
        if HAS_MPV and self.player and self._init:
            v = self.player.time_pos
            if v is not None:
                return float(v)
        return 0.0

    @position.setter
    def position(self, val):
        if HAS_MPV and self.player and self._init:
            try:
                self.player.time_pos = float(val)
                if self.player.pause:
                    self.player.pause = False
                self.positionChanged.emit()
            except Exception:
                pass

    @Property(float, notify=durationChanged)
    def duration(self):
        if HAS_MPV and self.player and self._init:
            v = self.player.duration
            if v is not None:
                return float(v)
        return 0.0

    @Property(int, notify=volumeChanged)
    def volume(self):
        if HAS_MPV and self.player and self._init:
            v = self.player.volume
            if v is not None:           # фикс: громкость 0 больше не превращается в 80
                return int(v)
        return 80

    @volume.setter
    def volume(self, val):
        if HAS_MPV and self.player and self._init:
            try:
                self.player.volume = int(val)
                self.volumeChanged.emit()
            except Exception:
                pass

    @Property(bool, notify=playbackStateChanged)
    def isPaused(self):
        if HAS_MPV and self.player and self._init:
            return bool(self.player.pause)
        return False

    @isPaused.setter
    def isPaused(self, val):
        if HAS_MPV and self.player and self._init:
            self.player.pause = bool(val)
            self.playbackStateChanged.emit()

    @Slot()
    def togglePause(self):
        if HAS_MPV and self.player and self._init:
            try:
                if self.player.time_pos and self.player.duration \
                        and self.player.time_pos >= self.player.duration - 1:
                    self.player.time_pos = 0.0
            except Exception:
                pass
            self.player.pause = not self.player.pause
            self.playbackStateChanged.emit()

    @Property(str, notify=statusChanged)
    def targetCode(self): return self._target_code

    @Property(str, notify=statusChanged)
    def targetName(self): return self._target_name

    @Property(QObject, constant=True)
    def epgModel(self): return self._em

    # --------------------------------------------------------
    #  ПЛЕЙЛИСТЫ
    # --------------------------------------------------------
    @Slot(int)
    def loadPlaylist(self, pid):
        r = self.db.fetchone(
            "SELECT name, proto, channels, epg_db, movies, series, "
            "xtream_host, xtream_user, xtream_pwd FROM playlists WHERE id=?", (pid,))
        if r:
            self.current_playlist_id = pid
            self._current_playlist_name = r["name"]
            self._current_proto = (r.get("proto") or "").upper()
            try:
                self._ch = json.loads(r["channels"])
                self._ed = json.loads(r["epg_db"])
            except Exception:
                self._ch, self._ed = [], {}
            self._movies = json.loads(r.get("movies") or "[]")
            self._series = json.loads(r.get("series") or "[]")
            self._xtream = {"host": r.get("xtream_host") or "",
                            "user": r.get("xtream_user") or "",
                            "pwd": r.get("xtream_pwd") or ""}
            self._content_mode = "live"
            self._series_info_cache = {}
            self._rebuild_indexes()   # мгновенная фильтрация и EPG
            favs = self.db.fetchall(
                "SELECT channel_id FROM favorites WHERE playlist_id=?", (pid,))
            self._fav_ids = {f["channel_id"] for f in favs}
            self.channelsChanged.emit()
            self.contentModeChanged.emit()

    def _rebuild_indexes(self):
        """Индексы для мгновенной фильтрации/EPG. Строится один раз при загрузке."""
        self._cat_cache = {}
        self._group_index = {}
        for mode, src in (("live", self._ch), ("movies", self._movies), ("series", self._series)):
            groups = {}
            cats = set()
            for c in src:
                g = c.get("group", "Общие") or "Общие"
                cats.add(g)
                groups.setdefault(g, []).append(c)
            self._cat_cache[mode] = sorted(cats)
            self._group_index[mode] = groups
        self._epg_starts_cache = {}
        for cid, plist in self._ed.items():
            self._epg_starts_cache[cid] = sorted(p.get("start", "") for p in plist)

    @Slot(str, str, result=list)
    def getFilteredChannels(self, cat, query):
        """Каналы: тот же механизм — индекс по группам + лимит."""
        src = self._ch
        q = query.lower().strip()
        pool = self._group_pool(cat, src)
        if q:
            pool = [c for c in pool if q in (c.get("name", "") or "").lower()]
        self._filtered_cache = pool
        self._filtered_state = (cat, q)
        self._filtered_offset = self._MAX_RESULT
        return pool[:self._MAX_RESULT] if len(pool) > self._MAX_RESULT else pool

    @Slot(str, result=bool)
    def toggleFavorite(self, cid):
        if not self.current_playlist_id:
            return False
        if cid in self._fav_ids:
            self._fav_ids.discard(cid)
            self.db.execute(
                "DELETE FROM favorites WHERE playlist_id=? AND channel_id=?",
                (self.current_playlist_id, cid))
        else:
            self._fav_ids.add(cid)
            self.db.execute(
                "INSERT OR REPLACE INTO favorites (playlist_id, channel_id) VALUES (?, ?)",
                (self.current_playlist_id, cid))
        self.db.commit()
        self.channelsChanged.emit()
        return cid in self._fav_ids

    @Slot(str, result=bool)
    def isFavorite(self, cid):
        return cid in self._fav_ids

    @Slot(str, str, str, str, str, str, str)
    def addPlaylist(self, name, proto, host, epg, user, pwd, mac):
        if not name.strip() or not host.strip():
            self._set_status("Ошибка: Имя и URL обязательны!")
            return
        self._set_status("Подключение...")
        self.w = IPTVWorker(proto, host, epg, user, pwd, mac)
        self.w.finished.connect(
            lambda ch, epg_db, msg: self._on_loaded(name, proto, host, epg, user, pwd, mac, ch, epg_db))
        self.w.error.connect(self._on_error)
        self.w.start()

    def _on_loaded(self, name, proto, host, epg, user, pwd, mac, ch, epg_db):
        movies = getattr(self.w, "movies", [])
        series = getattr(self.w, "series", [])
        # Для Xtream валиден, если есть ХОТЯ БЫ ОДИН раздел (live ИЛИ фильмы ИЛИ сериалы).
        # Раньше проверялся только ch (каналы) — если сервер вернул пустой live,
        # но отдал фильмы/сериалы, плейлист считался «пустым» → ложная ошибка.
        if not ch and not movies and not series:
            print(f"❌ [Load] ПУСТОЙ ПЛЕЙЛИСТ: ch={len(ch)}, movies={len(movies)}, series={len(series)}")
            self._set_status("Ошибка: пустой плейлист!")
            self.loadFailed.emit("Плейлист пуст (сервер не вернул ни каналов, ни фильмов, ни сериалов)")
            return
        try:
            is_xt = (proto or "").upper().startswith("XTREAM")
            self.db.execute(
                "INSERT INTO playlists (name, proto, host, epg, user, pwd, mac, channels, epg_db, "
                "movies, series, xtream_host, xtream_user, xtream_pwd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, proto, host, epg, user, pwd, mac, json.dumps(ch), json.dumps(epg_db),
                 json.dumps(movies), json.dumps(series),
                 host if is_xt else "", user if is_xt else "", pwd if is_xt else ""))
            self.db.commit()
            self._set_status("Добавлено!")
            self.playlistsChanged.emit()
            self.loadFinished.emit()
        except Exception as e:
            self._set_status(f"Ошибка БД: {e}")
            self.loadFailed.emit(str(e))

    def _on_error(self, msg):
        print(f"❌ [Core] ОШИБКА ЗАГРУЗКИ ПЛЕЙЛИСТА: {msg}")
        self._set_status(f"Ошибка: {msg}")
        self.loadFailed.emit(msg)

    @Slot()
    def cancelConnection(self):
        if self.w:
            try:
                self.w.finished.disconnect()
                self.w.error.disconnect()
            except Exception:
                pass
            self._set_status("Отменено")

    @Slot('QVariant')
    def deletePlaylist(self, pid):
        if not pid:
            return
        try:
            if hasattr(pid, 'toVariant'):
                pid = pid.toVariant()
            pid = int(pid)
            self.db.execute("DELETE FROM playlists WHERE id=?", (pid,))
            self.db.execute("DELETE FROM favorites WHERE playlist_id=?", (pid,))
            self.db.commit()
            self.playlistsChanged.emit()
        except Exception as e:
            print(f"❌ Delete: {e}")

    # --------------------------------------------------------
    #  EPG
    # --------------------------------------------------------
    @Slot(str)
    def updateEPG(self, cid):
        self._em.set_data(self._ed.get(cid, []))

    @Slot(str, result=str)
    def getCurrentEPG(self, cid):
        """Текущая передача. starts берётся из предвычисленного кэша
        (_epg_starts_cache) — без пересоздания списка при каждом вызове.
        Поэтому даже при десятках тысяч каналов это мгновенный bisect."""
        epg_list = self._ed.get(cid)
        if not epg_list:
            return "Нет программы"
        starts = self._epg_starts_cache.get(cid)
        if starts is None:
            starts = sorted(p.get("start", "") for p in epg_list)
            self._epg_starts_cache[cid] = starts
        import bisect
        now = datetime.now().strftime("%Y%m%d%H%M%S")
        idx = bisect.bisect_right(starts, now)
        if idx > 0:
            return epg_list[idx - 1].get("title", "") or "Нет программы"
        return epg_list[0].get("title", "") or "Нет программы"

    @Slot(str, str, result=str)
    def getArchiveUrl(self, url, start_raw):
        return _append_utc(url, start_raw)

    # --------------------------------------------------------
    #  СЕРИАЛЫ: сезоны и серии (get_series_info)
    # --------------------------------------------------------
    @Slot(str)
    def loadSeriesInfo(self, series_id):
        """Асинхронно грузит get_series_info и кэширует; эммитит seriesInfoReady."""
        # чистим числовой series_id (в списке хранится как series_123 → 123)
        sid = str(series_id).replace("series_", "")
        if sid in self._series_info_cache:
            self.seriesInfoReady.emit(sid)
            return
        threading.Thread(target=self._fetch_series_info, args=(sid,), daemon=True).start()

    def _fetch_series_info(self, sid):
        try:
            host = self._xtream.get("host", "").rstrip("/")
            user = self._xtream.get("user", "")
            pwd = self._xtream.get("pwd", "")
            if not host:
                return
            url = (f"{host}/player_api.php?username={user}&password={pwd}"
                   f"&action=get_series_info&series_id={sid}")
            data = http_session.get(url, headers=BROWSER_HEADERS, timeout=30).json()
            self._series_info_cache[sid] = data
            self.seriesInfoReady.emit(sid)
            print(f"[Series] info loaded for {sid}: seasons={list(data.get('episodes', {}).keys())}")
        except Exception as e:
            print(f"[Series] info error: {e}")

    @Slot(str, result='QVariant')
    def getSeriesSeasons(self, series_id):
        """Возвращает список сезонов [{id, name, episode_count}] для сериала."""
        sid = str(series_id).replace("series_", "")
        info = self._series_info_cache.get(sid, {})
        episodes = info.get("episodes", {})
        seasons = []
        for sk in sorted(episodes.keys(), key=lambda x: int(x) if str(x).isdigit() else 999):
            elist = episodes.get(sk, [])
            seasons.append({
                "id": str(sk),
                "name": f"Сезон {sk}",
                "episode_count": len(elist)
            })
        return seasons

    @Slot(str, str, result='QVariant')
    def getSeasonEpisodes(self, series_id, season):
        """Возвращает серии сезона [{title, episode_num, url}]."""
        import re as _re
        sid = str(series_id).replace("series_", "")
        info = self._series_info_cache.get(sid, {})
        episodes = info.get("episodes", {}).get(str(season), [])
        host = self._xtream.get("host", "").rstrip("/")
        user = self._xtream.get("user", "")
        pwd = self._xtream.get("pwd", "")
        result = []
        for idx, ep in enumerate(episodes):
            eid = ep.get("id")
            ext = ep.get("container_extension", "mp4")
            url = f"{host}/series/{user}/{pwd}/{eid}.{ext}" if host and eid else ""
            title = ep.get("title", "") or f"Эпизод {idx + 1}"
            # episode_num может быть int, str, или отсутствовать.
            # Фолбэк: достаём из title "S01E0001" → "1"
            ep_num = ep.get("episode_num", "")
            if ep_num in (None, "", 0, "0"):
                m = _re.search(r'E(\d+)', title)
                ep_num = str(int(m.group(1))) if m else str(idx + 1)
            else:
                ep_num = str(ep_num).lstrip("0") or "0"
            result.append({
                "title": title,
                "episode_num": ep_num,
                "url": url,
                "id": str(eid) if eid is not None else str(idx)
            })
        return result

    # --------------------------------------------------------
    #  ПРЕДЗАГРУЗКА КАНАЛОВ (мгновенный старт)
    # --------------------------------------------------------
    _prefetch_semaphore = None
    _prefetched_recently = {}
    _MAX_CONCURRENT_PREFETCH = 4
    _MAX_PREFETCH_BATCH = 30
    _PREFETCH_RETRIES = 3

    @Slot('QVariant')
    def prefetchChannel(self, ch):
        if not ch or not isinstance(ch, dict):
            return
        url = ch.get('url', '')
        if not url:
            return
        # защита от роста
        if len(self._prefetched_recently) > 500:
            cutoff = time.time() - 600
            self._prefetched_recently = {k: v for k, v in self._prefetched_recently.items() if v > cutoff}
        self._prefetched_recently[url] = time.time()
        threading.Thread(target=self._do_prefetch, args=(ch,), daemon=True).start()

    def _do_prefetch(self, ch):
        if self._prefetch_semaphore is None:
            self._prefetch_semaphore = threading.Semaphore(self._MAX_CONCURRENT_PREFETCH)
        if not self._prefetch_semaphore.acquire(timeout=15):
            return
        try:
            url = ch.get('url', '')
            if not url:
                return
            cache = HLSCache(None, stream_optimizer.quality_level)
            content, _ = cache.fetch_with_headers(url)
            if not content:
                return
            # Ищем URI варианта или первый сегмент
            try:
                lines = content.split('\n')
                variant_uri = None
                for i, line in enumerate(lines):
                    if '#EXT-X-STREAM-INF' in line:
                        for j in range(i + 1, len(lines)):
                            cand = lines[j].strip()
                            if cand and not cand.startswith('#'):
                                variant_uri = cand
                                break
                        if variant_uri:
                            break
                if not variant_uri:
                    for line in lines:
                        s = line.strip()
                        if s and not s.startswith('#') and '.ts' in s.lower():
                            variant_uri = s
                            break
                if variant_uri:
                    abs_url = make_absolute(variant_uri, url.rsplit('/', 1)[0] + '/', url)
                    for attempt in range(self._PREFETCH_RETRIES):
                        try:
                            http_session.get(abs_url, headers=BROWSER_HEADERS, timeout=10, stream=True)
                            break
                        except Exception:
                            if attempt == self._PREFETCH_RETRIES - 1:
                                raise
            except Exception:
                pass
        except Exception:
            pass
        finally:
            self._prefetch_semaphore.release()

    @Slot()
    def prefetchVisibleChannels(self):
        if not self._ch:
            return
        for ch in self._ch[:self._MAX_PREFETCH_BATCH]:
            self.prefetchChannel(ch)

    @Slot('QVariant')
    def recordChannelClick(self, ch):
        try:
            if not ch or not isinstance(ch, dict):
                return
            now = time.time()
            ts_struct = time.localtime(now)
            self.db.execute(
                "INSERT INTO click_history (ts, channel_id, channel_name, category, playlist_id, hour, weekday) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (int(now), str(ch.get('id', '')), str(ch.get('name', '')),
                 str(ch.get('group', '')),
                 int(self.current_playlist_id) if self.current_playlist_id else 0,
                 ts_struct.tm_hour, ts_struct.tm_wday))
            self.db.commit()
        except Exception as e:
            print(f"[Predictor] record error: {e}")

    def _all_content(self):
        """Все элементы контента (каналы + фильмы + сериалы) — для поиска предсказаний."""
        return self._ch + self._movies + self._series

    @Slot('QVariant', result='QVariant')
    def predictNextChannel(self, current_ch):
        """Многоступенчатое предсказание + префетч топ-кандидатов.
        Учитывает последовательность, время суток, день недели, категорию.
        Ищет кандидатов по ВСЕМУ контенту (каналы + фильмы + сериалы)."""
        try:
            if not current_ch or not isinstance(current_ch, dict):
                return None
            cur_id = str(current_ch.get('id', ''))
            cur_cat = str(current_ch.get('group', ''))
            now_struct = time.localtime(time.time())
            cur_hour, cur_wday = now_struct.tm_hour, now_struct.tm_wday

            candidates = {}

            def _add(cid, name, cat, score, source):
                if not cid or cid == cur_id:
                    return
                if cid in candidates:
                    old = candidates[cid]
                    candidates[cid] = (old[0], old[1], old[2] + score, source + "+" + old[3])
                else:
                    candidates[cid] = (name, cat, score, source)

            # 1. Последовательная (>=3 подтверждений)
            for r in self.db.fetchall(
                """SELECT ch2.channel_id AS cid, ch2.channel_name AS cname, ch2.category AS cat, COUNT(*) AS cnt
                   FROM click_history ch1
                   JOIN click_history ch2 ON ch2.ts > ch1.ts AND ch2.ts < ch1.ts + 120
                                          AND ch2.channel_id != ch1.channel_id
                   WHERE ch1.channel_id = ? AND ABS(ch1.hour - ?) <= 2
                   GROUP BY ch2.channel_id ORDER BY cnt DESC LIMIT 5""",
                (cur_id, cur_hour)):
                if r["cnt"] >= 3:
                    _add(r["cid"], r["cname"], r["cat"], r["cnt"] * 10, "seq")

            # 2. Популярный в этот час (±1)
            for r in self.db.fetchall(
                """SELECT channel_id AS cid, channel_name AS cname, category AS cat, COUNT(*) AS cnt
                   FROM click_history WHERE ABS(hour - ?) <= 1
                   GROUP BY channel_id ORDER BY cnt DESC LIMIT 5""",
                (cur_hour,)):
                _add(r["cid"], r["cname"], r["cat"], r["cnt"] * 2, "hour")

            # 3. Популярный в этот день недели
            for r in self.db.fetchall(
                """SELECT channel_id AS cid, channel_name AS cname, category AS cat, COUNT(*) AS cnt
                   FROM click_history WHERE weekday = ?
                   GROUP BY channel_id ORDER BY cnt DESC LIMIT 5""",
                (cur_wday,)):
                _add(r["cid"], r["cname"], r["cat"], r["cnt"], "weekday")

            # 4. Из той же категории
            if cur_cat:
                for r in self.db.fetchall(
                    """SELECT channel_id AS cid, channel_name AS cname, COUNT(*) AS cnt
                       FROM click_history WHERE category = ?
                       GROUP BY channel_id ORDER BY cnt DESC LIMIT 3""",
                    (cur_cat,)):
                    _add(r["cid"], r["cname"], cur_cat, r["cnt"] * 0.5, "cat")

            if not candidates:
                return None

            sorted_cands = sorted(candidates.items(), key=lambda x: -x[1][2])
            top5 = sorted_cands[:5]
            # Ищем кандидатов по ВСЕМУ контенту (каналы + фильмы + сериалы)
            pool = self._all_content()
            for cid, _ in top5:
                for item in pool:
                    if item.get('id') == cid:
                        self.prefetchChannel(item)
                        break

            top_id, (top_name, top_cat, top_score, top_src) = top5[0]
            return {'id': top_id, 'name': top_name, 'category': top_cat,
                    'confidence': min(int(top_score), 100), 'source': top_src,
                    'candidates_count': len(top5)}
        except Exception as e:
            print(f"[Predictor] predict error: {e}")
        return None

    # --------------------------------------------------------
    #  КАЧЕСТВО
    # --------------------------------------------------------
    @Slot(str, result=str)
    def setQuality(self, quality):
        """Устанавливает качество. Профили кэша — из единого QUALITY_PROFILES.
        ABR (hls-bitrate) переключает поток с восстановлением позиции для архивов;
        масштабирование (vf scale) применяется НА ЛЕТУ без перезагрузки."""
        if self._current_quality == quality:
            return quality

        self._current_quality = quality
        stream_optimizer.quality_level = quality
        self.qualityChanged.emit(quality)
        print(f"📺 Quality set to: {quality}")

        if not (self.player and self._init):
            HLSCache.clear()
            return quality

        try:
            profile = QUALITY_PROFILES.get(quality, QUALITY_PROFILES["auto"])
            # 1. Потолок буфера под профиль (НЕ выше ТЗ-200МБ)
            self.player['cache'] = 'yes'
            self.player['demuxer-readahead-secs'] = profile["readahead"]
            self.player['demuxer-max-bytes'] = profile["max_bytes"]

            # 2. Масштабирование видео — НА ЛЕТУ (без перезагрузки потока)
            self._apply_video_scaling(quality)

            # 3. Переключение видеодорожки (мульти-битрейт внутри потока) — на лету
            self._apply_runtime_quality_track(quality)

            # 4. ABR-выбор через HLS-прокси (только для проксируемых потоков!).
            # Перезагрузка прямых Xtream-потоков с max_connections=1 убивает плейбэк.
            if self._last_url and self._needs_proxy(self._last_url):
                HLSCache.clear()
                pos = self.player.time_pos
                is_live = (self.duration == 0.0)

                print(f"🔄 [Quality] Reloading proxied stream for HLS variant '{quality}'...")
                self._play_url(self._last_url)

                if not is_live and pos is not None and pos > 0:
                    def restore_position():
                        time.sleep(1.2)
                        try:
                            self.player.time_pos = pos
                            print(f"🕒 [Quality] Position restored to {pos:.1f}s")
                        except Exception:
                            pass
                    threading.Thread(target=restore_position, daemon=True).start()
            else:
                print(f"📺 [Quality] Direct stream — только vf-масштабирование, без релоада")
        except Exception as e:
            print(f"⚠️ Quality setting error: {e}")

        HLSCache.clear()
        return quality

    def _apply_video_scaling(self, quality):
        """Масштабирование через vf-фильтр. hwdec=auto-copy установлен при
        инициализации и БОЛЬШЕ НЕ МЕНЯЕТСЯ — меняем только vf.
        auto-copy = аппаратный декодер + копирование кадра в CPU → vf работает."""
        try:
            original_height = None
            v_params = getattr(self.player, 'video_params', None)
            if v_params and isinstance(v_params, dict):
                original_height = v_params.get('h')
            if not original_height:
                vo_params = getattr(self.player, 'video_out_params', None)
                if vo_params and isinstance(vo_params, dict):
                    original_height = vo_params.get('h')

            if quality == "auto" or not original_height:
                self._set_vf("")
                print(f"🎬 [Quality] Native (source: {original_height or 'unknown'}p)")
                return

            target_height = QUALITY_HEIGHTS.get(quality, 720)
            if target_height == original_height:
                self._set_vf("")
                print(f"🎬 [Quality] Native {original_height}p — already at target")
            else:
                self._set_vf(f'scale=-2:{target_height}')
                direction = "Downscale" if target_height < original_height else "Upscale"
                print(f"🎬 [Quality] {direction} {original_height}p → {target_height}p")
        except Exception as e:
            print(f"⚠️ [Quality] scaling error: {e}")

    def _set_vf(self, spec):
        """Безопасно устанавливает vf-фильтр. Пробует несколько способов."""
        try:
            if not spec:
                # Очистка всех фильтров
                self.player.command('vf', 'set', '')
            else:
                # set заменяет всю цепочку фильтров
                self.player.command('vf', 'set', spec)
        except Exception:
            try:
                self.player['vf'] = spec
            except Exception:
                try:
                    if spec:
                        self.player.command('vf', 'add', spec)
                    else:
                        self.player.command('vf', 'clr', '')
                except Exception:
                    pass

    def _apply_runtime_quality_track(self, quality):
        if not self.player or not self._init:
            return
        try:
            tracks = getattr(self.player, 'track_list', []) or []
            video_tracks = [t for t in tracks if t.get('type') == 'video']
            if not video_tracks or len(video_tracks) <= 1:
                return

            if quality == "auto":
                self.player.vid = 'auto'
                print("🎯 [Real-time Quality] Set vid to 'auto'")
                return

            target_height = QUALITY_HEIGHTS.get(quality, 720)
            best_track, min_diff = None, 999999
            for t in video_tracks:
                h = t.get('demux-h') or t.get('height')
                if h is not None:
                    diff = abs(int(h) - target_height)
                    if diff < min_diff:
                        min_diff, best_track = diff, t
            if best_track:
                track_id = best_track.get('id')
                if track_id is not None:
                    self.player.vid = int(track_id)
                    print(f"🎯 [Real-time Quality] vid={track_id} "
                          f"(h={best_track.get('demux-h') or best_track.get('height')})")
        except Exception as e:
            print(f"⚠️ [Real-time Quality] track switch failed: {e}")

    @Slot(str, result=bool)
    def isQualityAvailable(self, quality):
        return quality in self._available_qualities

    def _update_available_qualities_from_tracks(self):
        """
        РЕАЛЬНЫЙ анализ доступных качеств по видеодорожкам / параметрам потока.
        Раньше всегда возвращался полный список (мёртвый код). Теперь кнопки в меню
        отключаются, если поток физически не поддерживает разрешение выше исходного.
        """
        result = ["auto"]
        try:
            height = None
            if self.player and self._init:
                # Учтём мульти-битрейт дорожки
                tracks = getattr(self.player, 'track_list', []) or []
                video_tracks = [t for t in tracks if t.get('type') == 'video']
                heights = []
                for t in video_tracks:
                    h = t.get('demux-h') or t.get('height')
                    if h:
                        heights.append(int(h))
                if heights:
                    height = max(heights)
                if height is None:
                    v_params = getattr(self.player, 'video_params', None)
                    if v_params and isinstance(v_params, dict):
                        height = v_params.get('h')

            if not height:
                # Пока не знаем разрешение — даём полный выбор
                self._available_qualities = ["auto", "ultra", "high", "medium", "low", "minimal"]
                self.availableQualitiesChanged.emit()
                print("📊 [Qualities] Resolution unknown — exposing all buttons")
                return

            # Включаем качества ДО исходного (downscale) + само исходное + upscale опции
            order = [("minimal", 360), ("low", 480), ("medium", 720),
                     ("high", 1080), ("ultra", 2160)]
            for name, h in order:
                if h <= height:
                    result.append(name)
            # upscale всегда доступен (Lanczos) — но только если исходник ниже
            for name, h in order:
                if h > height and name not in result:
                    result.append(name)

            # "auto" всегда первым
            ordered = ["auto"] + [n for n, _ in order if n in result]
            self._available_qualities = ordered
            self.availableQualitiesChanged.emit()
            print(f"📊 [Qualities] Source ~{height}p → enabled: {ordered}")
        except Exception as e:
            self._available_qualities = ["auto", "ultra", "high", "medium", "low", "minimal"]
            self.availableQualitiesChanged.emit()
            print(f"⚠️ [Qualities] analysis failed, fallback to all: {e}")

    # --------------------------------------------------------
    #  PLAY
    # --------------------------------------------------------
    @Slot(str, str, str, str)
    def play(self, url, name="", category="", start_raw=""):
        if not HAS_MPV or not self.player or not self._init:
            print("❌ Player unavailable")
            self._set_status("❌ MPV не инициализирован")
            return

        f_url = _append_utc(url, start_raw)

        print(f"🎬 Playing: {name}")
        print(f"   URL: {f_url[:80]}...")

        self._last_url = f_url
        self._last_channel_name = name
        self._last_category = category
        self._retry_count = 0
        self._available_qualities = ["auto", "ultra", "high", "medium", "low", "minimal"]
        self.availableQualitiesChanged.emit()
        self._qualities_analyzed = False
        self._set_status("Воспроизведение...")

        self._target_code, self._target_name = "ALL", "Глобальный"
        threading.Thread(target=self._detect_country, args=(f_url, category, name), daemon=True).start()

        try:
            root = self.engine.rootObjects()[0]
            if root:
                self.player.wid = int(root.winId())

            if 'iframe' in f_url.lower():
                print("⚠️ iframe URL detected")
                self._set_status("⚠️ iframe URL - используйте прямой m3u8")

            self._play_url(f_url)
            self._apply_stream_optimizations()
        except Exception as e:
            print(f"❌ Play error: {e}")
            self._set_status(f"❌ Ошибка воспроизведения: {type(e).__name__}")

    def _apply_stream_optimizations(self):
        """Инфраструктурные лимиты (потолок кэша — НЕ урезает качество)."""
        if not (HAS_MPV and self.player):
            return
        try:
            p = self.player
            # Единый источник правды — CacheConfig (200МБ/60с, ТЗ)
            p['cache'] = 'yes'
            p['cache-secs'] = CacheConfig.CACHE_SECS
            p['demuxer-max-bytes'] = CacheConfig.MAX_BYTES
            p['demuxer-max-back-bytes'] = CacheConfig.MAX_BACK_BYTES
            p['demuxer-readahead-secs'] = CacheConfig.READAHEAD_SECS
            p['demuxer-hysteresis-secs'] = CacheConfig.HYSTERESIS_SECS
            p['network-timeout'] = CacheConfig.NETWORK_TIMEOUT
            try: p['live-keepalive'] = 'yes'
            except Exception: pass
            p['force-seekable'] = 'yes'

            # Применяем качество ЧЕРЕЗ ЗАДЕРЖКУ — когда поток уже открыт и
            # video_params доступны. Безопасно для прямых Xtream-потоков.
            stream_optimizer.quality_level = self._current_quality
            try:
                QTimer.singleShot(3000, self._safe_apply_quality)
            except Exception:
                pass

            try:
                QTimer.singleShot(2500, self._update_available_qualities_from_tracks)
            except Exception:
                pass

            print(f"[Optimizer] ✅ Cache capped ({CacheConfig.MAX_BYTES}/{CacheConfig.CACHE_SECS}s, "
                  f"hysteresis={CacheConfig.HYSTERESIS_SECS}s); quality/subs/audio — выбор пользователя")
        except Exception as e:
            print(f"[Optimizer] Optimization error: {e}")

    def _safe_apply_quality(self):
        """Безопасное применение качества через 3с после старта.
        video_params уже доступны — vf scale сработает корректно."""
        try:
            if self.player and self._init and self._current_quality != "auto":
                self._apply_video_scaling(self._current_quality)
                print(f"📺 [Quality] Applied {self._current_quality} after stream opened")
        except Exception as e:
            print(f"⚠️ [Quality] delayed apply failed: {e}")

    def _detect_country(self, url, cat, name):
        code, cn = detect_country(cat, name)
        if code == "ALL" and not self._skip_country_detect:
            try:
                host = url.split("://")[-1].split("/")[0].split(":")[0]
                if host and not host.startswith(("127.", "192.168.", "10.")):
                    ip = socket.gethostbyname(host)
                    cc, nn = get_ip_country(ip)
                    if cc != "ALL":
                        code, cn = cc, nn
            except Exception:
                pass
        elif code == "ALL" and self._skip_country_detect:
            print("[SAVER] Пропускаем DNS+HTTP для определения страны канала")
        self._target_code = code
        self._target_name = cn
        print(f"🎯 Country: {code} ({cn})")

    @Slot(str)
    def setAspectRatio(self, ratio):
        if HAS_MPV and self.player and self._init:
            try:
                if ratio == "no":
                    self.player['keepaspect'] = False
                    self.player['video-aspect-override'] = "no"
                elif ratio == "stretch":
                    self.player['keepaspect'] = True
                    self.player['video-aspect-override'] = "no"
                else:
                    self.player['keepaspect'] = True
                    self.player['video-aspect-override'] = ratio
                print(f"📐 Aspect ratio set to: {ratio}")
            except Exception as e:
                print(f"⚠️ Aspect ratio error: {e}")

    @Slot()
    def stop(self):
        if HAS_MPV and self.player and self._init:
            try:
                self.player.stop()
                self._set_status("Ready")
                self.playingChanged.emit(False)
            except Exception:
                pass

    @Slot(str, result=str)
    def getFallback(self, channel_name):
        fallback = get_fallback_url(channel_name)
        if fallback:
            print(f"[Player] Trying fallback: {fallback[:50]}...")
            try:
                self.player.play(fallback)
                self._set_status("Использую резервный поток")
                return fallback
            except Exception:
                pass
        return None


def _append_utc(url, start_raw):
    """Превращает EPG-метку старта в параметр utc= архивной ссылки (общая логика)."""
    if not start_raw:
        return url
    try:
        from datetime import timezone
        ts = int(datetime.strptime(start_raw, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp())
        return f"{url}?utc={ts}" if "?" not in url else f"{url}&utc={ts}"
    except Exception:
        return url


if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    engine = QQmlApplicationEngine()
    core = IPTVCore(engine)
    engine.rootContext().setContextProperty("backend", core)
    engine.load(os.path.join(os.path.dirname(__file__), "main.qml"))
    sys.exit(app.exec())
