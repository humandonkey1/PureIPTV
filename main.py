import os
import sys
from ctypes import CDLL

# --- MPV preload ---
# Ищем папку MPV рядом со скриптом, скрипт можно класть куда угодно
script_dir = os.path.dirname(os.path.abspath(__file__))

# Пробуем несколько вариантов имени папки
mpv_candidates = [
    os.path.join(script_dir, "MPV"),
    os.path.join(script_dir, "mpv"),
    os.path.join(script_dir, "MPV-2"),
]
mpv_path = next((p for p in mpv_candidates if os.path.isdir(p)), mpv_candidates[0])

# Добавляем папку MPV в PATH, чтобы mpv нашёл все свои DLL
os.environ["PATH"] = mpv_path + os.pathsep + os.environ.get("PATH", "")

# Для Python 3.8+ на Windows нужно явно добавить директорию поиска DLL
if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    try:
        os.add_dll_directory(mpv_path)
    except Exception:
        pass

# Пробуем загрузить libmpv из локальной папки
mpv_dll_loaded = False
mpv_dll_names = ["libmpv-2.dll", "libmpv-1.dll", "mpv-2.dll", "mpv-1.dll"]
for dll_name in mpv_dll_names:
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

import requests
import re
import json
import gzip
import sqlite3
import socket
import locale
import threading
import urllib.parse
from datetime import datetime
from datetime import timezone
from xml.etree import ElementTree
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import time

from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtCore import QObject, Signal, Slot, Property, QThread, QAbstractListModel, qInstallMessageHandler, QTimer

def qt_message_handler(msg_type, context, message):
    if "QQuickImage" in message:
        return
    sys.stderr.write(f"{message}\n")

qInstallMessageHandler(qt_message_handler)

try:
    import mpv
    HAS_MPV = True
    print("✅ MPV imported")
except ImportError as e:
    HAS_MPV = False
    print(f"❌ MPV import: {e}")

try:
    locale.setlocale(locale.LC_NUMERIC, 'C')
except:
    pass

# Create a global session for Keep-Alive and connection reuse
http_session = requests.Session()


# ==============================================
# ОПТИМИЗИРОВАННЫЙ HLS ПРОКСИ - РАБОТАЕТ ВСЕГДА
# ==============================================

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-site',
}

class StreamOptimizer:
    """
    Оптимизатор потока для работы в любых условиях.
    Автоматически адаптируется к качеству связи.
    """
    
    def __init__(self):
        self.quality_level = "auto"  # auto, low, medium, high, ultra
        self.bandwidth = 0.0
        self.reconnect_count = 0
        self.max_reconnects = 5
        
    def detect_bandwidth(self, url, proxy_url=None):
        """Определяет пропускную способность канала"""
        try:
            proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
            start = time.time()
            r = http_session.get(url, headers=BROWSER_HEADERS, proxies=proxies, timeout=10, stream=True)
            data = b''
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    data += chunk
                    if len(data) > 512 * 1024:  # 512KB достаточно
                        break
            duration = time.time() - start
            if duration > 0:
                self.bandwidth = (len(data) / 1024) / duration  # KB/s
                print(f"[Optimizer] Bandwidth: {self.bandwidth:.1f} KB/s ({self.bandwidth * 8:.0f} Kbps)")
                return self.bandwidth
        except Exception as e:
            print(f"[Optimizer] Bandwidth test failed: {e}")
        return 0.0
    
    def get_quality_for_bandwidth(self, bandwidth):
        """Выбирает качество в зависимости от пропускной способности"""
        kbps = bandwidth * 8
        if kbps > 20000:
            return "ultra"  # 4K max
        elif kbps > 8000:
            return "high"   # 1080p
        elif kbps > 3000:
            return "medium" # 720p
        elif kbps > 1000:
            return "low"    # 480p
        else:
            return "minimal"  # 360p
    
    def should_reconnect(self):
        """Решает нужно ли переподключение"""
        return self.reconnect_count < self.max_reconnects
    
    def increase_reconnect(self):
        self.reconnect_count += 1
    
    def reset_reconnect(self):
        self.reconnect_count = 0


# Глобальный оптимизатор
stream_optimizer = StreamOptimizer()


class HLSCache:
    """Умный кэш для m3u8 манифестов с оптимизацией"""
    
    # Class-level cache dictionary so it is shared across all HLSCache instances!
    _global_cache = {}
    
    def __init__(self, proxy_url=None, quality="auto"):
        self.proxy_url = proxy_url
        self.quality = quality
        
    def _get_quality_variant(self, content, bandwidth):
        """Выбирает лучшую вариацию потока для пропускной способности и выбранного качества"""
        lines = content.split('\n')
        variants = []
        
        # Ищем #EXT-X-STREAM-INF
        for i, line in enumerate(lines):
            if '#EXT-X-STREAM-INF' in line:
                bandwidth_match = re.search(r'BANDWIDTH=(\d+)', line)
                resolution_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                
                if bandwidth_match:
                    bw = int(bandwidth_match.group(1))
                    res = resolution_match.group(1) if resolution_match else "unknown"
                    bw_kbps = bw / 1000
                    
                    # Находим следующую непустую строку, которая является URI
                    uri = ""
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip() and not lines[j].strip().startswith('#'):
                            uri = lines[j].strip()
                            break
                    
                    if uri:
                        variants.append({
                            'bw': bw,
                            'bw_kbps': bw_kbps,
                            'resolution': res,
                            'line_index': i,
                            'uri': uri
                        })
        
        if not variants:
            return content
        
        # Сортируем по битрейту по возрастанию
        variants.sort(key=lambda x: x['bw_kbps'])
        
        # Определяем целевой битрейт
        quality_level = stream_optimizer.quality_level
        if quality_level == "auto":
            # 15% запас под пропускную способность
            target_bw_kbps = bandwidth * 8 * 0.85 if bandwidth > 0 else 3000
        else:
            mapping = {
                "ultra": 50000,
                "high": 8000,
                "medium": 3000,
                "low": 1200,
                "minimal": 600
            }
            target_bw_kbps = mapping.get(quality_level, 3000)
            
        chosen = None
        for v in variants:
            if v['bw_kbps'] <= target_bw_kbps:
                chosen = v
            else:
                break
                
        if not chosen:
            # Если все варианты выше целевого, берем самый низкий
            chosen = variants[0]
            
        print(f"[Optimizer] Selected variant: {chosen['resolution']} ({chosen['bw_kbps']:.0f} Kbps) for target: {target_bw_kbps:.0f} Kbps (level: {quality_level})")
        
        # Перезаписываем m3u8 только с выбранным вариантом
        result_lines = []
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            if '#EXT-X-STREAM-INF' in line:
                is_chosen = False
                for v in variants:
                    if v['line_index'] == i and v == chosen:
                        is_chosen = True
                        break
                
                if is_chosen:
                    result_lines.append(line)
                else:
                    # Пропускаем этот stream-inf и его URI
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
        """Загружает m3u8 с оптимизацией под пропускную способность"""
        # Определяем, является ли плейлист мастер-плейлистом (содержит варианты качества)
        # Мастер-плейлисты кэшируем на 60 секунд, а медиа-плейлисты (сегменты live) на 3.5 секунды
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
                except:
                    pass
                
                content = content.decode('utf-8', errors='ignore')
                
                is_master = '#EXT-X-STREAM-INF' in content
                
                # Оптимизируем под пропускную способность
                if stream_optimizer.bandwidth > 0 or stream_optimizer.quality_level != "auto":
                    content = self._get_quality_variant(content, stream_optimizer.bandwidth)
                
                # Сохраняем в глобальный кэш
                HLSCache._global_cache[url] = (content, time.time(), is_master)
                
                return content, headers
        except Exception as e:
            print(f"❌ Cache fetch error: {e}")
        
        return None, headers


class HLSProxyHandler(BaseHTTPRequestHandler):
    """Оптимизированный HLS Proxy с буферизацией и переподключением"""
    
    cache = None
    proxy_url = None
    port = 8899
    use_optimizer = True
    core_ref = None
    last_base_url = None
    
    def log_message(self, format, *args):
        pass
    
    def do_GET(self):
        try:
            if self.path.startswith('/hls/'):
                encoded_url = self.path[5:]
                original_url = urllib.parse.unquote(encoded_url)
            elif self.path.startswith('/stream'):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                original_url = params.get('url', [''])[0]
            else:
                self.send_error(400)
                return
            
            if not original_url:
                self.send_error(400)
                return
            
            # Декодируем HTML сущности &amp; в URL (в цикле для глубокой очистки при double-encoding)
            while '&amp;' in original_url:
                original_url = original_url.replace('&amp;', '&')
            
            # Восстанавливаем относительный URL, если он пришел без http
            if not original_url.startswith('http'):
                if hasattr(HLSProxyHandler, 'last_base_url') and HLSProxyHandler.last_base_url:
                    if original_url.startswith('/'):
                        from urllib.parse import urlparse as up
                        p = up(HLSProxyHandler.last_base_url)
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
            except:
                pass
    
    def _handle_m3u8(self, url):
        """Обрабатывает m3u8 с оптимизацией"""
        # Сразу декодируем &amp; в URL, если они туда просочились
        while '&amp;' in url:
            url = url.replace('&amp;', '&')
            
        print(f"📡 [HLS] Fetching: {url[:60]}...")
        
        # Тестируем пропускную способность
        if stream_optimizer.bandwidth == 0:
            stream_optimizer.detect_bandwidth(url, self.proxy_url)
        
        cache = HLSCache(self.proxy_url, stream_optimizer.quality_level)
        content, headers = cache.fetch_with_headers(url)
        
        if not content:
            print(f"❌ [HLS] Failed to fetch manifest")
            self.send_error(502)
            return
        
        print(f"✅ [HLS] Manifest loaded, quality optimized")
        
        # Базовая URL для относительных ссылок
        base_url = url.rsplit('/', 1)[0] + '/'
        
        # Сохраняем базовый URL для возможного восстановления относительных путей в do_GET
        HLSProxyHandler.last_base_url = base_url
        
        # Определяем, нужно ли проксировать вложенные ресурсы этого домена.
        # Например, jmp2.uk блокирует длинные проксированные запросы по 414 из-за гигантских токенов,
        # поэтому их вложенные плейлисты и сегменты мы заставляем MPV запрашивать напрямую (через абсолютные ссылки)!
        url_lower = url.lower()
        proxy_sub_resources = any(domain in url_lower for domain in [
            'televizor-24', 'streaming.', 'online-television'
        ])
        
        lines = []
        for line in content.split('\n'):
            line = line.rstrip()
            
            # Декодируем &amp; в строке манифеста
            while '&amp;' in line:
                line = line.replace('&amp;', '&')
            
            if not line:
                lines.append(line)
                continue
                
            if line.startswith('#'):
                # Если строка содержит URI (например, в тегах #EXT-X-MEDIA или #EXT-X-I-FRAME-STREAM-INF)
                if 'URI=' in line:
                    match = re.search(r'URI="([^"]*)"', line)
                    if match:
                        uri_val = match.group(1)
                        while '&amp;' in uri_val:
                            uri_val = uri_val.replace('&amp;', '&')
                            
                        if not uri_val.startswith('http'):
                            if uri_val.startswith('/'):
                                from urllib.parse import urlparse as up
                                p = up(url)
                                absolute_uri = f"{p.scheme}://{p.netloc}{uri_val}"
                            else:
                                absolute_uri = base_url + uri_val
                        else:
                            absolute_uri = uri_val
                        
                        # Если требуется проксирование вложенных ресурсов
                        if proxy_sub_resources:
                            proxied_uri = f"http://127.0.0.1:{self.port}/hls/{urllib.parse.quote(absolute_uri, safe='')}"
                        else:
                            # Иначе даем прямой абсолютный URL
                            proxied_uri = absolute_uri
                            
                        line = line.replace(f'URI="{match.group(1)}"', f'URI="{proxied_uri}"')
                lines.append(line)
            else:
                # Обычная строка ссылки (сегмент или суб-манифест)
                if line.startswith('http'):
                    absolute = line
                elif line.startswith('/'):
                    from urllib.parse import urlparse as up
                    p = up(url)
                    absolute = f"{p.scheme}://{p.netloc}{line}"
                else:
                    absolute = base_url + line
                
                # Если требуется проксирование
                if proxy_sub_resources:
                    new_url = f"http://127.0.0.1:{self.port}/hls/{urllib.parse.quote(absolute, safe='')}"
                else:
                    new_url = absolute
                    
                lines.append(new_url)
        
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
        """Проксирует сегменты с буферизацией"""
        max_retries = 3
        retry_delay = 1
        
        for attempt in range(max_retries):
            try:
                segment_headers = BROWSER_HEADERS.copy()
                parsed = urllib.parse.urlparse(url)
                segment_headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
                segment_headers['Origin'] = f"{parsed.scheme}://{parsed.netloc}"
                segment_headers['Accept-Encoding'] = 'identity'
                
                proxies = {'http': self.proxy_url, 'https': self.proxy_url} if self.proxy_url else None
                response = http_session.get(url, headers=segment_headers, timeout=30, stream=True, proxies=proxies)
                
                if response.status_code == 200:
                    self.send_response(200)
                    for key, value in response.headers.items():
                        if key.lower() not in ['transfer-encoding', 'content-encoding', 'connection']:
                            self.send_header(key, value)
                    self.send_header('Connection', 'keep-alive')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    
                    # Отправляем с буферизацией и замеряем скорость
                    start_time = time.time()
                    bytes_downloaded = 0
                    buffer_size = 65536
                    for chunk in response.iter_content(chunk_size=buffer_size):
                        if chunk:
                            self.wfile.write(chunk)
                            bytes_downloaded += len(chunk)
                            
                    duration = time.time() - start_time
                    if duration > 0.05 and bytes_downloaded > 1024:
                        speed_kb = (bytes_downloaded / 1024) / duration
                        # Экспоненциальное сглаживание скорости
                        if stream_optimizer.bandwidth == 0:
                            stream_optimizer.bandwidth = speed_kb
                        else:
                            stream_optimizer.bandwidth = stream_optimizer.bandwidth * 0.7 + speed_kb * 0.3
                            
                        # Передаем скорость в IPTVCore для оценки сигнала
                        if HLSProxyHandler.core_ref:
                            try:
                                HLSProxyHandler.core_ref.segmentDownloaded.emit(speed_kb)
                            except:
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
        except:
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
            
        except Exception as e:
            try:
                self.send_error(500)
            except:
                pass
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()


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
        t = threading.Thread(target=_hls_server.serve_forever, daemon=True)
        t.start()
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


# ✅ Проверенные рабочие URL без гео-блокировки!
VERIFIED_WORKING_STREAMS = {
    "360": "https://cdn-evacoder-tv.facecast.io/evacoder_hls_hi/CkxfR1xNUAJwTgtXTBZTAJli/index.m3u8",
    "mma": "https://streams2.sofast.tv/vglive-sk-462904/playlist.m3u8",
    "pro100": "https://sirius.greenhosting.ru/Pro100tvRu/video.m3u8",
    "drive": "https://stream8.cinerama.uz/1421/tracks-v1a1/mono.m3u8",
}

def get_fallback_url(channel_name):
    """Возвращает резервный URL для канала"""
    for url in VERIFIED_WORKING_STREAMS.values():
        if url:
            return url
    return None

def detect_country(cat, name):
    text = (str(cat) + " " + str(name)).lower()
    country_map = {
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
    for kw, (code, cn) in country_map.items():
        if kw in text:
            return code, cn
    return "ALL", "Глобальный"

def get_ip_country(ip):
    try:
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=5).json()
        code = r.get("country_code", "ALL")
        name = r.get("country_name", "Глобальный")
        # Переводы на русский
        ru_names = {
            "RU": "Россия", "US": "США", "GB": "Великобритания", "DE": "Германия",
            "FR": "Франция", "IT": "Италия", "ES": "Испания", "TR": "Турция",
            "UA": "Украина", "BY": "Беларусь", "KZ": "Казахстан", "NL": "Нидерланды"
        }
        return code, ru_names.get(code, name)
    except:
        return "ALL", "Глобальный"


# ==============================================
# IPTV WORKER & PARSER
# ==============================================

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

            # Нормализуем имя протокола: QML и БД хранят короткие значения
            # "M3U" / "XTREAM" / "STALKER", а старые ветки могли писать
            # "M3U (URL)" / "M3U (Файл)" / "Xtream" / "Stalker".
            # Поддерживаем обе формы, чтобы и старые записи не сломались.
            proto_l = (self.proto or "").lower().strip()

            if proto_l == "m3u" or proto_l.startswith("m3u"):
                # В QML M3U объединён в одну вкладку — отличаем URL от файла по схеме
                host_l = (self.host or "").lower().strip()
                if host_l.startswith("http://") or host_l.startswith("https://"):
                    ch = self._parse_m3u(self.host, headers)
                else:
                    ch = self._parse_m3u_file(self.host)
            elif proto_l == "xtream" or proto_l.startswith("xtream"):
                ch = self._parse_xtream(headers)
            elif proto_l == "stalker" or proto_l.startswith("stalker"):
                ch = self._parse_stalker(headers)
            else:
                raise ValueError(f"Неизвестный протокол: '{self.proto}'")

            if self.epg and self.epg.strip():
                epg_db = self._load_epg(self.epg, headers)

            self.finished.emit(ch, epg_db, "Успешно загружено")
        except Exception as e:
            # Прокидываем тип исключения вместе с текстом — так в диалоге
            # сразу понятно, сетевая это ошибка, парсинг или БД.
            self.error.emit(f"{type(e).__name__}: {e}")

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
                current = {}
                name_match = re.search(r',([^,]*)$', line)
                name = name_match.group(1).strip() if name_match else "Канал без названия"
                attrs = {}
                for m in re.finditer(r'([a-zA-Z0-9_-]+)="([^"]*)"', line):
                    attrs[m.group(1).lower()] = m.group(2)
                current = {
                    "id": attrs.get("tvg-id") or attrs.get("tvg-name") or name,
                    "logo": attrs.get("tvg-logo") or "",
                    "group": attrs.get("group-title") or "Общие",
                    "name": name,
                    "url": ""
                }
            elif line.startswith('http') and current:
                current["url"] = line
                ch.append(current)
                current = {}
        return ch

    def _parse_xtream(self, h):
        base = self.host.rstrip('/')
        cats = {}
        try:
            cat_res = requests.get(f"{base}/player_api.php?username={self.user}&password={self.pwd}&action=get_live_categories", headers=h, timeout=15).json()
            for c in cat_res:
                cats[str(c.get('category_id'))] = c.get('category_name')
        except Exception as e:
            print(f"⚠️ Xtream cats: {e}")

        res = requests.get(f"{base}/player_api.php?username={self.user}&password={self.pwd}&action=get_live_streams", headers=h, timeout=25).json()
        ch = []
        for i in res:
            cid = str(i.get('category_id', ''))
            ch.append({
                "id": str(i.get('epg_channel_id', i.get('name'))),
                "name": i.get('name'),
                "logo": i.get('stream_icon', ''),
                "group": cats.get(cid) or "Общие",
                "url": f"{base}/live/{self.user}/{self.pwd}/{i.get('stream_id')}.ts"
            })
        return ch

    def _parse_stalker(self, h):
        base = self.host.rstrip('/')
        h["X-User-MAC"], h["Cookie"] = self.mac, f"mac={self.mac}"
        hs = requests.get(f"{base}/server/load.php?type=stb&action=handshake", headers=h, timeout=15).json()
        tk = hs['js']['token']
        res = requests.get(f"{base}/server/load.php?type=itv&action=get_all_channels&token={tk}", headers=h, timeout=20).json()
        return [{"id": str(c.get('tvg_id', c.get('name'))), "name": c['name'], "logo": "", "group": "Stalker", "url": c['url'].split(' ')[-1]} for c in res.get('js', [])]

    def _load_epg(self, url, h):
        epg_db = {}
        try:
            r = requests.get(url, headers=h, timeout=25)
            content = r.content
            if url.endswith('.gz') or content[:2] == b'\x1f\x8b':
                content = gzip.decompress(content)
            
            root = ElementTree.fromstring(content)
            
            # Парсим каналы
            chan_map = {}
            for chan in root.findall('channel'):
                cid = chan.get('id')
                dn = chan.find('display-name')
                if cid and dn is not None:
                    chan_map[cid] = dn.text

            # Парсим программы
            for prog in root.findall('programme'):
                channel_id = prog.get('channel')
                title_node = prog.find('title')
                desc_node = prog.find('desc')
                
                if channel_id and title_node is not None:
                    start_time = prog.get('start', '').split(' ')[0]
                    # Конвертируем время в красивый вид HH:MM
                    try:
                        t = start_time[8:10] + ":" + start_time[10:12]
                    except:
                        t = "00:00"
                        
                    title = title_node.text
                    desc = desc_node.text if desc_node is not None else ""
                    
                    if channel_id not in epg_db:
                        epg_db[channel_id] = []
                        
                    epg_db[channel_id].append({
                        "title": title,
                        "time": t,
                        "desc": desc,
                        "start": start_time
                    })
            
            # Сортируем программы по времени
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
        if role == 201:
            return v.get("title")
        if role == 202:
            return v.get("time")
        if role == 203:
            return v.get("desc")
        if role == 204:
            return v.get("start")
        return None
        
    def roleNames(self):
        return {201: b"displayTitle", 202: b"displayTime", 203: b"desc", 204: b"startRaw"}
        
    def set_data(self, d):
        self.beginResetModel()
        self._i = d
        self.endResetModel()


# ==============================================
# IPTV CORE - ОПТИМИЗИРОВАННЫЙ ДЛЯ ЛЮБЫХ УСЛОВИЙ
# ==============================================

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
        self._connection_quality = "unknown"  # unknown, poor, fair, good, excellent
        self._current_quality = "auto"
        self._available_qualities = ["auto", "ultra", "high", "medium", "low", "minimal"]
        self._qualities_analyzed = False
        
        # Подключаем сигнал загрузки сегментов для thread-safe обновления уровня сигнала
        self.segmentDownloaded.connect(self.update_connection_quality_from_speed)
        
        if HAS_MPV:
            try:
                self.player = mpv.MPV(
                    vo='gpu',
                    hwdec='no',
                    ytdl=False,
                    osc=False,
                    input_default_bindings=False,
                    input_vo_keyboard=True,
                    # БАЗОВЫЕ ОПТИМИЗАЦИИ ДЛЯ БУНКЕРА
                    keep_open='yes',
                    keep_open_pause='no',
                    hr_seek='yes',
                    network_timeout='30',
                    # ПРОФИЛЬ УМНОЙ БУФЕРИЗАЦИИ (до 60 секунд)
                    cache='yes',
                    demuxer_max_bytes='200MiB',
                    demuxer_max_back_bytes='100MiB',
                    demuxer_readahead_secs='60',
                    # УСТОЙЧИВОСТЬ К СЕТЕВЫМ ПРОБЛЕМАМ (переподключение на уровне демультиплексора)
                    demuxer_lavf_o='reconnect=1,reconnect_streamed=1,reconnect_delay_max=5,fflags=+nobuffer+fastseek',
                    # ДЛЯ LIVE СТРИМОВ
                    force_seekable='yes',
                )
                
                print("✅ MPV initialized (buffering: 60s, reconnect: yes)")
                
                self.player['user-agent'] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                
                # Оптимизация под низкую пропускную способность
                self.player['demuxer-max-bytes'] = '100MiB'  # Макс размер буфера
                self.player['network-timeout'] = '30'  # Таймаут сети 30 сек
                
                # События
                @self.player.property_observer('time-pos')
                def on_time(_n, v):
                    self.positionChanged.emit()
                    if v is not None and v > 0:
                        self.playingChanged.emit(True)
                        self._retry_count = 0  # Сброс счетчика при воспроизведении
                        if not getattr(self, '_qualities_analyzed', False):
                            self._qualities_analyzed = True
                            QTimer.singleShot(0, self._update_available_qualities_from_tracks)
                
                @self.player.property_observer('duration')
                def on_dur(_n, v):
                    self.durationChanged.emit()
                
                @self.player.property_observer('pause')
                def on_pause(_n, v):
                    self.playbackStateChanged.emit()
                    self.playingChanged.emit(not (v if v is not None else True))
                    
                @self.player.property_observer('volume')
                def on_vol(_n, v):
                    self.volumeChanged.emit()
                
                @self.player.property_observer('avsync')
                def on_avsync(_n, v):
                    # Следим за синхронизацией A/V
                    if v is not None and abs(v) > 1.0:
                        print(f"⚠️ AV desync detected: {v}")

                # Наблюдение за буферизацией (для индикатора в бункере)
                try:
                    @self.player.property_observer('paused-for-cache')
                    def on_paused_for_cache(_n, v):
                        self._is_buffering = bool(v) if v is not None else False
                        self.bufferingChanged.emit()
                        if self._is_buffering:
                            self._s = "Буферизация..."
                            self.statusChanged.emit()
                            self._connection_quality = "poor"
                            self.connectionQualityChanged.emit("poor")
                        else:
                            if self._retry_count > 0:
                                self._retry_count = 0
                            self._s = "Воспроизведение..."
                            self.statusChanged.emit()
                except Exception as e:
                    print(f"⚠️ Failed to observe paused-for-cache: {e}")

                try:
                    @self.player.property_observer('cache-buffering-state')
                    def on_cache_buffering_state(_n, v):
                        self._buffering_progress = int(v) if v is not None else 100
                        self.bufferingProgressChanged.emit()
                except Exception as e:
                    print(f"⚠️ Failed to observe cache-buffering-state: {e}")

                # Наблюдение за параметрами видео для динамического построения меню качеств
                try:
                    @self.player.property_observer('video-params')
                    def on_video_params(_n, v):
                        if v:
                            QTimer.singleShot(0, self._update_available_qualities_from_tracks)
                except Exception as e:
                    print(f"⚠️ Failed to observe video-params: {e}")

                @self.player.event_callback('end-file')
                def on_end(event):
                    try:
                        if event.data:
                            reason = event.data.reason
                            err = event.data.error
                            print(f"🎬 end-file: reason={reason}, error={err}")
                            
                            if reason == 0:  # EOF
                                self._handle_playback_end()
                            elif reason == 4:  # Error/Abort
                                self._handle_playback_error(err)
                    except Exception as e:
                        print(f"⚠️ end-file handler: {e}")
                
                @self.player.event_callback('log-message')
                def on_log(event):
                    if event.text:
                        text = event.text.lower()
                        if 'buffering' in text or 'underrun' in text:
                            self._connection_quality = "poor"
                            self.connectionQualityChanged.emit("poor")
                            print("📶 Connection: POOR (buffering)")
                        elif 'cache' in text and 'full' in text:
                            self._connection_quality = "good"
                            self.connectionQualityChanged.emit("good")

                @self.player.event_callback('seek')
                def on_seek(event):
                    print("[Player] Seeking...")

                self._init = True
                print("✅ MPV initialized (optimized for poor connection)")
            except Exception as e:
                print(f"❌ MPV init: {e}")
        
        self._init_db()

    def _init_db(self):
        self.db = sqlite3.connect("premium.db", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY, name TEXT, proto TEXT, host TEXT, epg TEXT,
            user TEXT, pwd TEXT, mac TEXT, channels TEXT, epg_db TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS favorites (
            playlist_id INTEGER, channel_id TEXT, PRIMARY KEY (playlist_id, channel_id))""")
        self.db.commit()

    def _schedule_reconnect(self):
        if self._retry_count < 5:
            self._retry_count += 1
            delay = 2 ** self._retry_count  # Экспоненциальная задержка: 2, 4, 8, 16, 32 сек
            print(f"[Player] Reconnecting in {delay}s... (attempt {self._retry_count}/5)")
            self._s = f"Переподключение (попытка {self._retry_count}/5 через {delay}с)..."
            self.statusChanged.emit()
            
            self._connection_quality = "poor"
            self.connectionQualityChanged.emit("poor")
            
            QTimer.singleShot(delay * 1000, self._do_reconnect)
        else:
            print("❌ Reconnect failed: max attempts reached.")
            self._s = "Ошибка: потеряно соединение"
            self.statusChanged.emit()
            self._retry_count = 0

    def _do_reconnect(self):
        if not self._last_url:
            return
        
        print(f"[Player] Reconnecting attempt {self._retry_count}/5...")
        try:
            url_lower = self._last_url.lower()
            needs_proxy = any(domain in url_lower for domain in [
                'televizor-24', 'streaming.', 'online-television'
            ])
            
            if needs_proxy:
                start_hls_proxy(None, core=self)
                proxied = get_proxied_url(self._last_url)
                self.player.play(proxied)
            else:
                self.player.play(self._last_url)
                
            self._apply_stream_optimizations()
        except Exception as e:
            print(f"[Player] Reconnect error: {e}")
            self._schedule_reconnect()

    def _handle_playback_end(self):
        """Обработка завершения воспроизведения (или разрыва в live)"""
        try:
            # Для LIVE потоков EOF означает потерю соединения, запускаем автопереподключение
            is_live = (self.duration == 0.0)
            if is_live and self._last_url:
                print("📺 Live stream EOF, treating as disconnect...")
                self._schedule_reconnect()
            else:
                self._retry_count = 0
                self._s = "Конец воспроизведения"
                self.statusChanged.emit()
        except Exception as e:
            print(f"⚠️ handle_playback_end error: {e}")

    def _handle_playback_error(self, err):
        """Обработка ошибки воспроизведения"""
        print(f"⚠️ Playback error: {err}")
        self._schedule_reconnect()

    def _test_connection_quality(self, url):
        """Тестирует качество соединения"""
        try:
            start = time.time()
            r = http_session.head(url, timeout=10, stream=True)
            duration = time.time() - start
            
            if r.status_code in [200, 301, 302]:
                if duration < 0.5:
                    self._connection_quality = "excellent"
                elif duration < 1.5:
                    self._connection_quality = "good"
                elif duration < 3:
                    self._connection_quality = "fair"
                else:
                    self._connection_quality = "poor"
                
                self.connectionQualityChanged.emit(self._connection_quality)
                print(f"📶 Connection quality: {self._connection_quality.upper()}")
                return self._connection_quality
        except:
            self._connection_quality = "poor"
            self.connectionQualityChanged.emit("poor")
        
        return "poor"

    @Slot(float)
    def update_connection_quality_from_speed(self, speed_kb):
        if self._is_buffering:
            quality = "poor"
        elif speed_kb > 2500:
            quality = "excellent"
        elif speed_kb > 1000:
            quality = "good"
        elif speed_kb > 300:
            quality = "fair"
        else:
            quality = "poor"
            
        if self._connection_quality != quality:
            self._connection_quality = quality
            self.connectionQualityChanged.emit(quality)
            print(f"📶 [Connection] Speed updated: {speed_kb:.1f} KB/s -> {quality}")

    @Property(str, notify=statusChanged)
    def status(self): return self._s

    @Property(list, notify=playlistsChanged)
    def playlists(self):
        cursor = self.db.execute("SELECT id, name, proto, host, epg, user, pwd, mac FROM playlists ORDER BY id DESC")
        return [{"id": r[0], "name": r[1], "proto": r[2], "host": r[3], "epg": r[4], "user": r[5], "pwd": r[6], "mac": r[7]} for r in cursor.fetchall()]

    @Property(str, notify=channelsChanged)
    def current_playlist_name(self): return self._current_playlist_name

    @Property(list, notify=channelsChanged)
    def categories(self):
        cats = set()
        for c in self._ch:
            cats.add(c.get("group", "Общие"))
        return ["Все каналы", "★ Избранные"] + sorted(list(cats))
    
    @Property(str, notify=connectionQualityChanged)
    def connectionQuality(self): return self._connection_quality
    
    @Property(str, notify=qualityChanged)
    def currentQuality(self): return self._current_quality

    @Property(list, notify=availableQualitiesChanged)
    def availableQualities(self):
        return self._available_qualities

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

    @Property(bool, notify=bufferingChanged)
    def isBuffering(self):
        return self._is_buffering
        
    @Property(int, notify=bufferingProgressChanged)
    def bufferingProgress(self):
        return getattr(self, '_buffering_progress', 100)

    @Property(float, notify=positionChanged)
    def position(self):
        if HAS_MPV and self.player and self._init and self.player.time_pos:
            return float(self.player.time_pos)
        return 0.0

    @position.setter
    def position(self, val):
        if HAS_MPV and self.player and self._init:
            try:
                self.player.time_pos = float(val)
                if self.player.pause:
                    self.player.pause = False
                self.positionChanged.emit()
            except:
                pass

    @Property(float, notify=durationChanged)
    def duration(self):
        if HAS_MPV and self.player and self._init and self.player.duration:
            return float(self.player.duration)
        return 0.0

    @Property(int, notify=volumeChanged)
    def volume(self):
        if HAS_MPV and self.player and self._init and self.player.volume:
            return int(self.player.volume)
        return 80

    @volume.setter
    def volume(self, val):
        if HAS_MPV and self.player and self._init:
            try:
                self.player.volume = int(val)
                self.volumeChanged.emit()
            except:
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
                if self.player.time_pos and self.player.duration and self.player.time_pos >= self.player.duration - 1:
                    self.player.time_pos = 0.0
            except:
                pass
            self.player.pause = not self.player.pause
            self.playbackStateChanged.emit()

    @Slot(int)
    def loadPlaylist(self, pid):
        r = self.db.execute("SELECT name, proto, channels, epg_db FROM playlists WHERE id=?", (pid,)).fetchone()
        if r:
            self.current_playlist_id = pid
            self._current_playlist_name = r[0]
            try:
                self._ch = json.loads(r[2])
                self._ed = json.loads(r[3])
            except:
                self._ch, self._ed = [], []
            favs = self.db.execute("SELECT channel_id FROM favorites WHERE playlist_id=?", (pid,)).fetchall()
            self._fav_ids = set(f[0] for f in favs)
            self.channelsChanged.emit()

    @Slot(str, str, result=list)
    def getFilteredChannels(self, cat, query):
        q = query.lower().strip()
        result = []
        for c in self._ch:
            if q and q not in c.get("name", "").lower():
                continue
            if cat == "Все каналы":
                result.append(c)
            elif cat == "★ Избранные":
                if c.get("id") in self._fav_ids:
                    result.append(c)
            else:
                if c.get("group", "Общие") == cat:
                    result.append(c)
        return result

    @Slot(str, result=bool)
    def toggleFavorite(self, cid):
        if not self.current_playlist_id:
            return False
        if cid in self._fav_ids:
            self._fav_ids.remove(cid)
            self.db.execute("DELETE FROM favorites WHERE playlist_id=? AND channel_id=?", (self.current_playlist_id, cid))
        else:
            self._fav_ids.add(cid)
            self.db.execute("INSERT OR REPLACE INTO favorites (playlist_id, channel_id) VALUES (?, ?)", (self.current_playlist_id, cid))
        self.db.commit()
        self.channelsChanged.emit()
        return cid in self._fav_ids

    @Slot(str, result=bool)
    def isFavorite(self, cid):
        return cid in self._fav_ids

    @Slot(str, str, str, str, str, str, str)
    def addPlaylist(self, name, proto, host, epg, user, pwd, mac):
        if not name.strip() or not host.strip():
            self._s = "Ошибка: Имя и URL обязательны!"
            self.statusChanged.emit()
            return
        self._s = "Подключение..."
        self.statusChanged.emit()
        self.w = IPTVWorker(proto, host, epg, user, pwd, mac)
        self.w.finished.connect(lambda ch, epg_db, msg: self._on_loaded(name, proto, host, epg, user, pwd, mac, ch, epg_db))
        self.w.error.connect(self._on_error)
        self.w.start()

    def _on_loaded(self, name, proto, host, epg, user, pwd, mac, ch, epg_db):
        if not ch:
            self._s = "Ошибка: пустой плейлист!"
            self.statusChanged.emit()
            self.loadFailed.emit("Плейлист пуст")
            return
        try:
            self.db.execute("INSERT INTO playlists (name, proto, host, epg, user, pwd, mac, channels, epg_db) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, proto, host, epg, user, pwd, mac, json.dumps(ch), json.dumps(epg_db)))
            self.db.commit()
            self._s = "Добавлено!"
            self.statusChanged.emit()
            self.playlistsChanged.emit()
            self.loadFinished.emit()
        except Exception as e:
            self._s = f"Ошибка БД: {e}"
            self.statusChanged.emit()
            self.loadFailed.emit(str(e))

    def _on_error(self, msg):
        self._s = f"Ошибка: {msg}"
        self.statusChanged.emit()
        self.loadFailed.emit(msg)

    @Slot()
    def cancelConnection(self):
        if self.w:
            try:
                self.w.finished.disconnect()
                self.w.error.disconnect()
            except:
                pass
            self._s = "Отменено"
            self.statusChanged.emit()

    @Slot('QVariant')
    def deletePlaylist(self, pid):
        if not pid:
            return
        try:
            if hasattr(pid, 'toVariant'):
                pid = pid.toVariant()
            self.db.execute("DELETE FROM playlists WHERE id=?", (int(pid),))
            self.db.execute("DELETE FROM favorites WHERE playlist_id=?", (int(pid),))
            self.db.commit()
            self.playlistsChanged.emit()
        except Exception as e:
            print(f"❌ Delete: {e}")

    @Slot(str)
    def updateEPG(self, cid):
        self._em.set_data(self._ed.get(cid, []))

    @Slot(str, result=str)
    def getCurrentEPG(self, cid):
        now = datetime.now().strftime("%Y%m%d%H%M%S")
        epg_list = self._ed.get(cid, [])
        if not epg_list:
            return "Нет программы"
        for p in epg_list:
            start = p.get("start", "")
            if start and start <= now:
                continue
        return epg_list[0].get("title", "")

    @Slot(str, str, result=str)
    def getArchiveUrl(self, url, start_raw):
        f_url = url
        if start_raw:
            try:
                ts = int(datetime.strptime(start_raw, "%Y%m%d%H%M%S").replace(timezone.utc).timestamp())
                f_url = f"{url}?utc={ts}" if "?" not in url else f"{url}&utc={ts}"
            except:
                pass
        return f_url

    @Slot(str, result=str)
    def setQuality(self, quality):
        """
        Устанавливает качество видео.
        quality: "auto", "ultra", "high", "medium", "low", "minimal"
        """
        if self._current_quality == quality:
            return quality
            
        self._current_quality = quality
        stream_optimizer.quality_level = quality
        self.qualityChanged.emit(quality)
        print(f"📺 Quality set to: {quality}")
        
        if self.player and self._init:
            try:
                # 1. Настройка параметров буферизации MPV под выбранный профиль
                if quality == "ultra":
                    self.player['demuxer-readahead-secs'] = '10'
                    self.player['demuxer-max-bytes'] = '300MiB'
                    try: self.player['hls-bitrate'] = 'max'
                    except: pass
                elif quality == "high":
                    self.player['demuxer-readahead-secs'] = '15'
                    self.player['demuxer-max-bytes'] = '200MiB'
                    try: self.player['hls-bitrate'] = '8000000'
                    except: pass
                elif quality == "medium":
                    self.player['demuxer-readahead-secs'] = '30'
                    self.player['demuxer-max-bytes'] = '150MiB'
                    try: self.player['hls-bitrate'] = '3000000'
                    except: pass
                elif quality == "low":
                    self.player['demuxer-readahead-secs'] = '60'
                    self.player['demuxer-max-bytes'] = '100MiB'
                    try: self.player['hls-bitrate'] = '1200000'
                    except: pass
                elif quality == "minimal":
                    self.player['demuxer-readahead-secs'] = '60'
                    self.player['demuxer-max-bytes'] = '50MiB'
                    try: self.player['hls-bitrate'] = 'min'
                    except: pass
                elif quality == "auto":
                    self.player['demuxer-readahead-secs'] = '60'
                    self.player['demuxer-max-bytes'] = '150MiB'
                    try: self.player['hls-bitrate'] = 'no'
                    except: pass
                
                self.player['cache'] = 'yes'
                
                # 2. Насильно масштабируем видео в реальном времени!
                # Мы используем сверхбыстрые флаги масштабирования sws-flags='fast_bilinear' (билинейная интерполяция),
                # чтобы программное масштабирование (даже до 4К) шло абсолютно плавно, мягко и без зависаний!
                try:
                    self.player['sws-flags'] = 'fast_bilinear'
                except:
                    pass
                
                height_map = {
                    "ultra": 2160,
                    "high": 1080,
                    "medium": 720,
                    "low": 480,
                    "minimal": 360
                }
                
                # Читаем исходную высоту видео из свойств MPV
                original_height = None
                try:
                    v_params = getattr(self.player, 'video_params', None)
                    if v_params and isinstance(v_params, dict):
                        original_height = v_params.get('h')
                    if not original_height:
                        vo_params = getattr(self.player, 'video_out_params', None)
                        if vo_params and isinstance(vo_params, dict):
                            original_height = vo_params.get('h')
                except:
                    pass
                
                if quality == "auto" or not original_height:
                    self.player['hwdec'] = 'auto-safe'
                    try: self.player.command('vf', 'set', '')
                    except: self.player['vf'] = ''
                    print("🎬 [Quality] Using auto-safe hardware decoder and native quality.")
                else:
                    target_height = height_map.get(quality, 720)
                    
                    if target_height < original_height:
                        # Включаем программное декодирование для гарантированного применения фильтра сжатия на CPU!
                        self.player['hwdec'] = 'no'
                        try:
                            self.player.command('vf', 'set', f'scale=-2:{target_height}')
                        except:
                            self.player['vf'] = f'scale=-2:{target_height}'
                        print(f"🎬 [Quality] Downscaling stream on CPU from {original_height}p to {target_height}p.")
                    else:
                        # Если выбранное качество выше или равно исходному, играем нативно на GPU без лишней нагрузки!
                        self.player['hwdec'] = 'auto-safe'
                        try: self.player.command('vf', 'set', '')
                        except: self.player['vf'] = ''
                        print(f"🎬 [Quality] Stream original height is {original_height}p. Playing natively on GPU.")
                
                # 3. Переключаем видеодорожку (vid) в реальном времени, если в потоке есть альтернативные варианты
                self._apply_runtime_quality_track(quality)
                
                # 4. Перезапускаем поток для гарантированного применения hwdec и vf опций!
                if self._last_url:
                    pos = self.player.time_pos
                    is_live = (self.duration == 0.0)
                    
                    print(f"🔄 [Quality] Reloading stream to apply HLS quality '{quality}'...")
                    
                    url_lower = self._last_url.lower()
                    needs_proxy = any(domain in url_lower for domain in [
                        'televizor-24', 'streaming.', 'online-television'
                    ])
                    
                    if needs_proxy:
                        proxied = get_proxied_url(self._last_url)
                        self.player.play(proxied)
                    else:
                        self.player.play(self._last_url)
                        
                    # Восстанавливаем позицию для архивов/файлов
                    if not is_live and pos is not None and pos > 0:
                        def restore_position():
                            time.sleep(1.2)
                            try:
                                self.player.time_pos = pos
                                print(f"🕒 [Quality] Position restored to {pos:.1f}s")
                            except:
                                pass
                        threading.Thread(target=restore_position, daemon=True).start()
                
            except Exception as e:
                print(f"⚠️ Quality setting error: {e}")
        
        # Если качество изменено во время воспроизведения, принудительно очищаем и обновляем HLS кэш
        HLSCache._global_cache.clear()
        
        return quality

    @Slot(str, result=bool)
    def isQualityAvailable(self, quality):
        return quality in self._available_qualities

    def _apply_runtime_quality_track(self, quality):
        if not self.player or not self._init:
            return
            
        try:
            tracks = getattr(self.player, 'track_list', None)
            if not tracks:
                return
                
            video_tracks = [t for t in tracks if t.get('type') == 'video']
            if not video_tracks or len(video_tracks) <= 1:
                return
                
            # Маппинг качества на высоту кадра
            height_map = {
                "ultra": 2160,
                "high": 1080,
                "medium": 720,
                "low": 480,
                "minimal": 360
            }
            
            if quality == "auto":
                self.player.vid = 'auto'
                print("🎯 [Real-time Quality] Set vid to 'auto'")
                return
                
            target_height = height_map.get(quality, 720)
            
            # Ищем дорожку с минимальным отклонением по высоте
            best_track = None
            min_diff = 999999
            
            for t in video_tracks:
                h = t.get('demux-h') or t.get('height')
                if h is not None:
                    diff = abs(int(h) - target_height)
                    if diff < min_diff:
                        min_diff = diff
                        best_track = t
            
            if best_track:
                track_id = best_track.get('id')
                if track_id is not None:
                    self.player.vid = int(track_id)
                    print(f"🎯 [Real-time Quality] Switched video track to vid={track_id} (height={best_track.get('demux-h') or best_track.get('height')})")
        except Exception as e:
            print(f"⚠️ [Real-time Quality] Failed to switch video track in real-time: {e}")

    def _update_available_qualities_from_tracks(self):
        # Всегда держим полный список кнопок качеств видимым по вашему требованию!
        self._available_qualities = ["auto", "ultra", "high", "medium", "low", "minimal"]
        self.availableQualitiesChanged.emit()
        print("📊 [Qualities] Exposing all quality buttons unconditionally: auto, ultra, high, medium, low, minimal")

    @Slot(str, str, str, str)
    def play(self, url, name="", category="", start_raw=""):
        if not HAS_MPV or not self.player or not self._init:
            print("❌ Player unavailable")
            self._s = "❌ MPV не инициализирован"
            self.statusChanged.emit()
            return
        
        f_url = url
        if start_raw:
            try:
                ts = int(datetime.strptime(start_raw, "%Y%m%d%H%M%S").replace(timezone.utc).timestamp())
                f_url = f"{url}?utc={ts}" if "?" not in url else f"{url}&utc={ts}"
            except:
                pass
        
        print(f"🎬 Playing: {name}")
        print(f"   URL: {f_url[:80]}...")
        
        self._last_url = f_url
        self._last_channel_name = name
        self._last_category = category
        self._retry_count = 0
        self._available_qualities = ["auto", "ultra", "high", "medium", "low", "minimal"]
        self.availableQualitiesChanged.emit()
        self._qualities_analyzed = False
        self._s = "Воспроизведение..."
        self.statusChanged.emit()
        
        self._target_code = "ALL"
        self._target_name = "Глобальный"
        threading.Thread(target=self._detect_country, args=(f_url, category, name), daemon=True).start()
        
        # Тестируем качество соединения
        threading.Thread(target=self._test_connection_quality, args=(f_url,), daemon=True).start()
        
        try:
            root = self.engine.rootObjects()[0]
            if root:
                self.player.wid = int(root.winId())
            
            url_lower = f_url.lower()
            needs_proxy = any(domain in url_lower for domain in [
                'televizor-24', 'streaming.', 'online-television'
            ])
            
            is_iframe_url = 'iframe' in url_lower
            
            if is_iframe_url:
                print("⚠️ iframe URL detected")
                self._s = "⚠️ iframe URL - используйте прямой m3u8"
                self.statusChanged.emit()
            
            # Запускаем локальный HLS прокси для оптимизации
            if needs_proxy or is_iframe_url:
                start_hls_proxy(None, core=self)
                proxied = get_proxied_url(f_url)
                print(f"📡 Using optimized proxy: {proxied[:60]}...")
                self.player.play(proxied)
            else:
                print(f"🎬 Playing directly...")
                self.player.play(f_url)
            
            # Устанавливаем оптимальные параметры для потока
            self._apply_stream_optimizations()
            
        except Exception as e:
            print(f"❌ Play error: {e}")
            self._s = f"❌ Ошибка воспроизведения: {type(e).__name__}"
            self.statusChanged.emit()

    def _apply_stream_optimizations(self):
        """Применяет оптимизации для текущего потока"""
        if not HAS_MPV or not self.player:
            return
            
        try:
            # Оптимизация для live потоков и keep-alive соединений
            try:
                self.player['live-keepalive'] = 'yes'
            except:
                pass
                
            try:
                self.player['force-seekable'] = 'yes'
            except:
                pass
            
            # Таймауты
            try:
                self.player['network-timeout'] = '30'
            except:
                pass
                
            try:
                self.player['stream-timeout'] = '15'
            except:
                pass
            
            # Настройка адаптивного буфера до 60 секунд
            try:
                self.player['demuxer-readahead-secs'] = '60'
                self.player['cache'] = 'yes'
            except:
                pass
                
            # Принудительно применяем текущее выбранное пользователем качество к новому потоку
            try:
                self.setQuality(self._current_quality)
            except Exception as q_err:
                print(f"⚠️ Failed to apply quality on play: {q_err}")
                
            # Запускаем динамический анализ разрешения через 2.5 секунды после начала воспроизведения
            try:
                QTimer.singleShot(2500, self._update_available_qualities_from_tracks)
            except:
                pass
                
            print("[Optimizer] Applied stream optimizations")
        except Exception as e:
            print(f"[Optimizer] Optimization error: {e}")

    def _detect_country(self, url, cat, name):
        code, cn = detect_country(cat, name)
        if code == "ALL":
            try:
                host = url.split("://")[-1].split("/")[0].split(":")[0]
                if host and not host.startswith("127.") and not host.startswith("192.168.") and not host.startswith("10."):
                    ip = socket.gethostbyname(host)
                    cc, nn = get_ip_country(ip)
                    if cc != "ALL":
                        code, cn = cc, nn
            except:
                pass
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
                self._s = "Ready"
                self.statusChanged.emit()
                self.playingChanged.emit(False)
            except:
                pass

    @Property(str, notify=statusChanged)
    def targetCode(self): return self._target_code

    @Property(str, notify=statusChanged)
    def targetName(self): return self._target_name

    @Slot(str, result=str)
    def getFallback(self, channel_name):
        fallback = get_fallback_url(channel_name)
        if fallback:
            print(f"[Player] Trying fallback: {fallback[:50]}...")
            try:
                self.player.play(fallback)
                self._s = "Использую резервный поток"
                self.statusChanged.emit()
                return fallback
            except:
                pass
        
        return None

    @Property(QObject, constant=True)
    def epgModel(self):
        return self._em


if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    engine = QQmlApplicationEngine()
    core = IPTVCore(engine)
    engine.rootContext().setContextProperty("backend", core)
    engine.load(os.path.join(os.path.dirname(__file__), "main.qml"))
    sys.exit(app.exec())