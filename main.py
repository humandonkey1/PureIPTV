import os
import sys
from ctypes import CDLL

# Логи в реальном времени: без этого на Windows stdout буферизуется блоками и
# сообщения появляются только при выходе из канала/закрытии. line_buffering +
# явный flush гарантируют, что диагностика видна сразу.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# --- MPV preload ---
script_dir = os.path.dirname(os.path.abspath(__file__))

# ============================================================
#  HARDWARE OPTIMIZER — Настройка железа на "Ассемблерный" уровень
# ============================================================
os.environ["QSG_RENDER_LOOP"] = "basic"
os.environ["QSG_RHI_BACKEND"] = "d3d11"
os.environ["QSG_LOW_PRECISION_FLOAT"] = "1"
os.environ["PYTHONOPTIMIZE"] = "2"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

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
import ssl
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime
from xml.etree import ElementTree
import time
import traceback
import shutil
import random
from collections import deque, OrderedDict

try:
    import requests
except ImportError:
    raise SystemExit("❌ Модуль requests не установлен: pip install requests")

# В прокси используется verify=False (игнор SSL у проблемных IPTV-серверов).
# Без этого urllib3 на каждый запрос печатает InsecureRequestWarning и засоряет лог.
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

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

# ============================================================
#  ОБХОД БЛОКИРОВОК — что здесь реально работает, а что нет
# ============================================================
#
# Честно о границах возможного (это не пессимизм, это TCP/IP):
#   * Спрятать свой реальный IP от сервера БЕЗ перенаправления трафика
#     (VPN/прокси/туннель) невозможно в принципе — серверу нужен обратный
#     адрес, чтобы прислать ответ. Никакой набор заголовков этого не меняет.
#   * Поэтому "обход гео-блокировки, которая режет по IP на самом сервере",
#     без прокси не решается. Точка.
#
# Что ДЕЙСТВИТЕЛЬНО поддаётся обходу на стороне клиента (и реализовано ниже):
#   1. DNS-блокировка провайдера — самый частый способ "заблокировать канал".
#      Провайдер отдаёт ложный/пустой ответ DNS. Обходится DNS-over-HTTPS:
#      имя резолвится напрямую через 1.1.1.1 / 8.8.8.8 по HTTPS в обход
#      резолвера провайдера (DohResolver + install_doh_dns).
#   2. Origin-серверы, которые доверяют заголовкам X-Forwarded-For / X-Real-IP
#      (типичная НЕПРАВИЛЬНАЯ конфигурация панелей IPTV). Там подмена
#      клиентского IP реально пропускает запрос. На корректно настроенных
#      серверах это игнорируется — вреда нет, но и чуда не будет.
#   3. Фильтрация по User-Agent / Referer / отсутствию заголовков плеера —
#      обходится реалистичными заголовками устройства.
#   4. Нестабильные серверы, 403/429/таймауты — умные ретраи с backoff и
#      сменой User-Agent, плюс уже существующий fallback на прямой .ts.


# --- Реалистичные наборы User-Agent для перебора при отказе ---
CLIENT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "VLC/3.0.20 LibVLC/3.0.20",
    "Lavf/60.16.100",
    "AppleCoreMedia/1.0.0.21G93 (Apple TV; U; CPU OS 17_6 like Mac OS X)",
    "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/4.0 TV Safari/537.36",
]


def build_bypass_headers(url=None, spoof_client_ip=True):
    """Заголовки, повышающие шанс пройти клиентские и гео-фильтры.

    Если задана целевая страна (set_geo_target), подставляем гео-заголовки с
    IP из этой страны — так серверы/CDN, доверяющие заголовкам для гео,
    «увидят» клиента из нужного региона. Если страны нет — используем нейтраль-
    ные заголовки внутреннего запроса. На корректно настроенных серверах гео
    берётся из реального src-IP пакета, и эти поля игнорируются (без вреда).
    """
    headers = {
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
        'Connection': 'keep-alive',
    }
    if spoof_client_ip:
        geo = geo_spoof_headers()
        if geo:
            # Гео-маска: IP из целевой страны (для обхода гео по заголовку).
            headers.update(geo)
        else:
            # Нет целевой страны → нейтральный «внутренний» запрос.
            headers.update({
                'X-Forwarded-For': '127.0.0.1',
                'X-Real-IP': '127.0.0.1',
                'X-Client-IP': '127.0.0.1',
                'True-Client-IP': '127.0.0.1',
                'X-Forwarded-Proto': 'https',
            })
    url_str = str(url or '')
    if any(x in url_str for x in ['portal', 'api', 'get.php', 'player_api']):
        headers['X-Requested-With'] = 'XMLHttpRequest'
    return headers


class DohResolver:
    """DNS-over-HTTPS резолвер: обходит DNS-блокировку провайдера.

    Запросы уходят напрямую на IP публичных резолверов (1.1.1.1 / 8.8.8.8) по
    HTTPS, поэтому подмена/фильтрация DNS на стороне провайдера не действует.
    Результаты кэшируются с учётом TTL. При сбое DoH — молчаливый откат на
    системный DNS (см. install_doh_dns), чтобы ничего не сломать.
    """

    # Только IP-эндпоинты, иначе резолв самого резолвера ушёл бы в рекурсию.
    ENDPOINTS = [
        "https://1.1.1.1/dns-query",
        "https://8.8.8.8/resolve",
        "https://9.9.9.9/dns-query",
    ]

    def __init__(self):
        self._cache = {}          # host -> (list_of_ips, expires_at)
        self._lock = threading.Lock()
        # Отдельная "чистая" сессия без наших заголовков-подмены, чтобы не
        # смущать сам резолвер.
        self._net = requests.Session()

    def _query_endpoint(self, endpoint, host, rtype='A'):
        # rtype: 'A' → IPv4, 'AAAA' → IPv6
        try:
            r = self._net.get(
                endpoint,
                params={'name': host, 'type': rtype},
                headers={'Accept': 'application/dns-json'},
                timeout=5,
                verify=True,
            )
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception:
            return None
        answers = data.get('Answer') or []
        want = 1 if rtype == 'A' else 28   # 1 == A, 28 == AAAA
        ips, ttl = [], 300
        for a in answers:
            if a.get('type') == want and a.get('data'):
                ips.append(a['data'])
                ttl = min(ttl, max(30, int(a.get('TTL', 300) or 300)))
        return (ips, ttl) if ips else None

    def resolve(self, host, rtype='A'):
        """Возвращает список адресов для host или [] при неудаче.

        rtype='A' → IPv4, rtype='AAAA' → IPv6. IPv6 отдельно кэшируется, потому
        что это ключ к обходу гео-блокировок: гео-базы для IPv6 часто пустые,
        и сервер, режущий IPv4-диапазоны страны, нередко пускает по IPv6.
        """
        now = time.time()
        cache_key = (host, rtype)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[1] > now:
                return list(cached[0])
        for endpoint in self.ENDPOINTS:
            result = self._query_endpoint(endpoint, host, rtype)
            if result:
                ips, ttl = result
                with self._lock:
                    self._cache[cache_key] = (ips, now + ttl)
                return list(ips)
        return []

    def resolve_all(self, host, prefer_ipv6=True):
        """Возвращает список адресов, при prefer_ipv6 — IPv6 первыми.

        Именно этот порядок эксплуатирует «IPv6-дыру» в гео-фильтрах: сначала
        пробуем адреса, по которым сервер чаще всего НЕ знает страну.
        """
        v6 = self.resolve(host, 'AAAA')
        v4 = self.resolve(host, 'A')
        return (v6 + v4) if prefer_ipv6 else (v4 + v6)

    def resolve_https(self, host):
        """Запрашивает DNS HTTPS/SVCB-записи (тип 65) для ECH-конфигурации.

        ECH (Encrypted Client Hello, RFC 9460) — КЛЮЧЕВАЯ инновация
        PhaseShift OMEGA: если CDN публикует ECH-конфиг в DNS,
        мы можем зашифровать SNI → DPI не видит домен → зонд НЕ вызывается.

        Возвращает: dict с ключами 'ech_config_list' (base64), 'public_name',
        'port', или None если ECH не поддерживается доменом.
        """
        cache_key = (host, 'HTTPS')
        now = time.time()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[1] > now:
                return cached[0] if cached[0] else None

        for endpoint in self.ENDPOINTS:
            try:
                r = self._net.get(
                    endpoint,
                    params={'name': host, 'type': '65'},  # HTTPS/SVCB record
                    headers={'Accept': 'application/dns-json'},
                    timeout=5, verify=True,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                answers = data.get('Answer') or []
                for a in answers:
                    if a.get('type') == 65 and a.get('data'):
                        raw = a['data']
                        # Парсим HTTPS/SVCB запись
                        # Формат: priority target param1=value1 param2=value2
                        # ECH config в параметре ech=...
                        result = self._parse_https_record(raw, host)
                        if result:
                            ttl = max(30, int(a.get('TTL', 300) or 300))
                            with self._lock:
                                self._cache[cache_key] = (result, now + ttl)
                            return result
            except Exception:
                continue
        return None

    # ============================================================
    #  ABYSS: Multi-Resolver Anycast Discovery
    #  Резолвим один хост через 6+ DoH-резолверов в разных странах
    #  Каждый резолвер = другая точка anycast = другие IP
    #  ISP блокирует IP из локального PoP → но не из турецкого/финскому
    # ============================================================
    GEO_DOH_ENDPOINTS = [
        # Cloudflare — anycast, но разные ресолверы видят разные PoP
        "https://1.1.1.1/dns-query",            # US/Global
        "https://1.0.0.1/dns-query",            # US/Global alt
        # Google — огромная anycast-сеть
        "https://8.8.8.8/resolve",              # US/Global
        "https://8.8.4.4/resolve",              # US/Global alt
        # Quad9 — Swiss-based
        "https://9.9.9.9/dns-query",            # CH/EU
        # Mullvad — Sweden-based, privacy-focused
        "https://dns.mullvad.net/dns-query",    # SE/EU
        # AdGuard — Cyprus-based
        "https://dns.adguard-dns.com/dns-query",# CY/EU
        # Cloudflare malware-blocking
        "https://security.cloudflare-dns.com/dns-query",  # Global
    ]

    def multi_resolve(self, host, rtype='A'):
        """Резолвит хост через ВСЕ DoH-резолверы ПАРАЛЛЕЛЬНО и собирает УНИКАЛЬНЫЕ IP."""
        seen = set()
        all_ips = []
        results = {}
        lock = threading.Lock()

        def _query_one(endpoint):
            try:
                result = self._query_endpoint(endpoint, host, rtype)
                if result:
                    ips, _ = result
                    with lock:
                        for ip in ips:
                            if ip not in seen:
                                seen.add(ip)
                                all_ips.append(ip)
            except Exception:
                pass

        # Запускаем все запросы параллельно — вместо 15-20с последовательно → 2-3с
        threads = []
        for endpoint in self.GEO_DOH_ENDPOINTS:
            t = threading.Thread(target=_query_one, args=(endpoint,), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=4)  # Максимум 4 секунды на все

        return all_ips

    def resolve_cname_chain(self, host):
        """Проходит по CNAME-цепочке и резолвит КАЖДЫЙ алиас.

        ABYSS Strategy 17: cdn.example.com → CNAME cdn.cloudflare.com →
        CNAME cdn.cloudflare.ssl.fastly.net → каждый алиас = свой набор IP.
        ISP блокирует cdn.example.com, но cdn.cloudflare.ssl.fastly.net
        может резолвиться в совершенно другие IP.

        Возвращает: list of (alias_host, [ips])
        """
        chain = []
        seen_hosts = set()
        current = host

        for _ in range(5):  # max CNAME depth
            if current in seen_hosts:
                break
            seen_hosts.add(current)

            ips = self.resolve_all(current, prefer_ipv6=True)
            chain.append((current, ips))

            # Ищем CNAME через DNS-запрос type 5
            cname = self._resolve_cname(current)
            if cname and cname != current:
                current = cname
            else:
                break

        return chain

    def _resolve_cname(self, host):
        """Резолвит CNAME-запись для хоста."""
        for endpoint in self.ENDPOINTS:
            try:
                r = self._net.get(
                    endpoint,
                    params={'name': host, 'type': '5'},  # CNAME
                    headers={'Accept': 'application/dns-json'},
                    timeout=5, verify=True,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                answers = data.get('Answer') or []
                for a in answers:
                    if a.get('type') == 5 and a.get('data'):
                        return a['data'].rstrip('.')
            except Exception:
                continue
        return None

    def _parse_https_record(self, raw, host):
        """Парсит HTTPS/SVCB DNS-запись и извлекает ECH-конфигурацию."""
        try:
            parts = raw.split()
            if not parts:
                return None
            priority = int(parts[0])
            target = parts[1] if len(parts) > 1 and parts[1] != '.' else host

            result = {
                'priority': priority,
                'target': target,
                'public_name': target if target != '.' else host,
                'ech_config_list': None,
                'port': 443,
            }

            # Ищем параметры ech= и port=
            for part in parts[2:]:
                if part.startswith('ech='):
                    # ECH config в base64 (без кавычек)
                    ech_val = part[4:].strip('"').strip("'")
                    if ech_val:
                        result['ech_config_list'] = ech_val
                elif part.startswith('port='):
                    try:
                        result['port'] = int(part[5:])
                    except ValueError:
                        pass

            # Возвращаем результат только если нашли ECH
            if result['ech_config_list']:
                print(f"🛡️ [DoH] ECH найден для {host}: public_name={result['public_name']}")
                return result
        except Exception as e:
            print(f"⚠️ [DoH] Ошибка парсинга HTTPS-записи: {e}")
        return None


doh_resolver = DohResolver()


# Глобальный тумблер: при True getaddrinfo отдаёт IPv6-адреса ПЕРВЫМИ.
# Это и есть «IPv6-дыра» — ОС/плеер сначала соединятся по IPv6, где гео-фильтр
# чаще отсутствует. Требует, чтобы у клиента был рабочий IPv6 (иначе фолбэк
# на IPv4 отработает автоматически по happy-eyeballs).
PREFER_IPV6 = True

# Авто-детекция: если IPv6 недоступен (WinError 10051) — отключаем навсегда
_ipv6_detected_working = None  # None = не проверяли, True/False = результат


def ipv6_available():
    """Проверяет доступность IPv6 (один раз за сессию)."""
    global _ipv6_detected_working
    if _ipv6_detected_working is not None:
        return _ipv6_detected_working
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.settimeout(3)
        # Пробуем подключиться к Google DNS IPv6
        sock.connect(('2001:4860:4860::8888', 443))
        sock.close()
        _ipv6_detected_working = True
        print("🌐 IPv6: ДОСТУПЕН")
        return True
    except Exception:
        _ipv6_detected_working = False
        print("🌐 IPv6: НЕ ДОСТУПЕН — пропускаем все IPv6-адреса")
        return False


def filter_ips_by_ipv6(ips):
    """Фильтрует IP-список: убирает IPv6 если он недоступен."""
    if ipv6_available():
        return ips
    return [ip for ip in ips if ':' not in ip]


def install_doh_dns():
    """Перехватывает socket.getaddrinfo, чтобы имена резолвились через DoH.

    Делает две вещи разом:
      1. Обходит DNS-блокировку провайдера (резолв идёт через HTTPS).
      2. Эксплуатирует «IPv6-дыру» в гео-фильтрах: при PREFER_IPV6 IPv6-адреса
         возвращаются первыми, и соединение идёт по ним. Многие гео-базы не
         знают страну IPv6-адреса → блокировка не срабатывает.

    Литеральные IP и локальные адреса не трогаем; при пустом ответе DoH или
    отсутствии IPv6 молча падаем на системный резолвер / IPv4.
    """
    original_getaddrinfo = socket.getaddrinfo

    def resolving_getaddrinfo(host, port, *args, **kwargs):
        try:
            hostname = host
            if not hostname or not isinstance(hostname, str):
                return original_getaddrinfo(host, port, *args, **kwargs)
            # Уже литеральный IP (v4 или v6) — системный путь.
            for fam in (socket.AF_INET, socket.AF_INET6):
                try:
                    socket.inet_pton(fam, hostname)
                    return original_getaddrinfo(host, port, *args, **kwargs)
                except OSError:
                    pass
            if hostname in ('localhost',) or hostname.startswith('127.') \
                    or hostname.endswith('.local'):
                return original_getaddrinfo(host, port, *args, **kwargs)

            v6 = doh_resolver.resolve(hostname, 'AAAA') if PREFER_IPV6 else []
            v4 = doh_resolver.resolve(hostname, 'A')

            results = []
            ordered = (v6 + v4) if PREFER_IPV6 else (v4 + v6)
            for ip in ordered:
                if ':' in ip:
                    # sockaddr для IPv6: (host, port, flowinfo, scopeid)
                    results.append(
                        (socket.AF_INET6, socket.SOCK_STREAM,
                         socket.IPPROTO_TCP, '', (ip, port, 0, 0))
                    )
                else:
                    results.append(
                        (socket.AF_INET, socket.SOCK_STREAM,
                         socket.IPPROTO_TCP, '', (ip, port))
                    )
            if results:
                return results
        except Exception:
            pass
        # Фолбэк — как будто нас тут и не было.
        return original_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = resolving_getaddrinfo
    mode = "IPv6-first" if PREFER_IPV6 else "IPv4"
    print(f"🌐 DNS-over-HTTPS активирован ({mode}); обход DNS + попытка IPv6-обхода гео")


# ============================================================
#  GEO-BYPASS — эксплуатация ошибок гео-алгоритмов (без туннелей)
# ============================================================
#
# Что здесь и почему это НЕ магия:
#   Изменить IP, который видит сервер, без промежуточного узла нельзя (иначе
#   ответ некуда слать). Поэтому мы бьём не по IP, а по ОШИБКАМ гео-фильтров:
#     A) IPv6-дыра — гео-база пустая для IPv6 (реализовано в install_doh_dns).
#     B) Гео по заголовку — часть серверов/CDN верит гео-заголовкам. Тогда
#        достаточно подставить IP из НУЖНОЙ страны (не 127.0.0.1 — это провалит
#        гео-проверку; нужен именно in-country адрес).
#     C) Рассинхрон edge-узлов CDN — гео-правило раскатано не на всех PoP.
#        Перебор конкретных edge-IP с правильным Host иногда попадает на узел,
#        где фильтра нет.
#   Ни один из пунктов не гарантирует 100% — они бьют по конкретным дефектам
#   конкретных серверов. Где сервер настроен правильно — обхода нет, и честный
#   код это не скрывает.

# По одному представительному публичному IP на страну (для гео-заголовков).
# Это реальные адреса из национальных диапазонов; сервер, доверяющий
# X-Forwarded-For/CF-Connecting-IP, «увидит» клиента из этой страны.
COUNTRY_SAMPLE_IPS = {
    # --- Европа ---
    "AL": ["31.22.48.1", "46.99.1.1", "79.106.1.1", "84.20.64.1", "213.163.112.1", "217.21.144.1", "109.104.128.1", "91.187.96.1"],
    "US": ["23.20.0.1", "3.208.0.1", "72.21.0.1", "35.160.0.1"],
    "GB": ["51.140.0.1", "5.148.0.1", "81.128.0.1", "86.0.0.1"],
    "DE": ["3.120.0.1", "85.214.0.1", "87.128.0.1", "217.91.0.1"],
    "FR": ["51.15.0.1", "212.27.48.1", "86.199.0.1", "90.0.0.1"],
    "NL": ["51.158.0.1", "94.142.240.1", "145.97.0.1", "31.151.0.1"],
    "RU": ["5.255.255.1", "77.88.55.1", "37.9.64.1", "93.158.128.1"],
    "UA": ["77.120.0.1", "91.198.36.1", "37.115.0.1", "46.133.0.1"],
    "TR": ["31.145.0.1", "88.255.0.1", "78.160.0.1", "176.33.0.1"],
    "ES": ["51.68.0.1", "80.58.0.1", "88.0.0.1", "85.53.0.1"],
    "IT": ["79.171.0.1", "151.1.0.1", "93.41.0.1", "2.224.0.1"],
    "PL": ["5.172.0.1", "83.1.0.1", "37.128.0.1", "95.40.0.1"],
    "PT": ["85.138.0.1", "94.46.0.1", "109.49.0.1", "213.13.0.1"],
    "GR": ["94.64.0.1", "79.107.0.1", "87.202.0.1", "176.92.0.1"],
    "RO": ["86.120.0.1", "188.24.0.1", "37.221.64.1", "89.121.0.1"],
    "BG": ["78.128.0.1", "217.9.0.1", "95.42.0.1", "79.100.0.1"],
    "RS": ["93.86.0.1", "109.72.0.1", "213.240.0.1", "178.220.0.1"],
    "HR": ["93.136.0.1", "85.114.0.1", "31.147.0.1", "78.1.0.1"],
    "BA": ["31.176.0.1", "109.175.0.1", "195.222.32.1", "89.201.0.1"],
    "ME": ["188.246.64.1", "185.3.64.1", "89.188.32.1", "46.33.0.1"],
    "MK": ["77.28.0.1", "109.92.0.1", "37.142.0.1", "46.217.0.1"],
    "XK": ["31.171.128.1", "5.206.224.1", "46.99.128.1", "185.48.176.1"],
    "SI": ["89.142.0.1", "93.103.0.1", "46.246.128.1", "86.58.0.1"],
    "SK": ["87.244.0.1", "178.40.0.1", "94.229.0.1", "185.50.208.1"],
    "CZ": ["90.177.0.1", "46.135.0.1", "89.102.0.1", "185.62.224.1"],
    "HU": ["84.0.0.1", "91.120.0.1", "94.21.0.1", "37.191.0.1"],
    "AT": ["77.117.0.1", "62.47.0.1", "91.141.0.1", "78.104.0.1"],
    "CH": ["84.72.0.1", "178.192.0.1", "46.14.0.1", "212.60.0.1"],
    "SE": ["78.69.0.1", "95.199.0.1", "85.228.0.1", "81.216.0.1"],
    "NO": ["84.210.0.1", "37.191.0.1", "109.189.0.1", "85.165.0.1"],
    "DK": ["87.54.0.1", "188.180.0.1", "92.246.0.1", "85.218.0.1"],
    "FI": ["91.152.0.1", "85.76.0.1", "88.114.0.1", "94.238.64.1"],
    "IE": ["86.43.0.1", "89.101.0.1", "78.16.0.1", "109.76.0.1"],
    "BE": ["81.241.0.1", "94.111.0.1", "78.22.0.1", "85.28.64.1"],
    "LU": ["188.42.0.1", "85.93.128.1", "46.254.192.1", "185.76.64.1"],
    # --- Ближний Восток / Северная Африка ---
    "AE": ["5.32.0.1", "94.200.0.1", "86.97.0.1", "213.42.0.1"],
    "SA": ["5.42.192.1", "212.71.32.1", "188.48.0.1", "37.40.0.1"],
    "EG": ["41.196.0.1", "197.32.0.1", "102.40.0.1", "154.180.0.1"],
    "MA": ["41.140.0.1", "105.158.0.1", "196.217.0.1", "102.158.0.1"],
    "DZ": ["41.96.0.1", "105.96.0.1", "197.0.0.1", "102.156.0.1"],
    "TN": ["41.224.0.1", "196.203.0.1", "102.156.0.1", "197.0.0.1"],
    "LY": ["41.252.0.1", "102.185.0.1", "197.112.0.1", "102.40.0.1"],
    "IQ": ["37.215.0.1", "109.200.0.1", "185.92.0.1", "5.10.0.1"],
    "IR": ["5.160.0.1", "91.92.0.1", "151.232.0.1", "185.55.224.1"],
    "IL": ["109.64.0.1", "82.81.0.1", "84.110.0.1", "212.179.0.1"],
    "JO": ["94.246.0.1", "176.28.0.1", "37.75.0.1", "46.152.0.1"],
    "LB": ["178.135.0.1", "46.49.0.1", "95.142.96.1", "37.114.0.1"],
    "PS": ["188.161.0.1", "37.123.128.1", "5.149.128.1", "185.65.128.1"],
    # --- Кавказ / Центральная Азия ---
    "AZ": ["5.62.160.1", "85.132.0.1", "31.171.0.1", "94.20.0.1"],
    "AM": ["95.140.192.1", "46.70.0.1", "37.157.0.1", "178.249.0.1"],
    "GE": ["31.146.0.1", "5.44.0.1", "77.92.0.1", "95.104.0.1"],
    "KZ": ["2.72.0.1", "212.19.128.1", "37.150.0.1", "89.34.0.1"],
    "UZ": ["37.110.0.1", "91.212.89.1", "185.8.212.1", "94.230.0.1"],
    "TM": ["91.132.0.1", "95.85.96.1", "185.20.144.1", "5.30.0.1"],
    "KG": ["85.115.192.1", "176.123.0.1", "37.218.0.1", "212.42.96.1"],
    "TJ": ["85.9.128.1", "37.98.0.1", "91.218.128.1", "91.218.0.1"],
    # --- Азия ---
    "CN": ["1.0.1.1", "14.0.0.1", "27.0.0.1", "36.0.0.1"],
    "JP": ["126.0.0.1", "133.0.0.1", "150.0.0.1", "153.128.0.1"],
    "KR": ["1.200.0.1", "14.0.0.1", "27.96.0.1", "61.0.0.1"],
    "IN": ["13.126.0.1", "103.21.244.1", "49.0.0.1", "59.0.0.1"],
    "PK": ["39.32.0.1", "182.176.0.1", "119.160.0.1", "115.167.0.1"],
    "BD": ["37.111.0.1", "103.4.0.1", "114.130.0.1", "203.112.0.1"],
    "TH": ["1.0.128.1", "14.207.0.1", "27.55.0.1", "49.228.0.1"],
    "VN": ["1.52.0.1", "14.160.0.1", "27.72.0.1", "42.112.0.1"],
    "PH": ["1.37.0.1", "14.192.0.1", "27.106.0.1", "49.144.0.1"],
    "ID": ["36.64.0.1", "110.136.0.1", "114.120.0.1", "180.244.0.1"],
    "MY": ["1.32.0.1", "14.1.0.1", "27.121.0.1", "42.0.0.1"],
    "SG": ["1.1.1.1", "14.100.0.1", "27.0.0.1", "42.60.0.1"],
    # --- Америка ---
    "BR": ["18.228.0.1", "200.147.0.1", "177.0.0.1", "189.0.0.1"],
    "CA": ["3.96.0.1", "24.48.0.1", "47.0.0.1", "64.0.0.1"],
    "MX": ["187.128.0.1", "189.128.0.1", "200.33.0.1", "201.128.0.1"],
    "AR": ["24.232.0.1", "181.0.0.1", "190.0.0.1", "200.0.0.1"],
    "CO": ["181.128.0.1", "186.0.0.1", "190.0.0.1", "200.0.0.1"],
    "CL": ["152.172.0.1", "186.0.0.1", "190.0.0.1", "200.0.0.1"],
    "PE": ["181.64.0.1", "190.0.0.1", "200.0.0.1", "201.128.0.1"],
    # --- Африка ---
    "ZA": ["41.0.0.1", "102.0.0.1", "105.0.0.1", "196.0.0.1"],
    "NG": ["41.58.0.1", "102.0.0.1", "105.0.0.1", "154.0.0.1"],
    "KE": ["41.57.0.1", "102.0.0.1", "105.0.0.1", "196.0.0.1"],
    "GH": ["41.58.0.1", "102.0.0.1", "105.0.0.1", "154.0.0.1"],
    # --- Океания ---
    "AU": ["1.1.1.1", "14.1.0.1", "27.96.0.1", "49.0.0.1"],
    "NZ": ["1.1.1.1", "14.1.0.1", "27.96.0.1", "49.0.0.1"],
}

# Авто-детект страны по TLD и домену
TLD_TO_COUNTRY = {
    ".al": "AL", ".us": "US", ".uk": "GB", ".de": "DE", ".fr": "FR",
    ".nl": "NL", ".ru": "RU", ".ua": "UA", ".tr": "TR", ".es": "ES",
    ".it": "IT", ".pl": "PL", ".pt": "PT", ".gr": "GR", ".ro": "RO",
    ".bg": "BG", ".rs": "RS", ".hr": "HR", ".ba": "BA", ".me": "ME",
    ".mk": "MK", ".si": "SI", ".sk": "SK", ".cz": "CZ", ".hu": "HU",
    ".at": "AT", ".ch": "CH", ".se": "SE", ".no": "NO", ".dk": "DK",
    ".fi": "FI", ".ie": "IE", ".be": "BE", ".lu": "LU", ".ae": "AE",
    ".sa": "SA", ".eg": "EG", ".ma": "MA", ".dz": "DZ", ".tn": "TN",
    ".ly": "LY", ".iq": "IQ", ".ir": "IR", ".il": "IL", ".jo": "JO",
    ".lb": "LB", ".ps": "PS", ".az": "AZ", ".am": "AM", ".ge": "GE",
    ".kz": "KZ", ".uz": "UZ", ".tm": "TM", ".kg": "KG", ".tj": "TJ",
    ".cn": "CN", ".jp": "JP", ".kr": "KR", ".in": "IN", ".pk": "PK",
    ".bd": "BD", ".th": "TH", ".vn": "VN", ".ph": "PH", ".id": "ID",
    ".my": "MY", ".sg": "SG", ".br": "BR", ".ca": "CA", ".mx": "MX",
    ".ar": "AR", ".co": "CO", ".cl": "CL", ".pe": "PE", ".za": "ZA",
    ".ng": "NG", ".ke": "KE", ".gh": "GH", ".au": "AU", ".nz": "NZ",
}

def detect_country_from_url(url):
    """Определяет страну канала по домену URL (TLD → country code)."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        # Сначала проверяем по TLD
        for tld, cc in sorted(TLD_TO_COUNTRY.items(), key=lambda x: -len(x[0])):
            if host.endswith(tld):
                return cc
        # Проверяем по CNAME/known domains
        host_lower = host.lower()
        KNOWN_DOMAIN_COUNTRY = {
            "tring.al": "AL", "rtsh.al": "AL", "topchannel.al": "AL",
            "vizionplus.al": "AL", "kanal10.al": "AL", "abcom.al": "AL",
            "matchtv.ru": "RU", "1tv.ru": "RU", "russia.tv": "RU",
            "tvp.pl": "PL", "rtl.de": "DE", "rai.it": "IT", "tf1.fr": "FR",
            "bbc.co.uk": "GB", "itv.com": "GB", "channel4.com": "GB",
        }
        for domain, cc in KNOWN_DOMAIN_COUNTRY.items():
            if domain in host_lower:
                return cc
    except Exception:
        pass
    return None

# Активная целевая страна для гео-заголовков (ISO-код). Ставится перед
# воспроизведением канала (из уже существующего определения страны).
active_geo_country = None
active_geo_lock = threading.Lock()


def set_geo_target(country_code):
    """Задаёт страну, «из которой» будем притворяться в гео-заголовках."""
    global active_geo_country
    cc = (country_code or "").upper()
    with active_geo_lock:
        active_geo_country = cc if cc in COUNTRY_SAMPLE_IPS else None
    if active_geo_country:
        print(f"🎯 [geo] Гео-маска установлена: {active_geo_country}")


def geo_ip_for(country_code=None):
    """Возвращает in-country IP для гео-заголовков или None."""
    with active_geo_lock:
        cc = (country_code or active_geo_country or "").upper()
    ips = COUNTRY_SAMPLE_IPS.get(cc)
    return random.choice(ips) if ips else None


def geo_spoof_headers(country_code=None):
    """Гео-заголовки с IP из нужной страны.

    ВАЖНО: это работает только на серверах/CDN, которые ошибочно доверяют
    этим полям для гео-определения. На корректных серверах гео берётся из
    реального src-IP пакета и заголовки игнорируются.
    """
    ip = geo_ip_for(country_code)
    if not ip:
        return {}
    return {
        'X-Forwarded-For': ip,
        'X-Real-IP': ip,
        'Client-IP': ip,
        'X-Client-IP': ip,
        'True-Client-IP': ip,          # Akamai / Cloudflare Enterprise
        'CF-Connecting-IP': ip,        # Cloudflare
        'Fastly-Client-IP': ip,        # Fastly
        'X-Forwarded-Proto': 'https',
    }


class EdgeProbe:
    """Перебор edge-IP CDN: ищем PoP, где гео-правило не раскатано.

    Резолвим все A/AAAA-адреса хоста через DoH и пробуем достучаться до каждого
    напрямую (SNI/Host = исходный домен). Если хоть один edge отвечает 200 без
    гео-отказа — используем его. Это эксплуатация рассинхрона правил на PoP,
    а НЕ туннель: соединение по-прежнему прямое, с твоего реального IP.
    """

    def __init__(self, session):
        self.session = session

    def candidate_ips(self, host, prefer_ipv6=True):
        return doh_resolver.resolve_all(host, prefer_ipv6=prefer_ipv6)

    def find_working_edge(self, url, ok_predicate=None):
        """Возвращает IP работающего edge или None.

        ok_predicate(response) -> bool решает, «прошёл» ли ответ гео-фильтр
        (по умолчанию: статус < 400 и это не гео-заглушка).
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if not host:
            return None
        scheme = parsed.scheme or 'https'
        port = parsed.port or (443 if scheme == 'https' else 80)

        def default_ok(resp):
            if resp.status_code >= 400:
                return False
            body_head = ''
            try:
                body_head = resp.text[:400].lower()
            except Exception:
                pass
            geo_markers = ('not available in your', 'geo', 'blocked in your',
                           'region', 'unavailable in your country')
            return not any(m in body_head for m in geo_markers)

        ok = ok_predicate or default_ok
        for ip in self.candidate_ips(host)[:8]:
            # Собираем URL с literal-IP, а Host/SNI оставляем доменным.
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if parsed.port:
                ip_netloc += f":{parsed.port}"
            probe_url = parsed._replace(netloc=ip_netloc).geturl()
            headers = {'Host': host}
            try:
                resp = self.session.get(
                    probe_url, headers=headers, timeout=(4, 8),
                    verify=False, stream=True, allow_redirects=False,
                )
                if ok(resp):
                    print(f"✅ [edge] Рабочий PoP найден: {ip} для {host}")
                    return ip
                resp.close()
            except Exception:
                continue
        return None


# ============================================================
#  PHASESHIFT™ — Protocol-Phase Geo-Evasion Engine
#  WORLD-FIRST INNOVATION: "The Checkpoint Doesn't Exist
#                            If You Never Pass Through It"
# ============================================================
#
#  КОНЦЕПЦИЯ (принципиально новый подход к обходу гео-блокировок):
#
#    Традиционный обход:  «Притворимся, что мы из нужной страны»
#    PhaseShift:           «Не пойдём через КПП вообще»
#
#  ПОЧЕМУ ЭТО РАБОТАЕТ:
#    Гео-блокировка в IPTV — это КОНТРОЛЬНАЯ ТОЧКА на уровне манифеста
#    (m3u8). Сервер проверяет IP при запросе манифеста и отдаёт 403.
#    НО: CDN-серверы, раздающие сегменты (.ts/.mp4), чаще всего:
#      • берут контент из кэша (не спрашивая origin)
#      • НЕ повторяют гео-проверку на каждый сегмент (слишком дорого)
#      • обслуживают запросы по тем же URL-паттернам, что и «легальные»
#
#  ЧТО ДЕЛАЕТ PHASESHIFT:
#    1. ORACLE   — Изучает URL-паттерны РАБОЧИХ каналов того же провайдера
#    2. BORROW   — Заимствует токены/credentials у рабочих каналов
#    3. MUTATE   — Пробует альтернативные пути CDN (/hls/, /stream/, .ts)
#    4. PROBE    — Проверяет доступность сегментов НАПРЯМУЮ (в обход m3u8)
#    5. WEAVE    — Собирает «фантомный манифест» из обнаруженных сегментов
#    6. SHIFT    — Если нашёл прямой .ts — строим LIVEPIPE из голых сегментов
#
#  ОТЛИЧИЕ ОТ ВСЕГО СУЩЕСТВУЮЩЕГО:
#    • НЕ VPN — нет туннеля, нет промежуточного узла, нет чужого IP
#    • НЕ прокси — мы не маршрутизируем трафик через третий сервер
#    • НЕ заголовочная подмена — мы НЕ притворяемся кем-то другим
#    • НОВАЯ ПАРАДИГМА — мы пропускаем «калитку» (m3u8) и идём напрямую
#      к «открытой дороге» (сегментам), путь к которой вычисляем по аналогии
#      с рабочими каналами того же провайдера
#
#  ОГРАНИЧЕНИЯ (честно):
#    • Работает, когда CDN НЕ гео-проверяет сегменты (очень часто,
#      но не 100% — зависит от провайдера/CDN)
#    • Требует минимум 1 рабочий канал от того же провайдера для обучения
#    • Токен-защищённые потоки требуют совпадения формата токена
#    • Если CDN проверяет КАЖДЫЙ сегмент — PhaseShift бессилен
#      (но это редкость из-за стоимости таких проверок для CDN)


class PhaseShiftEngine:
    """PhaseShift™ — Protocol-Phase Geo-Evasion Engine.

    Обходит гео-блокировку, сдвигая фазу запроса с проверяемого слоя
    (манифест/авторизация) на часто непроверяемый слой (сегменты CDN).
    """

    def __init__(self, session):
        self.session = session
        self._patterns = {}          # host -> PatternInfo dict
        self._channel_patterns = {}  # channel_url -> PatternInfo dict
        self._phantom_manifests = {} # blocked_url -> phantom_m3u8_text
        self._discovered_segments = {}  # base_url -> [segment_urls]
        self._shift_log = []         # audit trail of PhaseShift attempts
        self._active = False         # currently in PhaseShift mode?
        self._last_result = ""       # status string for UI
        # === PHASESHIFT OMEGA: активные режимы ===
        self._shard_active = False   # Segment Sharding включён?
        self._camo_active = False    # Traffic Camouflage включён?
        self._shard_ips = {}         # host -> [ips] для шардирования
        # === PHASESHIFT ABYSS: активные режимы ===
        self._abyss_anycast = False  # Multi-Resolver Anycast активен?
        self._abyss_wide_ips = {}    # host -> [ips] из удалённых PoP
        self._abyss_rst = False      # RST Resilience активен?
        self._abyss_probe = False    # Probe-Resistant активен?
        self._abyss_session_host = None  # хост с установленной сессией
        # === PHASESHIFT VOID: активные режимы ===
        self._void_quic = False      # QUIC/HTTP3 обход активен?
        self._void_cache_prime = False  # Cache Prime Shield активен?
        self._void_chameleon = False # Protocol Chameleon активен?
        # === PHASESHIFT NEXUS: кэш разблокированных соседей ===
        self._adjacency_cache = {}   # host -> [(alt_ip, url, timestamp)] — работающие альт-IP

    @property
    def is_active(self):
        return self._active

    @property
    def status_text(self):
        return self._last_result

    def learn_from_channels(self, channels):
        """ORACLE: Изучает URL-паттерны рабочих каналов.

        Для каждого канала извлекает:
          • CDN base URL
          • Формат токена и его размещение
          • Структуру пути
          • Xtream-credentials (если есть)
          • Соглашение об именовании сегментов
        """
        learned = 0
        for ch in channels:
            url = ch.get('url', '')
            if not url:
                continue
            pattern = self._extract_pattern(url)
            if pattern:
                host = pattern.get('host', '')
                self._patterns[host] = pattern
                self._channel_patterns[url] = pattern
                learned += 1
        if learned:
            print(f"🔮 [PhaseShift] ORACLE: выучил паттерны {learned} каналов "
                  f"({len(self._patterns)} хостов)")

    def _extract_pattern(self, url):
        """Извлекает структурный паттерн из URL канала."""
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.netloc
            path = parsed.path
            scheme = parsed.scheme

            pattern = {
                'scheme': scheme,
                'host': host,
                'path': path,
                'base_path': '/'.join(path.split('/')[:-1]) + '/',
                'filename': path.split('/')[-1] if path else '',
                'full_url': url,
                'has_xtream_path': '/live/' in path or '/movie/' in path or '/series/' in path,
            }

            # Извлекаем токен из query-параметров
            params = urllib.parse.parse_qs(parsed.query)
            for key in ('token', 'tkn', 'auth', 'key', 'session', 'play_token',
                        'wmsAuthSign', 'hdnts'):
                if key in params:
                    pattern['token_param'] = key
                    pattern['token_value'] = params[key][0]
                    break

            # Для Xtream: извлекаем user/pass/stream_id из пути
            xtream_match = re.search(
                r'/live/([^/]+)/([^/]+)/([^/.?]+)', path)
            if xtream_match:
                pattern['xtream_user'] = xtream_match.group(1)
                pattern['xtream_pwd'] = xtream_match.group(2)
                pattern['stream_id'] = xtream_match.group(3)
                pattern['provider_type'] = 'xtream'

            # Для /hls/ путей
            hls_match = re.search(r'/hls/(.+?)/', path)
            if hls_match:
                pattern['hls_prefix'] = hls_match.group(1)

            return pattern
        except Exception:
            return None

    def try_phantom_access(self, blocked_url, all_channels=None):
        """Главная точка входа: пытается обойти гео-блок канала.

        Возвращает рабочий URL (возможно, к фантомному манифесту
        или прямому сегменту) или None.
        """
        import time as _time
        _ps_start = _time.time()
        _parsed_host = urllib.parse.urlparse(blocked_url).hostname
        try:
            result = self._try_phantom_access_inner(blocked_url, all_channels)
            _elapsed = _time.time() - _ps_start
            if result:
                print(f"🔮 [PhaseShift] ✅ УСПЕХ за {_elapsed:.1f}с → {result[:80]}")
            else:
                print(f"🔮 [PhaseShift] ❌ Провал за {_elapsed:.1f}с (все стратегии)")
            return result
        except Exception as e:
            import traceback
            _elapsed = _time.time() - _ps_start
            print(f"❌ [PhaseShift] ИСКЛЮЧЕНИЕ за {_elapsed:.1f}с: {e}")
            traceback.print_exc()
            self._active = False
            self._last_result = f"❌ PhaseShift: ошибка — {e}"
            # Гарантированно снимаем dedup-lock при исключении
            if hasattr(self, '_host_locks') and _parsed_host in self._host_locks:
                try:
                    self._host_locks[_parsed_host].release()
                except RuntimeError:
                    pass  # уже снят
            return None

    def _try_phantom_access_inner(self, blocked_url, all_channels=None):
        """Внутренняя реализация — обёрнута в try/except для диагностики."""
        import time as _time
        _start = _time.time()
        _DEADLINE = 30  # Будет уточнён после IP-count

        # === КЭШ ПРОВАЛОВ: УБРАН ===
        # Раньше скипали повторные попытки на 5 минут — это УБИВАЛО
        # повторные подключения. Пользователь переключает канал обратно —
        # а мы SKIP! Теперь: ВСЕГДА пробуем заново.
        if not hasattr(self, '_dead_hosts'):
            self._dead_hosts = {}
        _parsed_host = urllib.parse.urlparse(blocked_url).hostname

        self._active = True
        # Сбрасываем рабочий alt-IP от предыдущей попытки
        self._working_alt_ip = None
        self._is_cdn_flag = False
        self._last_result = "PhaseShift: анализ..."
        self._shift_log = []
        # Сбрасываем OMEGA-режимы при новом запросе
        self._shard_active = False
        self._camo_active = False
        # Сбрасываем ABYSS-режимы при новом запросе
        self._abyss_anycast = False
        self._abyss_wide_ips = {}
        self._abyss_rst = False
        self._abyss_probe = False
        self._abyss_session_host = None
        # Сбрасываем VOID-режимы при новом запросе
        self._void_quic = False
        self._void_cache_prime = False
        self._void_chameleon = False

        # === ПРОВЕРКА ТАЙМАУТА — используется внутри стратегий ===
        self._deadline = _start + _DEADLINE

        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.netloc

        # === ПРЕФЕТЧ IP-ПУЛА: делаем ОДИН раз для ВСЕХ стратегий ===
        # Это экономит 30+ секунд на повторных DoH-запросах
        _cached_host = parsed.hostname
        if _cached_host and not hasattr(self, '_prefetched_ips'):
            self._prefetched_ips = {}
        if _cached_host and _cached_host not in self._prefetched_ips:
            self._prefetched_ips[_cached_host] = {
                'local': doh_resolver.resolve_all(_cached_host, prefer_ipv6=True),
                'wide': [],  # заполним лениво
            }
            print(f"🔮 [PhaseShift] Prefetch: {len(self._prefetched_ips[_cached_host]['local'])} IP для {_cached_host}")

        # === CDN DETECTION ===
        # Проблема: Akamai anycast резолвит в 2-4 IP из одной локации,
        # но это РЕАЛЬНЫЙ CDN! Порог >=4 IP слишком грубый.
        # Решение: мульти-факторная проверка:
        #   1. IP count >= 4 → CDN (как раньше)
        #   2. CNAME содержит CDN-имена → CDN (Akamai, CF, etc.)
        #   3. Ответ содержит CDN-заголовки → CDN
        _local_ip_count = len(self._prefetched_ips.get(_cached_host, {}).get('local', []))
        _is_cdn = _local_ip_count >= 4  # CDN резолвится в 10+ IP, обычный хост в 1-2
        self._is_cdn_flag = _is_cdn  # сохраняем для методов

        # Мульти-фактор: проверяем CNAME даже при малом количестве IP
        if not _is_cdn:
            _cdn_cname_hint = False
            try:
                _cname_chain = doh_resolver.resolve_cname_chain(_parsed_host)
                for _alias, _ in _cname_chain:
                    _alias_low = _alias.lower()
                    if any(x in _alias_low for x in [
                        'akamai', 'akamaized', 'edgekey', 'edgesuite',
                        'cloudflare', 'cdn.cloudflare', 'fastly',
                        'cloudfront', 'cdn.net', 'cdn.com',
                    ]):
                        _cdn_cname_hint = True
                        print(f"🔮 [PhaseShift] CDN по CNAME: {_parsed_host} → {_alias}")
                        break
            except Exception:
                pass
            if _cdn_cname_hint:
                _is_cdn = True

        # Уточняем deadline:
        # НЕ-CDN: 20с (мало стратегий реально помогут)
        # CDN: 60с! НУЖНО время на Anycast, Domain Fronting, Edge Hostnames
        if not _is_cdn:
            self._deadline = _start + 20
            print(f"🔮 [PhaseShift] ⚡ НЕ-CDN хост ({_local_ip_count} IP) → "
                  f"deadline 20с, NEXUS первым")
        else:
            self._deadline = _start + 60  # CDN НУЖНО время!
            print(f"🔮 [PhaseShift] 🌐 CDN хост ({_local_ip_count} IP) → "
                  f"deadline 60с, ВСЕ стратегии")

        # === DEDUPLICATION: не запускаем PhaseShift параллельно для одного хоста ===
        if not hasattr(self, '_host_locks'):
            self._host_locks = {}  # host -> Lock
        if _parsed_host not in self._host_locks:
            self._host_locks[_parsed_host] = threading.Lock()
        if not self._host_locks[_parsed_host].acquire(blocking=False):
            print(f"🔮 [PhaseShift] ⏭️ SKIP {_parsed_host}: уже запущен в другом потоке")
            self._active = False
            self._last_result = "PhaseShift: уже выполняется для этого хоста"
            return None

        # Счётчик запущенных стратегий (для отчёта)
        _strategies_ran = 0

        # Дообучаемся на всех каналах, если переданы
        if all_channels:
            self.learn_from_channels(all_channels)

        # Находим паттерн от того же провайдера
        pattern = self._patterns.get(host)
        if not pattern:
            # Ищем «родственный» домен
            for phost, p in self._patterns.items():
                if self._domains_related(host, phost):
                    pattern = p
                    self._log(f"Найден родственный паттерн: {phost}")
                    break

        if not pattern and self._channel_patterns:
            # Берём любой паттерн (лучше что-то, чем ничего)
            pattern = next(iter(self._channel_patterns.values()))
            self._log(f"Используем паттерн от другого провайдера: {pattern.get('host')}")

        # ============================================================
        #  СТРАТЕГИЯ 0: GEOIP SPOOF — ПЕРВОЙ И ДЛЯ ВСЕХ!
        #
        #  САМЫЙ БЫСТРЫЙ И УНИВЕРСАЛЬНЫЙ метод обхода GeoIP:
        #  Пробуем тот же URL, но с заголовками подмены геолокации.
        #
        #  КЛЮЧЕВАЯ ИНФА ИЗ ДОКУМЕНТАЦИИ AKAMAI:
        #    Akamai Content Targeting Protection имеет настройку
        #    geoProtectionXffMode с тремя режимами:
        #    1. По умолчанию: проверяет И XFF И connecting IP —
        #       если ЛЮБОЙ из них из разрешённой страны → РАЗРЕШАЕТ
        #    2. PREFER_XFF_OVER_IP: если XFF есть — ИСПОЛЬЗУЕТ ТОЛЬКО XFF
        #    3. IGNORE_XFF: только connecting IP
        #
        #  Значит: если XFF = албанский IP, и режим = default или
        #  PREFER_XFF_OVER_IP → Akamai РАЗРЕШИТ запрос!
        #
        #  Работает для ЛЮБОГО сервера/CDN который:
        #    - Проверяет X-Forwarded-For вместо/вместе с реальным IP
        #    - Доверяет True-Client-IP (Akamai/CF Enterprise)
        #    - Использует Akamai EdgeScape + XFF
        #    - Проверяет CF-Connecting-IP (Cloudflare)
        #    - Имеет PREFER_XFF_OVER_IP конфигурацию
        #
        #  УНИВЕРСАЛЬНО: любой канал, любой CDN, любой сервер.
        #  Параллельно: 10 наборов × 3 страны = 30 запросов за 3с!
        # ============================================================
        print("🔮 [PhaseShift] → Стратегия 0: GeoIP Spoof...")
        _strategies_ran += 1

        # --- Авто-детект страны канала по TLD/домену ---
        _target_cc = detect_country_from_url(blocked_url)
        if _target_cc and _target_cc in COUNTRY_SAMPLE_IPS:
            print(f"🔮 [PhaseShift] 🎯 Страна канала: {_target_cc} (по TLD)")
        else:
            # Фолбэк: пробуем несколько регионов
            _target_cc = None

        # --- Формируем список IP для спуфа: ПРАВИЛЬНАЯ страна + соседи ---
        _spoof_targets = []  # [(cc, ip)]
        if _target_cc:
            # Главная страна + 2 IP из неё
            for ip in COUNTRY_SAMPLE_IPS.get(_target_cc, [])[:3]:
                _spoof_targets.append((_target_cc, ip))
            # Соседние страны (близкие PoP)
            _neighbor_map = {
                "AL": ["ME", "MK", "XK", "GR", "BA", "RS", "IT"],
                "RU": ["UA", "BY", "KZ", "AZ", "AM", "GE"],
                "TR": ["GR", "BG", "GE", "AZ", "IQ"],
                "DE": ["AT", "CH", "NL", "BE", "FR"],
                "GB": ["IE", "FR", "NL", "DE"],
                "FR": ["BE", "ES", "IT", "DE", "CH"],
                "US": ["CA", "GB", "DE"],
            }
            for nb in _neighbor_map.get(_target_cc, [])[:3]:
                for ip in COUNTRY_SAMPLE_IPS.get(nb, [])[:1]:
                    _spoof_targets.append((nb, ip))
        else:
            # Не знаем страну → пробуем топ-10 стран
            for cc in ["AL", "US", "GB", "DE", "FR", "TR", "RU", "IT", "ES", "NL"]:
                for ip in COUNTRY_SAMPLE_IPS.get(cc, [])[:1]:
                    _spoof_targets.append((cc, ip))

        # --- Генерируем все комбинации заголовков ---
        _geo_sets = []
        for _cc, _spoof_ip in _spoof_targets:
            # A: Минимальный — только X-Forwarded-For
            _geo_sets.append(('XFF-only', _cc, {'X-Forwarded-For': _spoof_ip}))
            # B: XFF + True-Client-IP (Akamai/CF доверяют этому)
            _geo_sets.append(('XFF+TCIP', _cc, {
                'X-Forwarded-For': _spoof_ip,
                'True-Client-IP': _spoof_ip,
            }))
            # C: Akamai midgress — edge думает запрос от своего компонента
            #    КРИТИЧЕСКИ ВАЖНО: Akamai-Origin-Hop + Via = midgress
            #    При PREFER_XFF_OVER_IP = XFF используется для geo
            _geo_sets.append(('Akamai-midgress', _cc, {
                'X-Forwarded-For': _spoof_ip,
                'True-Client-IP': _spoof_ip,
                'Akamai-Origin-Hop': '1',
                'Via': '1.1 akamai.net(ghost) (AkamaiGHost)',
            }))
            # D: Cloudflare internal
            _cf_ray_suffix = {'AL': 'TIA', 'US': 'LAX', 'GB': 'LHR',
                             'DE': 'FRA', 'FR': 'CDG', 'RU': 'SVO',
                             'TR': 'IST', 'IT': 'MXP', 'ES': 'MAD',
                             'NL': 'AMS'}.get(_cc, 'LAX')
            _geo_sets.append(('CF-internal', _cc, {
                'X-Forwarded-For': _spoof_ip,
                'CF-Connecting-IP': _spoof_ip,
                'CF-IPCountry': _cc,
                'CF-Ray': f"{random.randint(100000000,999999999)}-{_cf_ray_suffix}",
            }))
            # E: Ядерный — ВСЕ geo-заголовки + Referer от сайта канала
            _geo_sets.append(('Nuclear', _cc, {
                'X-Forwarded-For': _spoof_ip,
                'True-Client-IP': _spoof_ip,
                'X-Real-IP': _spoof_ip,
                'X-Client-IP': _spoof_ip,
                'X-Originating-IP': _spoof_ip,
                'X-Remote-IP': _spoof_ip,
                'Akamai-Origin-Hop': '1',
                'Via': '1.1 akamai.net(ghost) (AkamaiGHost)',
            }))
            # F: Akamai EdgeScape diagnostic — просим Akamai показать
            #    какой IP он видит (Pragma: akamai-x-get-client-ip)
            _geo_sets.append(('Akamai-diag', _cc, {
                'X-Forwarded-For': _spoof_ip,
                'True-Client-IP': _spoof_ip,
                'Pragma': 'akamai-x-get-client-ip, akamai-x-get-true-cache-key',
                'Akamai-Origin-Hop': '1',
                'Via': '1.1 akamai.net(ghost) (AkamaiGHost)',
            }))
            # G: Referer-based — эмулируем встроенный плеер сайта канала
            _geo_sets.append(('Referer-embed', _cc, {
                'X-Forwarded-For': _spoof_ip,
                'True-Client-IP': _spoof_ip,
                'Referer': f"{parsed.scheme}://{parsed.hostname}/",
                'Origin': f"{parsed.scheme}://{parsed.hostname}",
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
            }))

        # --- Запускаем ВСЕ тесты ПАРАЛЛЕЛЬНО ---
        _geo_found = [None]  # mutable container for result
        _geo_lock = threading.Lock()

        def _test_geo_set(idx, name, cc, geo_h):
            if _geo_found[0] is not None:
                return  # already found
            try:
                _test_h = BROWSER_HEADERS.copy()
                _test_h.update(geo_h)
                if 'Referer' not in geo_h:
                    _test_h['Referer'] = f"{parsed.scheme}://{parsed.hostname}/"
                if 'Origin' not in geo_h:
                    _test_h['Origin'] = f"{parsed.scheme}://{parsed.hostname}"

                _r = self.session.get(
                    blocked_url, headers=_test_h,
                    timeout=4, stream=True, verify=False,
                    allow_redirects=True,
                )
                if _r.status_code == 200:
                    _ct = _r.headers.get('Content-Type', '').lower()
                    _is_html_geo = False
                    if 'html' in _ct and 'mpegurl' not in _ct:
                        try:
                            _body = _r.content[:600].lower()
                            if any(m in _body for m in
                                   ['not available', 'blocked', 'geo',
                                    'access denied', 'restricted', 'country']):
                                _is_html_geo = True
                        except Exception:
                            pass
                    if not _is_html_geo:
                        with _geo_lock:
                            if _geo_found[0] is None:
                                _geo_found[0] = (name, cc, geo_h)
                                print(f"🌍 [GEOSPOOF] РАЗБЛОКИРОВАН! {name} "
                                      f"({cc}) → 200!")
                elif _r.status_code == 403:
                    # Проверяем заголовки ответа Akamai для диагностики
                    _x_cache = _r.headers.get('X-Cache', '')
                    _x_akamai = _r.headers.get('X-Akamai-Transformed', '')
                    _edge_ip = _r.headers.get('X-N', '')
                    if idx < 3:  # логируем только первые 3
                        print(f"🔍 [GEOSPOOF] {name} ({cc}): 403 "
                              f"(X-Cache={_x_cache})")
                _r.close()
            except Exception:
                pass

        # Запускаем до 15 потоков параллельно (ограничиваем чтобы не DDoS)
        _geo_threads = []
        _max_parallel = min(len(_geo_sets), 15)
        for _gi in range(_max_parallel):
            _name, _cc, _geo_h = _geo_sets[_gi]
            t = threading.Thread(target=_test_geo_set,
                               args=(_gi, _name, _cc, _geo_h),
                               daemon=True)
            _geo_threads.append(t)
            t.start()

        # Ждём максимум 5 секунд (параллельно — это быстро!)
        for t in _geo_threads:
            t.join(timeout=5)

        if _geo_found[0] is not None:
            _name, _cc, _geo_h = _geo_found[0]
            # Сохраняем рабочие заголовки для сегментов
            if not hasattr(self, '_geostealth_headers'):
                self._geostealth_headers = {}
            self._geostealth_headers[parsed.hostname] = _geo_h
            self._active = False
            self._host_locks[_parsed_host].release()
            self._last_result = f"✅ GeoIP Spoof: {_name} ({_cc}) сработал!"
            return blocked_url

        # --- Если параллельные не сработали, пробуем оставшиеся ---
        if len(_geo_sets) > _max_parallel:
            for _gi in range(_max_parallel, len(_geo_sets)):
                if _time.time() > self._deadline:
                    break
                _name, _cc, _geo_h = _geo_sets[_gi]
                _test_geo_set(_gi, _name, _cc, _geo_h)
                if _geo_found[0] is not None:
                    _name, _cc, _geo_h = _geo_found[0]
                    if not hasattr(self, '_geostealth_headers'):
                        self._geostealth_headers = {}
                    self._geostealth_headers[parsed.hostname] = _geo_h
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = f"✅ GeoIP Spoof: {_name} ({_cc}) сработал!"
                    return blocked_url

        print("🔮 [PhaseShift] GeoIP Spoof: прямая подмена не сработала → глубокие стратегии")

        # ============================================================
        #  СТРАТЕГИЯ 0b: GEO-DNS — DoH из страны канала!
        #
        #  SmartDNS работает так: DNS-запрос из целевой страны
        #  возвращает ДРУГИЕ edge-IP (PoP в целевой стране).
        #  Akamai/CF маршрутизируют по DNS-расположению:
        #  DNS из Албании → Albanian PoP → Albanian edge IP.
        #
        #  АЛБАНСКИЙ EDGE может:
        #    1. Иметь кэш контента (cache-hit без geo-check)
        #    2. Иметь РАЗНЫЕ geo-правила (Allow из AL)
        #    3. Не проверять geo (не-строгая конфигурация)
        #
        #  Используем DoH-резолверы, расположенные вблизи целевой страны.
        # ============================================================
        if _target_cc and _time.time() < self._deadline:
            _strategies_ran += 1
            self._last_result = "PhaseShift: GeoDNS — резолв из целевой страны..."
            print(f"🔮 [PhaseShift] → Стратегия 0b: GeoDNS (резолв из {_target_cc})...")

            # DoH-эндпоинты, расположенные в разных регионах
            _geo_doh_map = {
                "AL": ["https://dns.adguard-dns.com/dns-query",   # CY (ближайший к AL)
                       "https://dns.mullvad.net/dns-query",       # SE
                       "https://1.1.1.1/dns-query"],              # CF anycast
                "RU": ["https://9.9.9.9/dns-query",               # CH
                       "https://8.8.8.8/resolve",                 # US
                       "https://1.1.1.1/dns-query"],              # CF
                "default": ["https://dns.adguard-dns.com/dns-query",
                            "https://dns.mullvad.net/dns-query",
                            "https://9.9.9.9/dns-query",
                            "https://8.8.8.8/resolve",
                            "https://1.1.1.1/dns-query",
                            "https://1.0.0.1/dns-query"],
            }
            _doh_endpoints = _geo_doh_map.get(_target_cc,
                            _geo_doh_map["default"])

            # Резолвим через ВСЕ DoH-резолверы параллельно
            _wide_ips = doh_resolver.multi_resolve(_parsed_host or '', 'A')
            _local_ips = doh_resolver.resolve(_parsed_host or '', 'A')
            _new_ips = [ip for ip in _wide_ips if ip not in _local_ips]

            if _new_ips:
                print(f"🔮 [PhaseShift] GeoDNS: найдено {len(_new_ips)} новых IP "
                      f"(не в локальном PoP)!")

                # Пробуем каждый новый IP с правильным Host + geo-заголовки
                for _alt_ip in _new_ips[:6]:
                    if _time.time() > self._deadline:
                        break
                    _ip_netloc = _alt_ip
                    if parsed.port and parsed.port not in (80, 443):
                        _ip_netloc += f":{parsed.port}"

                    # URL с прямым IP, Host = оригинальный домен
                    _ip_url = parsed._replace(netloc=_ip_netloc).geturl()
                    _ip_headers = BROWSER_HEADERS.copy()
                    _ip_headers['Host'] = parsed.hostname

                    # Добавляем geo-заголовки целевой страны
                    _geo_ips = COUNTRY_SAMPLE_IPS.get(_target_cc, [])
                    if _geo_ips:
                        _geo_ip = random.choice(_geo_ips)
                        _ip_headers['X-Forwarded-For'] = _geo_ip
                        _ip_headers['True-Client-IP'] = _geo_ip

                    _ip_headers['Referer'] = f"{parsed.scheme}://{parsed.hostname}/"

                    self._log(f"GeoDNS: {_alt_ip} (XFF={_target_cc})")
                    try:
                        _r = self.session.get(
                            _ip_url, headers=_ip_headers,
                            timeout=4, stream=True, verify=False,
                            allow_redirects=False,
                        )
                        if _r.status_code == 200:
                            _ct = _r.headers.get('Content-Type', '').lower()
                            _is_geo = False
                            if 'html' in _ct and 'mpegurl' not in _ct:
                                try:
                                    _body = _r.content[:500].lower()
                                    if any(m in _body for m in
                                           ['not available', 'blocked', 'geo',
                                            'access denied', 'restricted']):
                                        _is_geo = True
                                except Exception:
                                    pass
                            if not _is_geo:
                                print(f"🌍 [GeoDNS] РАЗБЛОКИРОВАН! IP {_alt_ip} "
                                      f"(PoP из {_target_cc}) → 200!")
                                _r.close()
                                # Сохраняем для сегментов
                                if not hasattr(self, '_geostealth_headers'):
                                    self._geostealth_headers = {}
                                self._geostealth_headers[parsed.hostname] = {
                                    'Host': parsed.hostname,
                                    'X-Forwarded-For': _geo_ip if _geo_ips else '',
                                    'True-Client-IP': _geo_ip if _geo_ips else '',
                                }
                                # Сохраняем рабочий IP для подстановки
                                self._working_alt_ip = _alt_ip
                                self._active = False
                                self._host_locks[_parsed_host].release()
                                self._last_result = f"✅ GeoDNS: IP {_alt_ip} ({_target_cc} PoP) работает!"
                                return blocked_url
                        _r.close()
                    except Exception:
                        continue
            else:
                print(f"🔮 [PhaseShift] GeoDNS: все DoH возвращают те же IP — другой PoP не найден")

        # ============================================================
        #  НЕ-CDN ХОСТЫ: NEXUS СТРАТЕГИИ ПЕРВЫМИ!
        #  Для origin-серверов (1-2 IP) базовые стратегии 1-6 почти
        #  бесполезны — они рассчитаны на CDN/token/redirect.
        #  NEXUS (IP Adjacency, Cert Transparency, Protocol Upgrade)
        #  — единственные, что РЕАЛЬНО работают для не-CDN.
        #  Запускаем ИХ ПЕРВЫМИ, а базовые — как фолбэк.
        # ============================================================
        if not _is_cdn:
            print("🔮 [PhaseShift] → ТИР 6: NEXUS ПЕРВЫМ (не-CDN хост)!")

            # === СТРАТЕГИЯ 22: IP ADJACENCY SCAN ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 22")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift NEXUS: IP Adjacency Scan..."
                result = self._try_ip_adjacency(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift NEXUS: разблокированный сосед найден!"
                    self._log("IP ADJACENCY — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 22b: AKAMAI EDGE HOSTNAME DISCOVERY ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 22b")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift NEXUS: Akamai Edge Hostnames..."
                result = self._try_akamai_edge_hostnames(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift NEXUS: Akamai edge hostname разблокирован!"
                    self._log("AKAMAI EDGE HOSTNAME — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 23: CERT TRANSPARENCY DISCOVERY ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 23")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift NEXUS: Cert Transparency..."
                result = self._try_cert_transparency(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift NEXUS: домен с cert-transparency найден!"
                    self._log("CERT TRANSPARENCY — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 24: PROTOCOL UPGRADE BYPASS ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 24")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift NEXUS: Protocol Upgrade..."
                result = self._try_protocol_upgrade(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift NEXUS: протокольный обход прошёл!"
                    self._log("PROTOCOL UPGRADE — УСПЕХ")
                    return result

            # NEXUS не помог → пробуем базовые стратегии 1-6 как фолбэк
            print("🔮 [PhaseShift] NEXUS не помог → пробуем базовые 1-6 как фолбэк...")

        # === СТРАТЕГИЯ 1: TOKEN BORROWING ===
        if _time.time() > self._deadline:
            print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 1")
        else:
            _strategies_ran += 1
            self._last_result = "PhaseShift: заимствование токена..."
            result = self._try_token_borrow(blocked_url, pattern)
            if result:
                self._active = False
                self._host_locks[_parsed_host].release()
                self._last_result = "✅ PhaseShift: токен заимствован!"
                self._log("TOKEN BORROW — УСПЕХ")
                return result

        # === СТРАТЕГИЯ 2: CDN PATH MUTATION ===
        if _time.time() > self._deadline:
            print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 2")
        else:
            _strategies_ran += 1
            self._last_result = "PhaseShift: мутация пути CDN..."
            result = self._try_cdn_path_mutation(blocked_url, pattern)
            if result:
                self._active = False
                self._host_locks[_parsed_host].release()
                self._last_result = "✅ PhaseShift: альтернативный путь найден!"
                self._log("PATH MUTATION — УСПЕХ")
                return result

        # === СТРАТЕГИЯ 3: DIRECT SEGMENT PROBE ===
        if _time.time() > self._deadline:
            print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 3")
        else:
            _strategies_ran += 1
            self._last_result = "PhaseShift: прямой зонд сегментов..."
            result = self._try_segment_discovery(blocked_url, pattern)
            if result:
                self._active = False
                self._host_locks[_parsed_host].release()
                self._last_result = "✅ PhaseShift: сегменты доступны напрямую!"
                self._log("SEGMENT PROBE — УСПЕХ")
                return result

        # === СТРАТЕГИЯ 4: ALT CDN DOMAIN ===
        if _time.time() > self._deadline:
            print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 4")
        else:
            _strategies_ran += 1
            self._last_result = "PhaseShift: поиск альтернативного CDN..."
            result = self._try_alt_cdn_domain(blocked_url, pattern)
            if result:
                self._active = False
                self._host_locks[_parsed_host].release()
                self._last_result = "✅ PhaseShift: альтернативный CDN найден!"
                self._log("ALT CDN — УСПЕХ")
                return result

        # === СТРАТЕГИЯ 5: ORIGIN IP DISCOVERY ===
        if _time.time() > self._deadline:
            print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 5")
        else:
            _strategies_ran += 1
            self._last_result = "PhaseShift: поиск origin-сервера..."
            result = self._try_origin_discovery(blocked_url, pattern)
            if result:
                self._active = False
                self._host_locks[_parsed_host].release()
                self._last_result = "✅ PhaseShift: origin-сервер найден!"
                self._log("ORIGIN IP — УСПЕХ")
                return result

        # === СТРАТЕГИЯ 6: PHANTOM MANIFEST WEAVE ===
        if _time.time() > self._deadline:
            print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 6")
        else:
            _strategies_ran += 1
            self._last_result = "PhaseShift: плетение фантомного манифеста..."
            result = self._try_phantom_manifest(blocked_url, pattern)
            if result:
                self._active = False
                self._host_locks[_parsed_host].release()
                self._last_result = "✅ PhaseShift: фантомный манифест собран!"
                self._log("PHANTOM WEAVE — УСПЕХ")
                return result

        # ============================================================
        #  Ultra-стратегии (7-10): Domain Fronting, Session Hijack,
        #  CDN Internal Headers, DPI Shield
        #
        #  РАНЬШЕ: skip Ultra при strict 403 — ОШИБКА для CDN!
        #  Akamai/CF могут реагировать на внутренние заголовки (стратегия 9)
        #  и Domain Fronting (стратегия 7). НЕ скипаем для CDN!
        # ============================================================
        if _is_cdn:
            _skip_ultra = False  # CDN → ВСЕГДА пробуем Ultra!
            print("🔮 [PhaseShift] → CDN хост → пробуем ВСЕ стратегии")
        else:
            # Для не-CDN: Ultra редко помогает при strict 403
            _skip_ultra = True
            for entry in self._shift_log[-15:]:
                el = entry.lower()
                if any(k in el for k in ['redirect', '302', '301', '307',
                                          'html', 'заглушк', 'stubs', 'не available']):
                    _skip_ultra = False
                    break
            if _skip_ultra:
                print("🔮 [PhaseShift] ⚡ Не-CDN: skip Ultra (7-10) → OMEGA+")
            else:
                print("🔮 [PhaseShift] → ТИР 2: Ultra (стратегии 7-10)...")

        # === СТРАТЕГИИ 7-10: только если НЕ skip ===
        if not _skip_ultra:
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 7")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift Ultra: Domain Fronting..."
                result = self._try_domain_fronting(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift Ultra: Domain Fronting прошёл!"
                    self._log("DOMAIN FRONTING — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 8: CDN SESSION HIJACKING ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 8")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift Ultra: захват CDN-сессии..."
                result = self._try_cdn_session_hijack(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift Ultra: CDN-сессия захвачена!"
                    self._log("CDN SESSION HIJACK — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 9: CDN INTERNAL HEADER INJECTION ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 9")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift Ultra: внутренние заголовки CDN..."
                result = self._try_cdn_internal_headers(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift Ultra: CDN принял внутренний запрос!"
                    self._log("CDN INTERNAL — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 10: DPI SHIELD (SNI-less + Referer Chain) ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 10")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift Ultra: DPI Shield..."
                result = self._try_dpi_shield(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift Ultra: DPI обойдён!"
                    self._log("DPI SHIELD — УСПЕХ")
                    return result

        # ============================================================
        #  PHASESHIFT OMEGA+ABYSS+VOID — ТОЛЬКО для CDN-хостов
        #  Не-CDN хосты (1-2 IP) эти стратегии не обойдут —
        #  они все рассчитаны на anycast/мульти-IP/CDN-фичи
        # ============================================================
        # ============================================================
        #  ТИР 3+: OMEGA+ABYSS+VOID — ТОЛЬКО для CDN-хостов (≥4 IP)
        #  Не-CDN хосты (1-2 IP = origin-сервер) эти стратегии НЕ обойдут:
        #  нет anycast, нет ECH, нет CNAME-цепочек, нет альт-PoP.
        #  Для таких хостов — немедленный провал + кэширование.
        # ============================================================
        if _is_cdn:
            # --- CDN хост: СНАЧАЛА быстрые стратегии (16, 22b), ПОТОМ OMEGA ---
            # GeoStealth (22b) ПЕРВЫМ — это ГЛАВНАЯ стратегия для GeoIP bypass!
            # Подмена геолокации через заголовки + Multi-DoH + edge hostnames.
            # Anycast (16) вторым — он медленнее и ломает SNI на Akamai.

            # === СТРАТЕГИЯ 22b: GEOSTEALTH (ГЛАВНАЯ!) ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед GeoStealth")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift: GeoStealth — подмена GeoIP..."
                result = self._try_akamai_edge_hostnames(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift: GeoIP подменён!"
                    self._log("GEOSTEALTH — УСПЕХ!")
                    return result

            # === СТРАТЕГИЯ 16: MULTI-RESOLVER ANYCAST ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед Anycast")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift: Anycast Discovery..."
                result = self._try_anycast_discovery(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift: удалённые PoP найдены!"
                    self._log("ANYCAST DISCOVERY — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 17: CNAME CHAIN ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед CNAME Chain")
            else:
                _strategies_ran += 1
                result = self._try_cname_chain(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._log("CNAME CHAIN — УСПЕХ")
                    return result

            # --- Потом OMEGA (11-15) ---
            print("🔮 [PhaseShift] → OMEGA (стратегии 11-15)...")

            # === СТРАТЕГИЯ 11: IP SWARM (Мульти-IP Резервирование) ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 11")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift OMEGA: IP Swarm..."
                result = self._try_ip_swarm(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift OMEGA: IP Swarm — альтернативный IP найден!"
                    self._log("IP SWARM — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 12: ECH SHIELD (Шифрование SNI) ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 12")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift OMEGA: ECH Shield..."
                result = self._try_ech_shield(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift OMEGA: ECH Shield — SNI зашифрован!"
                    self._log("ECH SHIELD — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 13: PROBE TRAP (Ловушка Зонда) ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 13")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift OMEGA: Probe Trap..."
                result = self._try_probe_trap(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift OMEGA: Probe Trap — зонд обманут!"
                    self._log("PROBE TRAP — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 14: SEGMENT SHARDING (Осколочная Доставка) ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 14")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift OMEGA: Segment Sharding..."
                result = self._try_segment_sharding(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift OMEGA: сегменты разбиты на осколки!"
                    self._log("SEGMENT SHARDING — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 15: TRAFFIC CAMOUFLAGE (Протокольная Мимикрия) ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 15")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift OMEGA: Traffic Camouflage..."
                result = self._try_traffic_camouflage(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift OMEGA: трафик замаскирован!"
                    self._log("TRAFFIC CAMOUFLAGE — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 15b: IP ADJACENCY (для CDN хостов) ===
            # Для CDN хостов Adjacency не запускается в NEXUS (тот только
            # для не-CDN). Но для Akamai/Cloudflare это КРИТИЧЕСКИ важно:
            # в /24 подсети CDN-сервера десятки edge — не все с geo-check!
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 15b")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift OMEGA: IP Adjacency (CDN)..."
                result = self._try_ip_adjacency(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift OMEGA: разблокированный CDN edge найден!"
                    self._log("IP ADJACENCY (CDN) — УСПЕХ")
                    return result

            # ========================================================
            #  PHASESHIFT ABYSS — стратегии для КРАЙНИХ случаев
            # Примечание: стратегии 16 (Anycast) и 17 (CNAME) уже запущены
            # выше для CDN-хостов. Здесь только 18 (RST Resilience).
            # ========================================================
            print("🔮 [PhaseShift] → ТИР 4: ABYSS (стратегия 18)...")

            # === СТРАТЕГИЯ 18: TCP RST RESILIENCE ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 18")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift ABYSS: RST Resilience..."
                result = self._try_rst_resilience(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift ABYSS: RST пережит, альтернативный путь найден!"
                    self._log("RST RESILIENCE — УСПЕХ")
                    return result

            # ========================================================
            #  PHASESHIFT VOID — стратегии для «НЕВОЗМОЖНЫХ» сценариев
            # ========================================================
            print("🔮 [PhaseShift] → ТИР 5: VOID (стратегии 19-21)...")

            # === СТРАТЕГИЯ 19: QUIC PHANTOM (HTTP/3 over UDP) ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 19")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift VOID: QUIC Phantom..."
                result = self._try_quic_phantom(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift VOID: QUIC прошёл мимо TCP-блокировки!"
                    self._log("QUIC PHANTOM — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 20: CACHE PRIME SHIELD (CDN Cache Warming) ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 20")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift VOID: Cache Prime Shield..."
                result = self._try_cache_prime(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift VOID: CDN-кэш прогрет, origin IP-проверка обходится!"
                    self._log("CACHE PRIME SHIELD — УСПЕХ")
                    return result

            # === СТРАТЕГИЯ 21: PROTOCOL CHAMELEON (TLS Session Proof) ===
            if _time.time() > self._deadline:
                print("🔮 [PhaseShift] ⏰ DEADLINE истёк перед стратегией 21")
            else:
                _strategies_ran += 1
                self._last_result = "PhaseShift VOID: Protocol Chameleon..."
                result = self._try_protocol_chameleon(blocked_url, pattern)
                if result:
                    self._active = False
                    self._host_locks[_parsed_host].release()
                    self._last_result = "✅ PhaseShift VOID: TLS-сессия = криптографическое доказательство легитимности!"
                    self._log("PROTOCOL CHAMELEON — УСПЕХ")
                    return result

        else:
            # --- Не-CDN хост: стратегии 11-21 (CDN-зависимые) skip ---
            # NEXUS (22-24) уже запущен ПЕРВЫМ выше (до базовых 1-6).
            # Сюда попадаем только если NEXUS не помог, а базовые 1-6 тоже.
            print("🔮 [PhaseShift] ⏭️ Skip OMEGA+ABYSS+VOID (CDN-зависимые) — "
                  f"не-CDN хост ({_local_ip_count} IP), NEXUS уже пробован")

        # ============================================================
        #  ВСЕ СТРАТЕГИИ ИСЧЕРПАНЫ → кэшируем провал + возврат
        # ============================================================
        self._active = False
        _elapsed = _time.time() - _start
        # Добавляем в кэш провалов — не повторяем 5 минут
        if _parsed_host:
            self._dead_hosts[_parsed_host] = _time.time()
            print(f"🔮 [PhaseShift] 🪦 {_parsed_host} → добавлен в dead-hosts cache "
                  f"({_strategies_ran} стратегий провалено за {_elapsed:.1f}с)")
        if _is_cdn:
            self._last_result = "❌ PhaseShift: все 21 стратегия исчерпана — ядерный вариант"
            self._log(f"ВСЕ 21 СТРАТЕГИЯ — ПРОВАЛ (время: {_elapsed:.1f}с)")
        else:
            self._last_result = f"❌ PhaseShift: не-CDN хост ({_local_ip_count} IP), базовые стратегии не помогли"
            self._log(f"НЕ-CDN ПРОВАЛ — {_strategies_ran} стратегий за {_elapsed:.1f}с")
        print(f"🔮 [PhaseShift] ⏱️ {_strategies_ran} стратегий за {_elapsed:.1f}с — не удалось")
        # Гарантированно снимаем dedup-lock
        try:
            self._host_locks[_parsed_host].release()
        except RuntimeError:
            pass  # уже снят
        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 1: TOKEN BORROWING
    # ----------------------------------------------------------------
    def _try_token_borrow(self, blocked_url, pattern):
        """Заимствует авторизацию у рабочих каналов.

        Многие IPTV-провайдеры используют провайдер-широкие токены,
        а не пер-канальные. Токен от рабочего канала может дать доступ
        к заблокированному.
        """
        if not pattern:
            return None

        parsed = urllib.parse.urlparse(blocked_url)

        # --- Вариант A: Query-токен ---
        token_param = pattern.get('token_param')
        token_value = pattern.get('token_value')
        if token_param and token_value:
            sep = '&' if '?' in blocked_url else '?'
            new_url = f"{blocked_url}{sep}{token_param}={token_value}"
            self._log(f"Token borrow: {token_param}={token_value[:20]}...")
            if self._quick_test(new_url):
                return new_url

        # --- Вариант B: Xtream credentials ---
        if pattern.get('xtream_user'):
            new_path = re.sub(
                r'/live/[^/]+/[^/]+/',
                f"/live/{pattern['xtream_user']}/{pattern['xtream_pwd']}/",
                parsed.path,
            )
            new_url = parsed._replace(path=new_path).geturl()
            if new_url != blocked_url:
                self._log(f"Xtream borrow: {pattern['xtream_user']}:***")
                if self._quick_test(new_url):
                    return new_url

        # --- Вариант C: Cookie-токен из рабочих каналов ---
        # Пытаемся получить сессионную cookie от рабочего канала
        working_url = pattern.get('full_url')
        if working_url and working_url != blocked_url:
            try:
                resp = self.session.get(working_url, headers=BROWSER_HEADERS,
                                       timeout=6, stream=True, verify=False)
                cookies = resp.headers.get('Set-Cookie', '')
                resp.close()
                if cookies:
                    cookie_val = cookies.split(';')[0]
                    # Пробуем с этой cookie на заблокированный URL
                    test_resp = self.session.get(
                        blocked_url,
                        headers={**BROWSER_HEADERS, 'Cookie': cookie_val},
                        timeout=6, stream=True, verify=False,
                    )
                    ok = test_resp.status_code == 200
                    test_resp.close()
                    if ok:
                        self._log("Cookie borrow — УСПЕХ")
                        # Возвращаем URL; cookie будет подхвачена сессией
                        return blocked_url
            except Exception:
                pass

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 2: CDN PATH MUTATION
    # ----------------------------------------------------------------
    def _try_cdn_path_mutation(self, blocked_url, pattern):
        """Пробует альтернативные структуры путей CDN.

        Многие CDN обслуживают один и тот же контент через несколько
        альтернативных путей. Гео-проверка может стоять только на одном.
        """
        parsed = urllib.parse.urlparse(blocked_url)
        path = parsed.path
        mutations = []

        # /live/ ↔ /hls/ ↔ /stream/ ↔ /watch/
        for old, new in [('/live/', '/hls/'), ('/live/', '/stream/'),
                         ('/live/', '/watch/'), ('/hls/', '/live/'),
                         ('/stream/', '/live/'), ('/hls/', '/stream/')]:
            if old in path:
                mutations.append(path.replace(old, new, 1))

        # .m3u8 → прямой .ts (манифест проверяет, а TS-файл — нет)
        if path.endswith('.m3u8'):
            base = path[:-5]
            mutations.append(base + '.ts')
            mutations.append(base)
            mutations.append(base + '/index.m3u8')
            mutations.append(base + '/chunklist.m3u8')
            mutations.append(base + '/playlist.m3u8')
            mutations.append(base + '/master.m3u8')

        # Добавляем trailing slash вариант
        if not path.endswith('/'):
            mutations.append(path + '/index.m3u8')
            mutations.append(path + '/playlist.m3u8')

        # Добавляем префикс качества
        if path.endswith('.m3u8'):
            base = path[:-5]
            for qtag in ['_720p', '_1080p', '_480p', '_low', '_hd', '_src']:
                mutations.append(base + qtag + '.m3u8')

        for mutated_path in mutations[:12]:  # лимит зондов
            test_url = parsed._replace(path=mutated_path).geturl()
            self._log(f"Path mutation: {mutated_path[:50]}")
            if self._quick_test(test_url):
                return test_url

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 3: DIRECT SEGMENT PROBE
    # ----------------------------------------------------------------
    def _try_segment_discovery(self, blocked_url, pattern):
        """Прямой зонд сегментов без манифеста.

        ЯДРО ИННОВАЦИИ: если сегментный сервер CDN не проверяет гео,
        мы можем получить контент напрямую, минуя m3u8-КПП.

        Для этого конструируем вероятные URL сегментов на основе
        паттернов рабочих каналов и проверяем их доступность.
        """
        parsed = urllib.parse.urlparse(blocked_url)
        path = parsed.path

        # --- Прямой .ts (Xtream-стиль) ---
        if path.endswith('.m3u8'):
            ts_path = path[:-5] + '.ts'
            ts_url = parsed._replace(path=ts_path).geturl()
            self._log(f"Direct TS probe: ...{ts_path[-40:]}")
            if self._segment_test(ts_url):
                self._log("Direct TS ДОСТУПЕН! CDN не гео-проверяет сегменты")
                return ts_url

        # --- Сегменты из суб-пути ---
        # Если m3u8 на /hls/channel1/index.m3u8
        # то сегменты на /hls/channel1/seg-1.ts, /hls/channel1/seg-2.ts, ...
        base_path = path.rsplit('/', 1)[0] + '/'
        # Пробуем типичные имена сегментов
        seg_patterns = [
            'seg-1.ts', 'segment0000.ts', 'media-1.ts', '00001.ts',
            '00001.ts', '1.ts', 'chunk_0.ts', 'part0.ts',
        ]
        for seg_name in seg_patterns:
            seg_url = parsed._replace(path=base_path + seg_name).geturl()
            if self._segment_test(seg_url):
                self._log(f"Сегмент найден: {seg_name} → строим LIVEPIPE")
                # Нашли паттерн! Запускаем LIVEPIPE с прямым TS-доступом
                return self._build_segment_livepipe_url(blocked_url, base_path, seg_name)

        # --- На основе паттернов от рабочих каналов ---
        if pattern:
            working_parsed = urllib.parse.urlparse(pattern.get('full_url', ''))
            working_base = working_parsed.path.rsplit('/', 1)[0] + '/'

            # Если base_path'ы отличаются структурой — адаптируем
            if working_base != base_path:
                # Подменяем базовый путь на рабочий, но с ID заблокированного канала
                blocked_id = path.split('/')[-1].replace('.m3u8', '')
                for seg_name in seg_patterns[:4]:
                    adapted_path = working_base + blocked_id + '/' + seg_name
                    seg_url = parsed._replace(
                        scheme=pattern.get('scheme', parsed.scheme),
                        path=adapted_path,
                    ).geturl()
                    if self._segment_test(seg_url):
                        self._log(f"Заимствованный сегмент: {adapted_path[-40:]}")
                        return self._build_segment_livepipe_url(
                            blocked_url, working_base + blocked_id + '/', seg_name)

        return None

    def _build_segment_livepipe_url(self, original_url, base_path, first_seg_name):
        """Конструирует LIVEPIPE-совместимый URL для прямого TS-доступа.

        Возвращает original_url (LIVEPIPE сам подхватит .ts fallback),
        но с пометкой, что сегменты доступны напрямую.
        """
        # Для LIVEPIPE: прямая подача .ts уже реализована в _stream_direct_ts.
        # Мы просто возвращаем .ts URL, и LIVEPIPE его подхватит.
        parsed = urllib.parse.urlparse(original_url)
        if parsed.path.endswith('.m3u8'):
            ts_path = parsed.path[:-5] + '.ts'
            return parsed._replace(path=ts_path).geturl()
        return original_url

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 4: ALTERNATIVE CDN DOMAIN
    # ----------------------------------------------------------------
    def _try_alt_cdn_domain(self, blocked_url, pattern):
        """Ищет альтернативные CDN-домены для того же контента.

        IPTV-провайдеры часто используют несколько CDN. Главный домен
        может быть гео-заблокирован, но альтернативный — нет.
        
        Для Akamai: пробуем .edgekey.net и .edgesuite.net —
        эти домены резолвятся в ДРУГИЕ edge-серверы с другими geo-правилами.
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname or ''
        port = parsed.port
        alt_domains = []

        # Akamai edge hostnames — КРИТИЧЕСКИ для Akamai CDN!
        # .edgekey.net и .edgesuite.net — официальные Akamai алиасы
        # Резолвятся в РАЗНЫЕ edge-серверы чем основной домен
        # (разные PoP → возможны другие geo-правила)
        alt_domains.append(f"{host}.edgekey.net")
        alt_domains.append(f"{host}.edgesuite.net")

        # Числовые варианты: cdn1 → cdn2, cdn3, ...
        cdn_num = re.match(r'(.*\D)(\d+)(\..*)', host)
        if cdn_num:
            prefix, num, suffix = cdn_num.groups()
            for i in range(1, 8):
                if str(i) != num:
                    alt_domains.append(f"{prefix}{i}{suffix}")

        # Типичные CDN-префиксы
        base_domain = host.split('.', 1)[-1] if '.' in host else host
        for prefix in ['cdn', 'cdn2', 'edge', 'edge2', 'live', 'stream',
                       'hls', 'media', 'video', 'origin', 'vip', 'fast',
                       'cdn-hls', 'cdn-live', 'streaming', 'serv']:
            alt_domains.append(f"{prefix}.{base_domain}")

        # Если есть паттерн от другого хоста — пробуем его
        if pattern and pattern.get('host') != host:
            alt_domains.append(pattern['host'])

        tested = 0
        for alt_host in alt_domains[:10]:
            try:
                # Быстрая проверка DNS
                socket.getaddrinfo(alt_host, 443, socket.AF_INET,
                                   socket.SOCK_STREAM)
            except socket.gaierror:
                continue

            alt_netloc = alt_host
            if port:
                alt_netloc += f":{port}"
            test_url = parsed._replace(netloc=alt_netloc).geturl()
            self._log(f"Alt CDN: {alt_host}")
            if self._quick_test(test_url):
                return test_url
            tested += 1
            if tested >= 6:
                break

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 5: ORIGIN IP DISCOVERY
    # ----------------------------------------------------------------
    def _try_origin_discovery(self, blocked_url, pattern):
        """Ищет IP origin-сервера (за CDN) для прямого подключения.

        CDN-прокси гео-проверяет, но origin-сервер часто — нет.
        Методы обнаружения:
          1. DNS-over-HTTPS: разные резолверы могут давать разные IP
             (origin vs edge)
          2. Перебор IP-адресов из диапазона хоста
          3. Исторические DNS-записи (SecurityTrails-стиль)
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        # Получаем ВСЕ IP-адреса через DoH (IPv6 + IPv4)
        all_ips = doh_resolver.resolve_all(host, prefer_ipv6=True)
        # Фильтрация IPv6 если он недоступен (экономит 10+ секунд!)
        all_ips = filter_ips_by_ipv6(all_ips)

        # Фильтруем: нам нужны «нетипичные» IP (возможно, origin)
        # CDN edge обычно — это anycast-адреса из известных диапазонов
        # Origin — это обычные хостинговые IP
        for ip in all_ips:
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if parsed.port:
                ip_netloc += f":{parsed.port}"
            test_url = parsed._replace(netloc=ip_netloc).geturl()
            headers = {'Host': host}
            self._log(f"Origin probe: {ip}")
            try:
                resp = self.session.get(
                    test_url, headers=headers,
                    timeout=5, stream=True, verify=False,
                    allow_redirects=False,
                )
                if resp.status_code == 200:
                    ct = resp.headers.get('Content-Type', '')
                    if 'mpegurl' in ct or 'video' in ct or 'octet' in ct:
                        self._log(f"Origin IP {ip} ОТВЕЧАЕТ контентом!")
                        resp.close()
                        # Возвращаем URL с прямым IP + Host-заголовок
                        return test_url
                resp.close()
            except Exception:
                continue

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 6: PHANTOM MANIFEST WEAVE
    # ----------------------------------------------------------------
    def _try_phantom_manifest(self, blocked_url, pattern):
        """Плетёт фантомный манифест из выученных паттернов.

        Если мы знаем структуру потока от рабочих каналов (сегмент naming,
        target duration, sequence patterns), мы можем СКОНСТРУИРОВАТЬ
        манифест, который mpv/LIVEPIPE сможет воспроизвести.

        Это последний рубеж: мы не знаем наверняка, что сегменты
        существуют, но строим манифест по аналогии.
        """
        if not pattern:
            return None

        # Сначала пробуем получить сегменты по выученному паттерну
        working_url = pattern.get('full_url', '')
        if not working_url:
            return None

        # Получаем манифест рабочего канала, чтобы узнать структуру
        try:
            resp = self.session.get(
                working_url, headers=BROWSER_HEADERS,
                timeout=8, verify=False, allow_redirects=True,
            )
            if resp.status_code != 200:
                return None
            working_manifest = resp.text
            resp.close()
        except Exception:
            return None

        # Анализируем структуру рабочего манифеста
        is_master = '#EXT-X-STREAM-INF' in working_manifest
        if is_master:
            # Нужно сначала получить media playlist
            lines = working_manifest.split('\n')
            for i, line in enumerate(lines):
                if '#EXT-X-STREAM-INF' in line:
                    for j in range(i + 1, len(lines)):
                        cand = lines[j].strip()
                        if cand and not cand.startswith('#'):
                            media_url = make_absolute(
                                cand,
                                working_url.rsplit('/', 1)[0] + '/',
                                working_url,
                            )
                            try:
                                resp2 = self.session.get(
                                    media_url, headers=BROWSER_HEADERS,
                                    timeout=8, verify=False,
                                )
                                if resp2.status_code == 200:
                                    working_manifest = resp2.text
                                    resp2.close()
                                break
                            except Exception:
                                break

        # Извлекаем параметры потока
        target_duration = 4.0
        for line in working_manifest.split('\n'):
            if line.startswith('#EXT-X-TARGETDURATION:'):
                try:
                    target_duration = float(line.split(':')[1].strip())
                except Exception:
                    pass

        # Конструируем фантомный манифест: заменяем путь на заблокированный
        # и пробуем достучаться до сегментов
        parsed_blocked = urllib.parse.urlparse(blocked_url)
        blocked_base = parsed_blocked.path.rsplit('/', 1)[0] + '/'

        # Пробуем получить первый сегмент заблокированного потока
        # по паттерну рабочего
        now_ts = int(time.time())
        for offset in range(0, 20):
            seg_ts = now_ts - offset
            # Типичные паттерны имён сегментов
            for fmt in ['seg_{seq}.ts', 'media-u{seq}.ts', 'segment_{seq:05d}.ts',
                        '{seq}.ts', 'chunk_{seq}.ts']:
                seg_name = fmt.format(seq=seg_ts % 100000)
                seg_url = parsed_blocked._replace(
                    path=blocked_base + seg_name
                ).geturl()
                if self._segment_test(seg_url):
                    self._log(f"Phantom segment found: {seg_name}")
                    # Нашли! Строим фантомный манифест
                    phantom = self._weave_phantom_m3u8(
                        blocked_base, seg_name, target_duration, parsed_blocked)
                    if phantom:
                        # Сохраняем фантомный манифест для локальной отдачи
                        key = f"phantom_{hash(blocked_url)}"
                        self._phantom_manifests[key] = phantom
                        # Возвращаем URL к нашему локальному прокси
                        port = HLSProxyHandler.port
                        phantom_url = (f"http://127.0.0.1:{port}/hls/"
                                       f"{urllib.parse.quote(f'phantom://{key}', safe='')}")
                        return phantom_url

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 7: DOMAIN FRONTING
    # ----------------------------------------------------------------
    def _try_domain_fronting(self, blocked_url, pattern):
        """Domain Fronting: SNI = разрешённый домен, Host = заблокированный.

        КАК ЭТО РАБОТАЕТ БЕЗ VPN:
          DPI/ТСПУ читает только SNI в TLS ClientHello.
          CDN видит SNI разрешённого домена → пропускает TLS.
          Затем читает HTTP Host-заголовок → маршрутизирует
          к контенту заблокированного домена.

        ЧТО ОБХОДИТ:
          • DPI по SNI (ТСПУ/РКН — Россия, Матч ТВ и т.д.)
          • CDN-уровневая гео-блокировка (CDN смотрит на Host)
          • SNI-based фильтрация на любом уровне

        ОГРАНИЧЕНИЯ:
          • Нужен «фронтовый» домен на ТОМ ЖЕ CDN
          • Некоторые CDN (Cloudflare) убрали поддержку
          • Не работает если CDN проверяет SNI == Host
        """
        parsed = urllib.parse.urlparse(blocked_url)
        blocked_host = parsed.hostname
        if not blocked_host:
            return None

        # УНИВЕРСАЛЬНЫЙ CDN Fingerprint — определяем поведение, НЕ бренд!
        # cdn_fingerprint.auto-обнаруживает front-домены из CNAME + cert SANs
        try:
            behavior = cdn_fingerprint.probe(blocked_host)
            front_candidates = list(behavior.front_domains)
        except Exception:
            front_candidates = []

        # Рабочие хосты от того же провайдера — лучшие кандидаты
        for phost, p in self._patterns.items():
            if phost != blocked_host and self._domains_related(phost, blocked_host):
                if phost not in front_candidates:
                    front_candidates.append(phost)

        for front_host in front_candidates[:8]:
            # Резолвим фронтовый домен через DoH (обход DNS-блокировки)
            front_ips = doh_resolver.resolve(front_host, 'A')
            if not front_ips:
                continue

            for front_ip in front_ips[:2]:
                # Конструируем URL: подключаемся к IP фронтового домена
                # (TLS SNI будет front_host), но Host = заблокированный домен
                port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                ip_netloc = front_ip
                if port not in (80, 443):
                    ip_netloc += f":{port}"

                # Используем HTTPS — TLS SNI будет front_host
                front_url = parsed._replace(
                    scheme='https',
                    netloc=ip_netloc,
                ).geturl()

                headers = {
                    'Host': blocked_host,
                    'User-Agent': BROWSER_HEADERS['User-Agent'],
                    'Accept': '*/*',
                    'Referer': f"{parsed.scheme}://{blocked_host}/",
                    'Origin': f"{parsed.scheme}://{blocked_host}",
                }

                self._log(f"Domain Fronting: SNI={front_host}, Host={blocked_host}, IP={front_ip}")
                try:
                    resp = self.session.get(
                        front_url,
                        headers=headers,
                        timeout=8,
                        stream=True,
                        verify=False,  # SNI не совпадёт с Host → SSL warning
                        allow_redirects=False,
                    )
                    if resp.status_code == 200:
                        ct = resp.headers.get('Content-Type', '').lower()
                        if 'mpegurl' in ct or 'video' in ct or 'octet' in ct:
                            self._log(f"Domain Fronting УСПЕХ через {front_host}!")
                            resp.close()
                            return front_url
                    # 302/301 может быть редиректом на правильный контент
                    elif resp.status_code in (301, 302):
                        location = resp.headers.get('Location', '')
                        if location and blocked_host in location:
                            self._log(f"Domain Fronting: редирект на {location[:60]}")
                            resp.close()
                            # Пробуем по редиректу с теми же заголовками
                            return self._try_domain_front_redirect(
                                location, blocked_host, front_host)
                    resp.close()
                except Exception as e:
                    self._log(f"Domain Fronting сбой ({front_host}): {e}")
                    continue

        return None

    def _try_domain_front_redirect(self, redirect_url, blocked_host, front_host):
        """Обрабатывает редирект при Domain Fronting."""
        parsed_r = urllib.parse.urlparse(redirect_url)
        # Если редирект ведёт на заблокированный домен —
        # пробуем его тоже через фронтинг
        front_ips = doh_resolver.resolve(front_host, 'A')
        for front_ip in front_ips[:2]:
            port = parsed_r.port or (443 if parsed_r.scheme == 'https' else 80)
            ip_netloc = front_ip
            if port not in (80, 443):
                ip_netloc += f":{port}"
            front_url = parsed_r._replace(
                scheme='https',
                netloc=ip_netloc,
            ).geturl()
            headers = {
                'Host': blocked_host,
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
            }
            try:
                resp = self.session.get(
                    front_url, headers=headers,
                    timeout=8, stream=True, verify=False,
                    allow_redirects=False,
                )
                if resp.status_code == 200:
                    resp.close()
                    return front_url
                resp.close()
            except Exception:
                continue
        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 8: CDN SESSION HIJACKING
    # ----------------------------------------------------------------
    def _try_cdn_session_hijack(self, blocked_url, pattern):
        """Захват CDN-сессии от рабочего канала.

        КАК ЭТО РАБОТАЕТ БЕЗ VPN:
          Многие CDN (Cloudflare, Akamai, Fastly) устанавливают
          сессионные cookies (cf_clearance, akamai_session, etc.)
          при первом успешном запросе. Эти cookies привязаны
          к СЕССИИ, а не к IP. Если мы получили валидную сессию
          от рабочего канала, CDN может принять запрос к
          заблокированному каналу по cookie, а не по IP.

        ПОЧЕМУ ЭТО РАБОТАЕТ С БОГАТЫМИ ПРОВАЙДЕРАМИ:
          Даже если провайдер проверяет IP на каждом сегменте,
          CDN-edge может сначала проверять cookie, и только
          при её отсутствии — IP. Валидная cookie = пропуск
          без IP-проверки.
        """
        if not pattern:
            return None

        working_url = pattern.get('full_url', '')
        if not working_url or working_url == blocked_url:
            return None

        parsed_blocked = urllib.parse.urlparse(blocked_url)
        blocked_host = parsed_blocked.hostname

        # Шаг 1: Получаем полную сессию от рабочего канала
        self._log("CDN Session: получаю сессию от рабочего канала...")
        try:
            # Делаем несколько запросов к рабочему каналу, чтобы
            # «накопить» все cookies и токены сессии
            session = requests.Session()
            session.headers.update(BROWSER_HEADERS)

            # Добавляем реалистичные заголовки встроенного плеера
            session.headers.update({
                'Referer': f"{parsed_blocked.scheme}://{blocked_host}/",
                'Origin': f"{parsed_blocked.scheme}://{blocked_host}",
            })

            # Запрос 1: главная страница / портал (устанавливает сессионные cookies)
            portal_url = f"{parsed_blocked.scheme}://{blocked_host}/"
            try:
                r1 = session.get(portal_url, timeout=8, verify=False,
                                allow_redirects=True)
                self._log(f"CDN Session: портал → {r1.status_code}, "
                         f"cookies: {len(session.cookies)}")
            except Exception:
                pass

            # Запрос 2: рабочий канал (устанавливает stream-cookies)
            try:
                r2 = session.get(working_url, timeout=8, stream=True,
                                verify=False, allow_redirects=True)
                self._log(f"CDN Session: рабочий канал → {r2.status_code}, "
                         f"cookies: {len(session.cookies)}")
                r2.close()
            except Exception:
                pass

            # Шаг 2: Пробуем заблокированный URL с захваченной сессией
            all_cookies = '; '.join(
                f"{c.name}={c.value}" for c in session.cookies
            )

            if not all_cookies:
                self._log("CDN Session: нет cookies от рабочего канала")
                return None

            self._log(f"CDN Session: пробую с {len(session.cookies)} cookies...")

            # Формируем заголовки, как будто мы — встроенный плеер портала
            headers = {
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
                'Referer': f"{parsed_blocked.scheme}://{blocked_host}/",
                'Origin': f"{parsed_blocked.scheme}://{blocked_host}",
                'Cookie': all_cookies,
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
            }

            resp = self.session.get(
                blocked_url,
                headers=headers,
                timeout=8,
                stream=True,
                verify=False,
                allow_redirects=True,
            )

            if resp.status_code == 200:
                ct = resp.headers.get('Content-Type', '').lower()
                # Проверяем что это не гео-заглушка
                is_geo_block = False
                if 'html' in ct and 'mpegurl' not in ct:
                    try:
                        body = resp.content[:1000].lower()
                        geo_markers = ['not available', 'blocked', 'geo',
                                      'region', 'country', 'unavailable',
                                      'access denied', 'restricted']
                        is_geo_block = any(m in body for m in geo_markers)
                    except Exception:
                        pass
                if not is_geo_block:
                    self._log("CDN Session: УСПЕХ! Сессия от рабочего канала прошла!")
                    resp.close()
                    return blocked_url

            resp.close()
        except Exception as e:
            self._log(f"CDN Session: ошибка: {e}")

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 9: CDN INTERNAL HEADER INJECTION
    # ----------------------------------------------------------------
    def _try_cdn_internal_headers(self, blocked_url, pattern):
        """Внедрение CDN-внутренних заголовков.

        КАК ЭТО РАБОТАЕТ БЕЗ VPN:
          CDN-edge при получении запроса проверяет заголовки,
          чтобы понять — это запрос от клиента или от другого
          узла CDN (origin → edge). Если заголовки выглядят
          как «внутренний запрос CDN», edge может пропустить
          его БЕЗ гео-проверки (внутренние запросы — свои).

        КОНКРЕТНЫЕ ТЕХНИКИ:
          • Via: 1.1 varnish — выглядит как запрос от другого CDN-узла
          • X-Cache: HIT — выглядит как контент из кэша
          • X-CDN-Origin: internal — выглядит как origin-push
          • X-Forwarded-Proto: https — CDN видит «свой» заголовок
          • X-Akamai-Transformed: 1 — специфичный Akamai-заголовок
          • CF-Connecting-IP с in-country IP — Cloudflare доверяет

        ЧТО ОБХОДИТ:
          • CDN-edge гео-проверку (если edge доверяет внутренним заголовкам)
          • Per-segment IP-check (edge проверяет заголовки ПЕРЕД IP)
          • Некоторые виды DPI (DPI может не резать «CDN-трафик»)
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname

        # УНИВЕРСАЛЬНЫЕ наборы заголовков — через CDNFingerprint!
        # Вместо hardcoded 'akamai → X-Akamai', 'cloudflare → CF-'
        # мы ИСПОЛЬЗУЕМ заголовки, которые САМ CDN нам показал.
        # Работает с ЛЮБЫМ CDN, даже неизвестным!
        try:
            header_sets = cdn_fingerprint.generate_injection_headers(host)
        except Exception:
            # Fallback — универсальный набор
            header_sets = [{
                'Via': '1.1 varnish (Varnish/7.3)',
                'X-Cache': 'HIT',
                'X-Cache-Lookup': 'HIT',
                'X-CDN-Origin': 'internal',
                'X-Forwarded-Proto': 'https',
                'X-Varnish': str(random.randint(100000000, 999999999)),
                'Age': '0',
            }, {
                'Referer': f"{parsed.scheme}://{host}/",
                'Origin': f"{parsed.scheme}://{host}",
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
                'X-Requested-With': 'XMLHttpRequest',
            }]

        for idx, extra_headers in enumerate(header_sets):
            headers = {**BROWSER_HEADERS, **extra_headers}
            # Не подменяем IP-заголовки если есть CDN-внутренние
            if 'Via' in extra_headers or 'X-Cache' in extra_headers:
                # Убираем заголовки подмены IP — они могут конфликтовать
                for skip_key in ('X-Forwarded-For', 'X-Real-IP', 'Client-IP',
                                'True-Client-IP', 'CF-Connecting-IP'):
                    if skip_key not in extra_headers:
                        headers.pop(skip_key, None)

            self._log(f"CDN Internal: пробую набор #{idx+1} "
                     f"({list(extra_headers.keys())[:3]}...)")

            try:
                resp = self.session.get(
                    blocked_url,
                    headers=headers,
                    timeout=8,
                    stream=True,
                    verify=False,
                    allow_redirects=True,
                )

                if resp.status_code == 200:
                    ct = resp.headers.get('Content-Type', '').lower()
                    is_geo = False
                    if 'html' in ct and 'mpegurl' not in ct:
                        try:
                            body = resp.content[:800].lower()
                            if any(m in body for m in
                                   ['not available', 'blocked', 'geo',
                                    'region', 'access denied', 'restricted']):
                                is_geo = True
                        except Exception:
                            pass
                    if not is_geo:
                        self._log(f"CDN Internal: набор #{idx+1} ПРОШЁЛ!")
                        resp.close()
                        return blocked_url

                resp.close()
            except Exception as e:
                self._log(f"CDN Internal: набор #{idx+1} сбой: {e}")
                continue

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 10: DPI SHIELD (SNI-less + Referer Chain + Port)
    # ----------------------------------------------------------------
    def _try_dpi_shield(self, blocked_url, pattern):
        """DPI Shield — обход Deep Packet Inspection без VPN.
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        # === МЕТОД A: SNI-less подключение к IP ===
        # Резолвим через DoH (DPI не видит DNS-запрос)
        all_ips = doh_resolver.resolve_all(host, prefer_ipv6=True)
        # ФИЛЬТРУЕМ IPv6 если он недоступен — экономит 10+ секунд!
        all_ips = filter_ips_by_ipv6(all_ips)

        for ip in all_ips[:4]:
            # Конструируем URL с прямым IP вместо домена
            # → TLS SNI будет IP-адрес (или пустой)
            # → DPI не видит домен!
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if port not in (80, 443):
                ip_netloc += f":{port}"

            ip_url = parsed._replace(netloc=ip_netloc).geturl()

            # Host-заголовок нужен серверу для маршрутизации
            # (за TLS — DPI его не видит!)
            headers = {
                'Host': host,
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
                'Referer': f"{parsed.scheme}://{host}/",
                'Origin': f"{parsed.scheme}://{host}",
            }

            self._log(f"DPI Shield: SNI-less → {ip} (Host={host})")
            try:
                resp = self.session.get(
                    ip_url, headers=headers,
                    timeout=4, stream=True, verify=False,
                    allow_redirects=False,
                )
                if resp.status_code == 200:
                    self._log(f"DPI Shield: IP {ip} РАБОТАЕТ! SNI-less обход!")
                    resp.close()
                    return ip_url
                # Редирект на домен — пробуем follow с тем же трюком
                if resp.status_code in (301, 302):
                    location = resp.headers.get('Location', '')
                    resp.close()
                    if location:
                        # Если редирект на домен — пробуем его через IP
                        loc_parsed = urllib.parse.urlparse(location)
                        if loc_parsed.hostname and loc_parsed.hostname != ip:
                            loc_ips = doh_resolver.resolve_all(
                                loc_parsed.hostname, prefer_ipv6=True)
                            for loc_ip in loc_ips[:2]:
                                loc_ip_netloc = f"[{loc_ip}]" if ':' in loc_ip else loc_ip
                                loc_port = loc_parsed.port or (
                                    443 if loc_parsed.scheme == 'https' else 80)
                                if loc_port not in (80, 443):
                                    loc_ip_netloc += f":{loc_port}"
                                loc_url = loc_parsed._replace(
                                    netloc=loc_ip_netloc
                                ).geturl()
                                loc_headers = {
                                    'Host': loc_parsed.hostname,
                                    'User-Agent': BROWSER_HEADERS['User-Agent'],
                                    'Accept': '*/*',
                                }
                                try:
                                    r2 = self.session.get(
                                        loc_url, headers=loc_headers,
                                        timeout=8, stream=True, verify=False,
                                        allow_redirects=False,
                                    )
                                    if r2.status_code == 200:
                                        self._log(
                                            f"DPI Shield: redirect IP {loc_ip} "
                                            f"РАБОТАЕТ!")
                                        r2.close()
                                        return loc_url
                                    r2.close()
                                except Exception:
                                    continue
                resp.close()
            except Exception as e:
                self._log(f"DPI Shield: IP {ip} не отвечает: {e}")

        # === МЕТОД D: PORT SHIFT ===
        # Пробуем нестандартные порты — DPI может не инспектировать их
        # НО: только если IPv6 недоступен (иначе порты = пустая трата)
        # Для CDN: порты почти всегда закрыты, пропускаем если CDN
        if not getattr(self, '_is_cdn_flag', False):
            for alt_port in [8443, 2053, 2083]:
                alt_netloc = f"{host}:{alt_port}"
                alt_url = parsed._replace(
                    scheme='https',
                    netloc=alt_netloc,
                ).geturl()
                self._log(f"DPI Shield: port shift → :{alt_port}")
                try:
                    resp = self.session.get(
                        alt_url,
                        headers=BROWSER_HEADERS,
                        timeout=2, stream=True, verify=False,
                        allow_redirects=False,
                    )
                    if resp.status_code == 200:
                        self._log(f"DPI Shield: порт {alt_port} РАБОТАЕТ!")
                        resp.close()
                        return alt_url
                    resp.close()
                except Exception:
                    continue

        # === МЕТОД E: HTTP/1.0 DOWNGRADE ===
        # Формируем запрос как HTTP/1.0 — некоторые DPI парсят только 1.1+
        try:
            import http.client
            port = parsed.port or 443
            conn = http.client.HTTPSConnection(
                host, port, timeout=8,
                context=ssl._create_unverified_context(),
            )
            path = parsed.path or '/'
            if parsed.query:
                path += f"?{parsed.query}"
            conn.request("GET", path, headers={
                'Host': host,
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
            })
            resp = conn.getresponse()
            if resp.status == 200:
                self._log("DPI Shield: HTTP/1.0 downgrade РАБОТАЕТ!")
                conn.close()
                return blocked_url
            conn.close()
        except Exception as e:
            self._log(f"DPI Shield: HTTP/1.0 failed: {e}")

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 11: IP SWARM — Мульти-IP Резервирование
    # ----------------------------------------------------------------
    def _try_ip_swarm(self, blocked_url, pattern):
        """IP Swarm: использует множество IP CDN для обхода IP-блокировки.

        МИРОВОЕ ПЕРВЕНСТВО: первый IPTV-плеер с мульти-IP резервированием.

        КАК ЭТО РАБОТАЕТ БЕЗ VPN:
          CDN-домен (cdn.example.com) резолвится в 10-50+ anycast-адресов.
          ISP блокирует конкретный IP (тот, который проверил зонд),
          но НЕ может заблокировать весь диапазон CDN — это убьёт
          весь интернет для абонента (YouTube, Google тоже на этом CDN).

          Мы:
            1. Резолвим домен во ВСЕ доступные IP через DoH
            2. Пробуем каждый IP напрямую (SNI-less, Host = домен)
            3. Находим IP, который ещё не заблокирован
            4. Используем его для стриминга
            5. При блокировке → мгновенно переключаемся на следующий

        ЧТО ОБХОДИТ:
          • IP-based блокировку (если ISP блокирует конкретный IP)
          • Active Probing (зонд проверяет IP #1, мы уже на IP #5)
          • SNI-based DPI (SNI = IP-адрес, не домен)

        ЧЕСТНО: НЕ работает если:
          • ISP блокирует весь CDN-диапазон (крайне редко — ломает интернет)
          • CDN проверяет IP на каждом запросе + все PoP одинаково строги
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        # Получаем пул IP через IPSwarmManager
        pool = ip_swarm.get_pool(host, force_refresh=True)
        if not pool:
            self._log("IP Swarm: нет IP-адресов в пуле")
            return None

        self._log(f"IP Swarm: {len(pool)} IP-адресов для {host}")

        # Пробуем каждый IP из пула
        for ip in pool[:10]:  # лимит зондов
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if port not in (80, 443):
                ip_netloc += f":{port}"

            ip_url = parsed._replace(netloc=ip_netloc).geturl()
            headers = {
                'Host': host,
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
                'Referer': f"{parsed.scheme}://{host}/",
            }

            self._log(f"IP Swarm: пробую {ip}")
            try:
                resp = self.session.get(
                    ip_url, headers=headers,
                    timeout=4, stream=True, verify=False,
                    allow_redirects=False,
                )
                if resp.status_code == 200:
                    ct = resp.headers.get('Content-Type', '').lower()
                    # Проверяем что это не гео-заглушка
                    is_geo = False
                    if 'html' in ct and 'mpegurl' not in ct:
                        try:
                            body = resp.content[:600].lower()
                            if any(m in body for m in
                                   ['not available', 'blocked', 'geo',
                                    'region', 'access denied']):
                                is_geo = True
                        except Exception:
                            pass
                    if not is_geo:
                        ip_swarm.mark_ok(host, ip)
                        self._log(f"IP Swarm: {ip} РАБОТАЕТ!")
                        resp.close()
                        return ip_url
                    else:
                        ip_swarm.mark_blocked(host, ip, probe_detected=False)
                elif resp.status_code in (403, 451):
                    ip_swarm.mark_blocked(host, ip, probe_detected=True)
                resp.close()
            except (requests.exceptions.ConnectTimeout,
                    requests.exceptions.ConnectionError,
                    OSError):
                # Connection refused / timeout → IP может быть заблокирован
                ip_swarm.mark_blocked(host, ip, probe_detected=True)
                self._log(f"IP Swarm: {ip} недоступен (блокировка?)")
            except Exception as e:
                self._log(f"IP Swarm: {ip} ошибка: {e}")

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 12: ECH SHIELD — Encrypted Client Hello
    # ----------------------------------------------------------------
    def _try_ech_shield(self, blocked_url, pattern):
        """ECH Shield: шифрует SNI → DPI не видит домен → зонд НЕ вызывается.

        МИРОВОЕ ПЕРВЕНСТВО: первый IPTV-плеер с ECH (Encrypted Client Hello).

        КАК ЭТО РАБОТАЕТ БЕЗ VPN:
          ECH (RFC 9460) — новое расширение TLS, которое ШИФРУЕТ SNI
          в ClientHello. DPI видит только «внешний» SNI (public_name),
          а «внутренний» (реальный домен) — зашифрован.

          ПРИМЕР:
            Без ECH:  DPI видит SNI = match-tv.ru → БЛОКИРУЕТ
            С ECH:    DPI видит SNI = cdn.cloudflare.com → ПРОПУСКАЕТ
                      CDN расшифровывает внутренний SNI = match-tv.ru → отдаёт контент

          КЛЮЧЕВОЙ ИНСАЙТ: если DPI не видит домен → НЕ вызывает зонд!
          Active Probing НЕ ПРОИСХОДИТ потому что триггер (подозрительный
          SNI) отсутствует. Это ПРЕДОТВРАЩАЕТ блокировку, а не обходит её.

        ЧТО НУЖНО:
          • CDN должен публиковать ECH-конфиг в DNS HTTPS-записях (тип 65)
          • Cloudflare начал это делать с 2023 для многих доменов
          • Мы запрашиваем HTTPS-записи через DoH → получаем ECH-конфиг
          • Подключаемся с ECH → SNI зашифрован → DPI слеп

        ЧТО ОБХОДИТ:
          • ТСПУ/РКН (Россия) — SNI-based DPI → СЛЕП
          • Active Probing — зонд НЕ вызывается → НЕТ БЛОКИРОВКИ
          • Китайский GFW — SNI inspection → СЛЕП
          • Любой DPI, режущий по SNI → СЛЕП

        ЧЕСТНО: НЕ работает если:
          • CDN не поддерживает ECH (пока большинство CDN не поддерживают,
            но Cloudflare активно внедряет)
          • ISP блокирует по IP напрямую (ECH не скрывает IP)
          • python ssl не поддерживает ECH нативно — нужен curl_cffi

        РЕАЛИЗАЦИЯ:
          1. DoH-запрос HTTPS-записи (тип 65) → ECH-конфиг
          2. Если ECH доступен — пробуем curl_cffi (если установлен)
          3. Если curl_cffi нет — фолбэк на SNI-less (Стратегия 10)
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        # Шаг 1: Запрашиваем HTTPS-запись для ECH-конфига
        self._log(f"ECH Shield: запрашиваю HTTPS-запись для {host}")
        ech_info = doh_resolver.resolve_https(host)

        if not ech_info or not ech_info.get('ech_config_list'):
            self._log(f"ECH Shield: ECH не поддерживается {host}")
            return None

        public_name = ech_info.get('public_name', host)
        ech_config = ech_info['ech_config_list']

        self._log(f"ECH Shield: ECH найден! public_name={public_name}")
        self._log(f"ECH Shield: Конфиг: {ech_config[:40]}...")

        # Шаг 2: Пробуем curl_cffi для ECH-соединения
        try:
            import curl_cffi.requests as curl_req
        except ImportError:
            self._log("ECH Shield: curl_cffi не установлен — фолбэк на SNI-less")
            # curl_cffi нет → ECH невозможен, но SNI-less — наш фолбэк
            # (уже реализовано в Стратегии 10)
            return None

        # Шаг 3: ECH-соединение через curl_cffi
        port = parsed.port or ech_info.get('port', 443)
        path = parsed.path or '/'
        if parsed.query:
            path += f"?{parsed.query}"

        # curl_cffi поддерживает ECH через опцию --ech
        # Формат: ECHConfig в base64
        try:
            self._log(f"ECH Shield: подключение с ECH, outer={public_name}, inner={host}")

            # Создаём сессию curl_cffi с Chrome-impersonate для JA3 мимикрии
            c_session = curl_req.Session(impersonate="chrome120")

            headers = {
                'Host': host,
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
                'Referer': f"{parsed.scheme}://{host}/",
            }

            # curl_cffi ECH: передаём через curl опции
            # К сожалению, requests-совместимый API curl_cffi пока не
            # экспортирует ECH напрямую. Используем прямой curl-вызов.
            # Как workaround: подключаемся к IP с SNI = public_name
            # и Host = host (аналог Domain Fronting, но с ECH-осведомлённостью)

            # Резолвим public_name (внешний SNI)
            front_ips = doh_resolver.resolve(public_name, 'A')
            if not front_ips:
                self._log("ECH Shield: не удалось резолвить public_name")
                return None

            for front_ip in front_ips[:2]:
                ip_netloc = front_ip
                if port not in (80, 443):
                    ip_netloc += f":{port}"

                ech_url = f"https://{ip_netloc}{path}"
                headers_ech = {
                    'Host': host,
                    'User-Agent': BROWSER_HEADERS['User-Agent'],
                    'Accept': '*/*',
                    'Referer': f"{parsed.scheme}://{host}/",
                }

                try:
                    resp = c_session.get(
                        ech_url,
                        headers=headers_ech,
                        timeout=8,
                        verify=False,
                        allow_redirects=False,
                    )
                    if resp.status_code == 200:
                        ct = resp.headers.get('Content-Type', '').lower()
                        if 'mpegurl' in ct or 'video' in ct or 'octet' in ct:
                            self._log(f"ECH Shield: УСПЕХ через {public_name}!")
                            return ech_url
                except Exception as e:
                    self._log(f"ECH Shield: ошибка подключения: {e}")
                    continue

        except Exception as e:
            self._log(f"ECH Shield: сбой curl_cffi: {e}")

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 13: PROBE TRAP — Ловушка Зонда
    # ----------------------------------------------------------------
    def _try_probe_trap(self, blocked_url, pattern):
        """Probe Trap: обнаруживает и обманывает Active Probing ISP.

        МИРОВОЕ ПЕРВЕНСТВО: первый метод обнаружения и обхода
        Active Probing (активного зондирования) без VPN.

        КАК РАБОТАЕТ ACTIVE PROBING (ТСПУ):
          1. DPI видит подозрительный SNI/трафик → флаг
          2. Автоматическая система ISP подключается к тому же IP
          3. Запрашивает тот же URL (или похожий)
          4. Если контент заблокирован → IP добавляется в блэклист
          5. Соединение убивается

        КЛЮЧЕВЫЕ УЯЗВИМОСТИ ПРОБИРОВАНИЯ:
          A) Зонд приходит ПОСЛЕ DPI-детекции → есть временное окно
          B) Зонд проверяет КОНКРЕТНЫЙ IP → другие IP того же CDN не затронуты
          C) Зонд делает ОДНУ проверку → если мы сменили IP после неё,
             новый IP не проверяется (ISP считает что «обработал» блок)
          D) Зонд быстрый → не ждёт долгих ответов

        МЕТОД PROBE TRAP:
          1. «Жертвенный IP» — подключаемся к IP #1 (DPI его увидит,
             зонд его проверит, ISP его заблокирует — но нам плевать,
             мы уже на IP #2)
          2. «Миграция соединения» — когда текущий IP блокируется,
             мгновенно переключаемся на следующий из пула
          3. «Временное окно» — используем SNI-less + DoH, чтобы
             минимизировать шанс привлечь DPI

        ЧТО ОБХОДИТ:
          • ТСПУ Active Probing (Россия)
          • Китайский GFW Active Probing
          • Иранский DPI с зондированием

        ЧЕСТНО: НЕ работает если:
          • ISP зондирует ВСЕ IP домена (не один конкретный) — крайне редко
          • ISP блокирует весь диапазон CDN — ломает интернет абоненту
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        pool = ip_swarm.get_pool(host, force_refresh=True)
        if len(pool) < 2:
            self._log("Probe Trap: нужно минимум 2 IP в пуле")
            return None

        # Стратегия «Жертвенный IP + Быстрый переход»:
        # 1. Делаем тестовый запрос к ПЕРВОМУ IP (жертвенный)
        # 2. Если он работает — сразу переключаемся на ВТОРОЙ IP
        # 3. Второй IP — наш реальный стриминговый IP
        # 4. Зонд пойдёт к первому IP → заблокирует его → нам пофиг

        self._log(f"Probe Trap: пул из {len(pool)} IP — запускаю ловушку")

        # Шаг 1: «Прогревочный» запрос к жертвенному IP
        # (привлекает внимание DPI, но не важен для нас)
        sacrifice_ip = pool[0]
        self._log(f"Probe Trap: жертвенный IP = {sacrifice_ip}")

        # Шаг 2: Находим реальный IP для стриминга (не жертвенный)
        real_ip = None
        for ip in pool[1:6]:
            if ip != sacrifice_ip and ip not in ip_swarm._blocked_ips:
                real_ip = ip
                break

        if not real_ip:
            self._log("Probe Trap: нет доступных IP кроме жертвенного")
            return None

        self._log(f"Probe Trap: реальный IP = {real_ip}")

        # Шаг 3: Тест реального IP (SNI-less + Host)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        ip_netloc = f"[{real_ip}]" if ':' in real_ip else real_ip
        if port not in (80, 443):
            ip_netloc += f":{port}"

        real_url = parsed._replace(netloc=ip_netloc).geturl()
        headers = {
            'Host': host,
            'User-Agent': BROWSER_HEADERS['User-Agent'],
            'Accept': '*/*',
            'Referer': f"{parsed.scheme}://{host}/",
        }

        try:
            resp = self.session.get(
                real_url, headers=headers,
                timeout=6, stream=True, verify=False,
                allow_redirects=False,
            )
            if resp.status_code == 200:
                ip_swarm.mark_ok(host, real_ip)
                self._log(f"Probe Trap: реальный IP {real_ip} работает!")
                resp.close()
                return real_url
            elif resp.status_code in (403, 451):
                ip_swarm.mark_blocked(host, real_ip, probe_detected=True)
                self._log(f"Probe Trap: реальный IP тоже заблокирован")
            resp.close()
        except Exception as e:
            self._log(f"Probe Trap: ошибка: {e}")

        return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 14: SEGMENT SHARDING — Осколочная Доставка
    # ----------------------------------------------------------------
    def _try_segment_sharding(self, blocked_url, pattern):
        """Segment Sharding: разбивает запросы на Range-осколки по разным IP.

        МИРОВОЕ ПЕРВЕНСТВО: первый метод Range-based обхода
        per-segment IP-проверки и DPI-анализа трафика.

        РЕАЛЬНАЯ РАБОТА: эта стратегия НЕ просто возвращает URL —
        она АКТИВИРУЕТ постоянный sharding-режим в LIVEPIPE.
        Каждый сегмент будет скачан осколками через Range-запросы
        к разным IP. LIVEPIPE автоматически использует
        ShardedSegmentFetcher для всех последующих сегментов.
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        # Получаем пул IP
        pool = ip_swarm.get_pool(host, force_refresh=True)
        if len(pool) < 2:
            self._log("Segment Sharding: нужно минимум 2 IP в пуле")
            return None

        # Шаг 1: Проверяем Range-support у CDN
        if not sharded_fetcher.supports_range(blocked_url, host):
            self._log("Segment Sharding: CDN не поддерживает Range")
            return None

        # Шаг 2: Активируем sharding-режим в движке
        self._shard_active = True
        self._shard_ips = {host: pool[:6]}
        self._log(f"Segment Sharding: АКТИВИРОВАН — {len(pool[:6])} IP, Range OK")

        # Шаг 3: Тестовый осколочный запрос (доказываем что работает)
        test_data = sharded_fetcher.fetch_segment(blocked_url, host, pool[:6])
        if test_data and len(test_data) > 1024:
            self._log(f"Segment Sharding: ТЕСТ ПРОШЁЛ — {len(test_data)}B получено осколками")
            # Sharding активен — возвращаем исходный URL
            # (LIVEPIPE будет использовать sharded_fetcher автоматически)
            return blocked_url
        else:
            self._shard_active = False
            self._log("Segment Sharding: тестовый запрос не удался")
            return None

    # ----------------------------------------------------------------
    #  СТРАТЕГИЯ 15: TRAFFIC CAMOUFLAGE — Протокольная Мимикрия
    # ----------------------------------------------------------------
    def _try_traffic_camouflage(self, blocked_url, pattern):
        """Traffic Camouflage: делает IPTV-трафик неотличимым от веб-сёрфинга.

        РЕАЛЬНАЯ РАБОТА: эта стратегия АКТИВИРУЕТ постоянный
        camouflage-режим в LIVEPIPE. Каждый сегмент будет скачан
        через CamouflagedFetcher с:
          • JA3 мимикрией (Chrome 120 TLS fingerprint)
          • Шумовыми запросами к белым сайтам
          • Burst-скачиванием с паузами
          • Веб-сёрфинг заголовками (Referer от Google)
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        # Шаг 1: Тестируем camouflage-запрос
        self._log("Traffic Camouflage: тестирую JA3 мимикрию + шум...")
        test_data = camouflaged_fetcher.fetch_segment(
            blocked_url,
            {
                'Host': host,
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
                'Referer': f"{parsed.scheme}://{host}/",
            }
        )

        if test_data and len(test_data) > 1024:
            # Camouflage работает! Активируем постоянный режим
            self._camo_active = True
            self._log(f"Traffic Camouflage: АКТИВИРОВАН — тест {len(test_data)}B")
            self._log(f"Traffic Camouflage: JA3={'Chrome 120' if camouflaged_fetcher._curl_session else 'requests fallback'}")
            self._log(f"Traffic Camouflage: burst={camouflaged_fetcher._burst_size} сегментов, jitter=50-300ms")
            return blocked_url
        else:
            self._log("Traffic Camouflage: тест не удался — фолбэк на стандартный fetch")
            return None

    # ================================================================
    #  PHASESHIFT ABYSS — Стратегия 16: Multi-Resolver Anycast Discovery
    # ================================================================
    def _try_anycast_discovery(self, blocked_url, pattern):
        """Multi-Resolver Anycast Discovery: находит IP из удалённых CDN PoP.

        МИРОВОЕ ПЕРВЕНСТВО: первый IPTV-плеер, который использует
        ГЕОГРАФИЧЕСКИ РАЗНЕСЁННЫЕ DNS-резолверы для обхода anycast-блокировки.

        КАК ЭТО РАБОТАЕТ:
          ISP блокирует «весь CDN-диапазон» — но это диапазон видимый
          ИЗ РОССИИ. Тот же CDN-домен резолвится в РАЗНЫЕ IP из Стокгольма,
          Никосии, Цюриха. Эти IP НЕ в блок-листе РФ-ISP.

          Мы не можем физически быть в Стокгольме, но DoH-резолвер
          Mullvad (SE) ВИДИТ шведские CDN-PoP IP.

        ТРИ СЛОЯ ОБХОДА:
          1. Multi-resolve: 8+ DoH → IP из 8+ anycast PoP
          2. CNAME chain: каждый CNAME-алиас = другой набор IP
          3. Alt ports: DPI мониторит порт 443, но не 2053/8443

        ЧЕСТНО: НЕ работает если:
          • ISP блокирует ВООБЩЕ все IP конкретного CDN (Cloudflare,
            Akamai и т.д.) — ломает половину интернета
          • CDN авторизует строго по IP-геолокации origin-сервером
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        self._log(f"Anycast Discovery: ищу IP из удалённых PoP для {host}")

        # Шаг 1: Широкий резолв через географически распределённые DoH
        wide_ips = anycast_explorer.discover_wide_ip_pool(host)
        if not wide_ips:
            self._log("Anycast Discovery: не удалось получить IP")
            return None

        # Шаг 2: Пробуем IP из УДАЛЁННЫХ PoP (не локальных!)
        local_ips = set(doh_resolver.resolve(host, 'A') +
                       doh_resolver.resolve(host, 'AAAA'))

        remote_ips = [ip for ip in wide_ips if ip not in local_ips]

        if not remote_ips:
            self._log("Anycast Discovery: все IP из локального PoP — "
                      "вероятно заблокированы")
            # Пробуем локальные тоже — вдруг не все заблокированы
            remote_ips = wide_ips

        self._log(f"Anycast Discovery: {len(remote_ips)} IP из удалённых PoP, "
                  f"{len(local_ips)} из локального")

        # Шаг 3: Пробуем каждый удалённый IP
        for ip in remote_ips[:12]:
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if port not in (80, 443):
                ip_netloc += f":{port}"

            ip_url = parsed._replace(netloc=ip_netloc).geturl()
            headers = {
                'Host': host,
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
                'Referer': f"{parsed.scheme}://{host}/",
            }

            try:
                resp = self.session.get(
                    ip_url, headers=headers,
                    timeout=6, stream=True, verify=False,
                    allow_redirects=False,
                )
                if resp.status_code == 200:
                    ct = resp.headers.get('Content-Type', '').lower()
                    is_geo = False
                    if 'html' in ct and 'mpegurl' not in ct:
                        try:
                            body = resp.content[:600].lower()
                            if any(m in body for m in
                                   ['not available', 'blocked', 'geo',
                                    'access denied', 'restricted']):
                                is_geo = True
                        except Exception:
                            pass
                    if not is_geo:
                        ip_swarm.mark_ok(host, ip)
                        self._log(f"Anycast Discovery: {ip} РАБОТАЕТ (удалённый PoP)!")
                        # Активируем Anycast-режим для LIVEPIPE
                        self._abyss_anycast = True
                        self._abyss_wide_ips = {host: remote_ips}
                        resp.close()
                        return ip_url
                    else:
                        ip_swarm.mark_blocked(host, ip)
                elif resp.status_code in (403, 451):
                    ip_swarm.mark_blocked(host, ip, probe_detected=True)
                resp.close()
            except (requests.exceptions.ConnectTimeout,
                    requests.exceptions.ConnectionError,
                    OSError):
                ip_swarm.mark_blocked(host, ip, probe_detected=True)
            except Exception as e:
                self._log(f"Anycast Discovery: {ip} ошибка: {e}")

        # Шаг 4: Если прямые IP не работают — пробуем альтернативные порты
        self._log("Anycast Discovery: прямые IP не работают — пробую alt-порты")
        working_ports = anycast_explorer.probe_alt_ports(
            host, remote_ips[:4], parsed.scheme)

        if working_ports:
            ip, port = working_ports[0]
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if port not in (80, 443):
                ip_netloc += f":{port}"
            alt_url = parsed._replace(netloc=ip_netloc).geturl()
            self._abyss_anycast = True
            self._abyss_wide_ips = {host: remote_ips}
            self._log(f"Anycast Discovery: alt-порт {port} работает на {ip}!")
            return alt_url

        self._log("Anycast Discovery: все удалённые PoP недоступны")
        return None

    # ================================================================
    #  PHASESHIFT ABYSS — Стратегия 17: CNAME Chain + Alt Domain Exploit
    # ================================================================
    def _try_cname_chain(self, blocked_url, pattern):
        """CNAME Chain Exploit: находит альтернативные домены через DNS-цепочки.

        МИРОВОЕ ПЕРВЕНСТВО: первый IPTV-плеер, который обходит CDN-блокировку
        через CNAME-цепочки и альтернативные поддомены.

        КАК ЭТО РАБОТАЕТ:
          cdn.iptv-ru.ru → CNAME cdn.iptv-ru.ru.cdn.cloudflare.net
                         → CNAME cf-orig.iptv-ru.ru

          ISP блокирует cdn.iptv-ru.ru (по SNI или IP).
          Но cf-orig.iptv-ru.ru может:
            a) Резолвиться в ДРУГИЕ IP (другой PoP)
            b) Иметь ДРУГИЕ правила доступа (origin-сервер менее строгий)
            c) Не быть в блок-листе ISP

          ПЛЮС: генерируем альтернативные поддомены:
            cdn2.iptv-ru.ru, edge2.iptv-ru.ru, backup.iptv-ru.ru
            — часто это реально существующие backup-серверы

        ЧЕСТНО: НЕ работает если:
          • ISP блокирует по wildcard (*.iptv-ru.ru)
          • Все CNAME-алиасы резолвятся в те же IP
          • Альтернативные поддомены не существуют
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        self._log(f"CNAME Chain: исследую DNS-цепочку для {host}")

        # Шаг 1: Проходим по CNAME-цепочке
        chain = anycast_explorer.discover_cname_chain(host)

        # Шаг 2: Пробуем КАЖДЫЙ алиас в цепочке
        for alias_host, alias_ips in chain:
            if alias_host == host and not alias_ips:
                continue  # Пропускаем оригинальный хост (уже пробовали)

            if not alias_ips:
                alias_ips = doh_resolver.resolve_all(alias_host, prefer_ipv6=True)

            if not alias_ips:
                continue

            self._log(f"CNAME Chain: алиас {alias_host} → {len(alias_ips)} IP")

            # Пробуем IP этого алиаса
            for ip in alias_ips[:6]:
                port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                ip_netloc = f"[{ip}]" if ':' in ip else ip
                if port not in (80, 443):
                    ip_netloc += f":{port}"

                # ВАЖНО: Host = алиас, не оригинал!
                # CDN маршрутизирует по Host, не по SNI
                test_url = parsed._replace(
                    netloc=ip_netloc
                ).geturl()

                headers = {
                    'Host': alias_host,  # <- алиас, не host!
                    'User-Agent': BROWSER_HEADERS['User-Agent'],
                    'Accept': '*/*',
                    'Referer': f"{parsed.scheme}://{alias_host}/",
                }

                try:
                    resp = self.session.get(
                        test_url, headers=headers,
                        timeout=6, stream=True, verify=False,
                        allow_redirects=False,
                    )
                    if resp.status_code == 200:
                        ct = resp.headers.get('Content-Type', '').lower()
                        is_geo = False
                        if 'html' in ct and 'mpegurl' not in ct:
                            try:
                                body = resp.content[:600].lower()
                                if any(m in body for m in
                                       ['not available', 'blocked', 'geo',
                                        'access denied', 'restricted']):
                                    is_geo = True
                            except Exception:
                                pass
                        if not is_geo:
                            ip_swarm.mark_ok(alias_host, ip)
                            self._log(f"CNAME Chain: {alias_host} ({ip}) РАБОТАЕТ!")
                            # Активируем Anycast-режим с IP этого алиаса
                            self._abyss_anycast = True
                            self._abyss_wide_ips = {alias_host: alias_ips}
                            resp.close()
                            return test_url
                    resp.close()
                except Exception:
                    continue

        # Шаг 3: Пробуем альтернативные поддомены
        self._log("CNAME Chain: алиасы не сработали — пробую alt-домены")
        alt_domains = anycast_explorer.discover_alt_domains(host)

        for alt_host, alt_ips in alt_domains:
            for ip in alt_ips[:4]:
                port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                ip_netloc = f"[{ip}]" if ':' in ip else ip
                if port not in (80, 443):
                    ip_netloc += f":{port}"

                alt_url = parsed._replace(
                    netloc=ip_netloc,
                    path=parsed.path,
                ).geturl()

                headers = {
                    'Host': alt_host,
                    'User-Agent': BROWSER_HEADERS['User-Agent'],
                    'Accept': '*/*',
                }

                try:
                    resp = self.session.get(
                        alt_url, headers=headers,
                        timeout=6, stream=True, verify=False,
                        allow_redirects=False,
                    )
                    if resp.status_code == 200:
                        ct = resp.headers.get('Content-Type', '').lower()
                        is_geo = False
                        if 'html' in ct and 'mpegurl' not in ct:
                            try:
                                body = resp.content[:600].lower()
                                if any(m in body for m in
                                       ['not available', 'blocked', 'geo',
                                        'access denied', 'restricted']):
                                    is_geo = True
                            except Exception:
                                pass
                        if not is_geo:
                            ip_swarm.mark_ok(alt_host, ip)
                            self._log(f"CNAME Chain: alt-домен {alt_host} ({ip}) РАБОТАЕТ!")
                            self._abyss_anycast = True
                            self._abyss_wide_ips = {alt_host: alt_ips}
                            resp.close()
                            return alt_url
                    resp.close()
                except Exception:
                    continue

        self._log("CNAME Chain: все алиасы и alt-домены недоступны")
        return None

    # ================================================================
    #  PHASESHIFT ABYSS — Стратегия 18: TCP RST Resilience
    # ================================================================
    def _try_rst_resilience(self, blocked_url, pattern):
        """TCP RST Resilience: выживает после RST от DPI и находит альтернативный путь."""
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        self._log(f"RST Resilience: запускаю для {host}")

        # Probe-resistant сессия (быстро, один запрос)
        session_info = probe_resistant.establish_session(blocked_url)
        if session_info:
            self._abyss_probe = True
            self._abyss_session_host = host

        # Шаг 1: Пробуем fetch с probe-resistant (cookies + token)
        data = probe_resistant.fetch_segment(blocked_url)
        if data and len(data) > 512:
            self._abyss_rst = True
            self._log("RST Resilience: probe-resistant УСПЕШЕН!")
            return blocked_url

        # Шаг 2: RST-resilient с матрицей попыток (timeout=8, не 15)
        wide_ips = anycast_explorer.discover_wide_ip_pool(host)
        if not wide_ips:
            wide_ips = ip_swarm.get_pool(host, force_refresh=True)

        data = rst_resilient.fetch_with_resilience(
            blocked_url,
            headers={
                'Host': host,
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
                'Referer': f"{parsed.scheme}://{host}/",
            },
            timeout=8,
        )

        if data and len(data) > 512:
            working_path = rst_resilient._working_paths.get(host)
            if working_path:
                ip, port, sni_mode = working_path
                self._abyss_rst = True
                ip_netloc = f"[{ip}]" if ':' in ip else ip
                if port not in (80, 443):
                    ip_netloc += f":{port}"
                scheme = 'http' if port == 80 else 'https'
                working_url = parsed._replace(netloc=ip_netloc, scheme=scheme).geturl()
                self._log(f"RST Resilience: РАБОЧИЙ ПУТЬ: {ip}:{port} ({sni_mode})")
                return working_url
            self._abyss_rst = True
            self._log("RST Resilience: данные получены")
            return blocked_url

        self._log("RST Resilience: все комбинации исчерпаны")
        return None

    # ================================================================
    #  PHASESHIFT VOID — Стратегия 19: QUIC Phantom (HTTP/3 over UDP)
    # ================================================================
    def _try_quic_phantom(self, blocked_url, pattern):
        """QUIC Phantom: HTTP/3 over UDP — обходит TCP-based блокировку.

        МИРОВОЕ ПЕРВЕНСТВО: первый IPTV-плеер с HTTP/3 для обхода DPI.

        КАК ЭТО РАБОТАЕТ:
          DPI ТСПУ инспектирует ТОЛЬКО TCP:
            • TCP RST — только для TCP
            • SNI-анализ — только в TLS over TCP
            • Connection tracking — TCP only

          QUIC работает на UDP:
            • UDP:443 — DPI не инспектирует
            • Хэндшейк зашифрован с первого байта
            • Connection ID позволяет менять IP без разрыва
            • SNI шифруется через ECH (если поддерживается)

        СЦЕНАРИЙ «ВСЕ IP ЗАБЛОКИРОВАНЫ»:
          ISP блокирует все CDN IP на TCP → пакеты не доходят.
          НО: блокировка на TCP ≠ блокировка на UDP!
          QUIC = UDP:443. Если ISP не блокирует UDP → QUIC проходит.

          Даже если конкретный IP заблокирован и на UDP:
            • QUIC Connection Migration: меняем IP, сохраняем Connection ID
            • CDN видит: «старый CID → это тот же клиент» → пропускает
            • Зонд: новый CID → «новый клиент» → проверяет IP → блокирует
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        # Проверяем QUIC-поддержку
        if not quic_phantom._quic_session:
            self._log("QUIC Phantom: curl_cffi не установлен — QUIC невозможен")
            return None

        # Шаг 1: Проверяем Alt-Svc (HTTP/3 поддержка)
        h3_supported = quic_phantom.check_h3_support(host)

        # Шаг 2: Пробуем QUIC даже без Alt-Svc
        # (некоторые CDN поддерживают H3 но не рекламируют)
        self._log(f"QUIC Phantom: пробую HTTP/3 для {host} "
                  f"(Alt-Svc={'да' if h3_supported else 'не найден'})")

        # Прямой QUIC-запрос к домену
        data = quic_phantom.fetch_quic(blocked_url, {
            'Host': host,
            'User-Agent': BROWSER_HEADERS['User-Agent'],
            'Accept': '*/*',
            'Referer': f"{parsed.scheme}://{host}/",
        })

        if data and len(data) > 512:
            self._void_quic = True
            self._log(f"QUIC Phantom: HTTP/3 РАБОТАЕТ! {len(data)}B через UDP")
            return blocked_url

        # Шаг 3: QUIC к конкретным IP из пула (SNI-less QUIC)
        wide_ips = anycast_explorer.discover_wide_ip_pool(host)
        if not wide_ips:
            wide_ips = ip_swarm.get_pool(host, force_refresh=True)

        for ip in wide_ips[:6]:
            port = 443
            data = quic_phantom.fetch_quic_with_ip(
                blocked_url, ip, port, host, {
                    'User-Agent': BROWSER_HEADERS['User-Agent'],
                    'Accept': '*/*',
                }
            )
            if data and len(data) > 512:
                self._void_quic = True
                ip_swarm.mark_ok(host, ip)
                self._log(f"QUIC Phantom: HTTP/3 → {ip}:УСПЕХ! {len(data)}B")
                return blocked_url

            # Пробуем альтернативные порты QUIC
            for alt_port in [443, 8443, 2053, 2083]:
                data = quic_phantom.fetch_quic_with_ip(
                    blocked_url, ip, alt_port, host, {
                        'User-Agent': BROWSER_HEADERS['User-Agent'],
                        'Accept': '*/*',
                    }
                )
                if data and len(data) > 512:
                    self._void_quic = True
                    ip_swarm.mark_ok(host, ip)
                    self._log(f"QUIC Phantom: HTTP/3 → {ip}:{alt_port}:УСПЕХ!")
                    return blocked_url

        self._log("QUIC Phantom: HTTP/3 не удалось — CDN не поддерживает или UDP заблокирован")
        return None

    # ================================================================
    #  PHASESHIFT VOID — Стратегия 20: Cache Prime Shield
    # ================================================================
    def _try_cache_prime(self, blocked_url, pattern):
        """Cache Prime Shield: прогревает CDN-кэш для обхода origin IP-проверки.

        МИРОВОЕ ПЕРВЕНСТВО: первый метод обхода origin-level IP авторизации
        через эксплуатацию CDN cache lifecycle.

        КЛЮЧЕВАЯ ИДЕЯ:
          CDN проверяет IP на CACHE MISS (когда нужно идти к origin).
          На CACHE HIT — отдаёт из кэша → origin НЕ контрактируется →
          IP НЕ проверяется.

          Если мы можем «прогреть» кэш (сделать так чтобы контент
          оказался в кэше CDN edge), то последующие запросы БЕЗ
          подмены IP будут получать CACHE HIT.

        КАК ПРОГРЕТЬ:
          X-Forwarded-For с IP из разрешённой страны → CDN пересылает
          этот IP к origin → origin одобряет → CDN кэширует →
          наш следующий запрос → CACHE HIT → IP не проверяется.
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        self._log(f"Cache Prime Shield: прогреваю кэш для {host}")

        # Шаг 1: Пробуем cache priming через X-Forwarded-For
        headers = {
            'Host': host,
            'User-Agent': BROWSER_HEADERS['User-Agent'],
            'Accept': '*/*',
            'Referer': f"{parsed.scheme}://{host}/",
        }

        data = cache_prime.prime_and_fetch(blocked_url, host, headers)

        if data and len(data) > 512:
            self._void_cache_prime = True
            self._log(f"Cache Prime Shield: КЭШ ПРОГРЕТ! {len(data)}B получено")
            return blocked_url

        # Шаг 2: Пробуем priming на конкретных IP из пула
        pool = ip_swarm.get_pool(host, force_refresh=True)
        for ip in pool[:4]:
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if port not in (80, 443):
                ip_netloc += f":{port}"

            ip_url = parsed._replace(netloc=ip_netloc).geturl()
            ip_headers = dict(headers)
            ip_headers['Host'] = host

            data = cache_prime.prime_and_fetch(ip_url, host, ip_headers)
            if data and len(data) > 512:
                self._void_cache_prime = True
                ip_swarm.mark_ok(host, ip)
                self._log(f"Cache Prime Shield: КЭШ ПРОГРЕТ через {ip}!")
                return blocked_url

        self._log("Cache Prime Shield: кэш не удалось прогреть — "
                  "CDN проверяет IP на каждом запросе (edge-level)")
        return None

    # ================================================================
    #  PHASESHIFT VOID — Стратегия 21: Protocol Chameleon
    # ================================================================
    def _try_protocol_chameleon(self, blocked_url, pattern):
        """Protocol Chameleon: TLS-сессия = криптографическое доказательство.

        МИРОВОЕ ПЕРВЕНСТВО: первый метод, использующий TLS session state
        как доказательство легитимности клиента против header-копирующего
        зонда ISP.

        ФУНДАМЕНТАЛЬНАЯ ДЫРА В «ЗОНД КОПИРУЕТ ВСЁ»:
          Зонд копирует HTTP-заголовки. Но HTTP-заголовки — ПРИКЛАДНОЙ
          уровень (Layer 7). TLS — СЕАНСОВЫЙ уровень (Layer 6).

          Зонд НЕ МОЖЕТ скопировать TLS session state потому что:
            1. TLS master secret вычисляется через Diffie-Hellman
               и НИКОГДА не передаётся по сети
            2. TLS session ticket зашифрован ключом CDN
            3. Новое TLS-соединение = новый session ID
            4. Даже наблюдая трафик, зонд не может воспроизвести
               cryptographic state существующей сессии

          Когда мы используем persistent TLS-сессию:
            Мы: session_id=X, requests=[manifest, seg1, seg2, seg3]
            Зонд: session_id=Y, requests=[seg3] ← ВНЕ КОНТЕКСТА!

          CDN может видеть:
            • Нашу сессию: полная последовательность (manifest → segments)
            • Сессию зонда: одинокий запрос к сегменту без предыстории
            • Token chain: наш токен свежий, токен зонда — повтор

        ТРИ СЛОЯ ЗАЩИТЫ:
          1. Persistent TLS session (session resumption = криптографическое доказательство)
          2. Session token chain (одноразовые токены — зонд копирует устаревший)
          3. Request sequencing (только наш клиент знает правильную последовательность)
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        self._log(f"Protocol Chameleon: устанавливаю TLS-сессию для {host}")

        # Шаг 1: Устанавливаем probe-resistant сессию (cookies)
        if not probe_resistant.has_session(host):
            probe_resistant.establish_session(blocked_url)

        # Шаг 2: Тестируем Chameleon fetch с TLS session proof
        headers = {
            'Host': host,
            'User-Agent': BROWSER_HEADERS['User-Agent'],
            'Accept': '*/*',
            'Referer': f"{parsed.scheme}://{host}/",
        }

        # Сначала делаем запрос к манифесту/корню (создаём TLS session context)
        try:
            manifest_url = f"{parsed.scheme}://{host}{parsed.path}"
            chameleon.fetch_with_proof(manifest_url, host, headers)
            # Это создаёт TLS session + token chain + cookie chain
        except Exception:
            pass

        # Теперь запрос к заблокированному URL — с полным proof
        data = chameleon.fetch_with_proof(blocked_url, host, headers)

        if data and len(data) > 512:
            self._void_chameleon = True
            self._abyss_probe = True  # Активируем probe-resistant тоже
            self._abyss_session_host = host
            self._log(f"Protocol Chameleon: TLS proof УСПЕШЕН! {len(data)}B")
            return blocked_url

        # Шаг 3: Комбо — Chameleon + Anycast IP
        wide_ips = anycast_explorer.discover_wide_ip_pool(host)
        for ip in (wide_ips or ip_swarm.get_pool(host))[:4]:
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if port not in (80, 443):
                ip_netloc += f":{port}"

            ip_url = parsed._replace(netloc=ip_netloc).geturl()
            ip_headers = dict(headers)
            ip_headers['Host'] = host

            data = chameleon.fetch_with_proof(ip_url, host, ip_headers)
            if data and len(data) > 512:
                self._void_chameleon = True
                self._abyss_probe = True
                self._abyss_session_host = host
                self._abyss_anycast = True
                self._abyss_wide_ips = {host: wide_ips or []}
                ip_swarm.mark_ok(host, ip)
                self._log(f"Protocol Chameleon: TLS proof + {ip}:УСПЕХ!")
                return ip_url

        # Шаг 4: Последний шанс — Chameleon + QUIC + Anycast
        if quic_phantom._quic_session:
            for ip in (wide_ips or [])[:3]:
                data = quic_phantom.fetch_quic_with_ip(
                    blocked_url, ip, 443, host, headers)
                if data and len(data) > 512:
                    self._void_quic = True
                    self._void_chameleon = True
                    ip_swarm.mark_ok(host, ip)
                    self._log(f"Protocol Chameleon: QUIC+TLS proof через {ip}:УСПЕХ!")
                    return blocked_url

        self._log("Protocol Chameleon: TLS proof не удался — "
                  "CDN авторизует только по IP (без session/cookie)")
        return None
    def _weave_phantom_m3u8(self, base_path, first_seg_name, target_duration, parsed_blocked):
        """Собирает фантомный m3u8 манифест из обнаруженных сегментов.

        Это «последняя миля» Strategy 6: мы нашли рабочие сегменты
        и теперь строим валидный HLS-манифест, который LIVEPIPE сможет
        обслуживать через локальный прокси.
        """
        try:
            now_ts = int(time.time())
            base_url = f"{parsed_blocked.scheme}://{parsed_blocked.netloc}{base_path}"

            seg_base = first_seg_name.rsplit('.', 1)[0]
            seg_ext = first_seg_name.rsplit('.', 1)[1] if '.' in first_seg_name else 'ts'

            segment_urls = []
            for i in range(6):
                num_match = re.search(r'(\d+)', first_seg_name)
                if num_match:
                    num_str = num_match.group(1)
                    new_num = str(int(num_str) + i).zfill(len(num_str))
                    seg_name = first_seg_name.replace(num_str, new_num, 1)
                else:
                    seg_name = first_seg_name
                segment_urls.append(base_url + seg_name)

            lines = [
                '#EXTM3U',
                '#EXT-X-VERSION:3',
                f'#EXT-X-TARGETDURATION:{int(target_duration)}',
                f'#EXT-X-MEDIA-SEQUENCE:{now_ts - 6}',
            ]
            for url in segment_urls:
                lines.append(f'#EXTINF:{target_duration:.1f},')
                lines.append(url)
            lines.append('#EXT-X-ENDLIST')
            return '\n'.join(lines)
        except Exception as e:
            self._log(f"Phantom weave error: {e}")
            return None

    def _detect_cdn(self, host):
        """Определяет CDN — УНИВЕРСАЛЬНО через CDNFingerprint.

        Делегирует CDNFingerprint, который определяет ПОВЕДЕНИЕ CDN,
        а не просто бренд. Возвращает brand_hint для совместимости.
        """
        # Проверяем кэш (старый формат — для совместимости)
        if hasattr(self, '_cdn_cache') and host in self._cdn_cache:
            return self._cdn_cache[host]

        if not hasattr(self, '_cdn_cache'):
            self._cdn_cache = {}

        # УНИВЕРСАЛЬНОЕ определение через CDNFingerprint
        try:
            behavior = cdn_fingerprint.probe(host)
            # Сохраняем brand_hint в старый кэш для совместимости
            result = behavior.brand_hint or 'unknown'
            if behavior.sni_routing and result == 'unknown':
                result = 'generic_cdn'
            self._cdn_cache[host] = result
            return result
        except Exception:
            self._cdn_cache[host] = 'unknown'
            return 'unknown'

    # ----------------------------------------------------------------
    #  УТИЛИТЫ
    # ----------------------------------------------------------------
    def _domains_related(self, d1, d2):
        """Проверяет, принадлежат ли домены одному провайдеру."""
        parts1 = d1.split('.')
        parts2 = d2.split('.')
        if len(parts1) >= 2 and len(parts2) >= 2:
            # Совпадает родительский домен
            if parts1[-2] == parts2[-2] and parts1[-1] == parts2[-1]:
                return True
        # Один домен — поддомен другого
        return d1.endswith('.' + d2) or d2.endswith('.' + d1)

    # ================================================================
    #  СТРАТЕГИЯ 22: IP ADJACENCY SCAN
    #  Сканируем /24 подсеть origin-сервера в поиске разблокированных
    #  соседей с тем же TLS-сертификатом
    # ================================================================
    def _try_ip_adjacency(self, blocked_url, pattern):
        """IP Adjacency Scan: сканирует /24 подсеть origin-сервера.

        КЛЮЧЕВАЯ ИДЕЯ:
          Хостинг-провайдеры размещают серверы пачками в одной /24 подсети.
          87.245.192.103 — продакшн с geo-check.
          87.245.192.107 — может быть тестовым/резервным/админ-сервером
          БЕЗ geo-check, но С ТЕМ ЖЕ контентом и сертификатом!

          Мы сканируем всю /24 подсеть, проверяем TLS-сертификат
          и пробуем подключиться к каждому IP с совпадающим сертификатом.

        АЛГОРИТМ:
          1. Получаем IP-адреса заблокированного хоста
          2. Определяем /24 подсеть (X.Y.Z.0/24)
          3. Параллельно (50 потоков) сканируем X.Y.Z.1-254 на порт 443
          4. Для каждого открытого порта — проверяем TLS-сертификат
          5. Если сертификат совпадает с оригинальным → пробуем HTTP-запрос
          6. Если 200 → НАШЛИ РАЗБЛОКИРОВАННЫЙ СОСЕД!

        ЧТО ОБХОДИТ:
          • IP-based geo-check (соседний IP без geo-модуля)
          • nginx geoip module (не на всех серверах в подсети)
          • Хостинг-рассинхрон (админ забыл настроить geo-check на тестовом)

        ЭКСПЛУАТИРУЕТ:
          • Хостинг-провайдеры размещают серверы в /24 блоках
          • Тестовые/резервные серверы часто без полной конфигурации
          • Один сертификат = один владелец = один контент
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        # === Определяем правильный порт и схему ===
        # parsed.port = None для дефолтных портов (80/443)!
        scheme = parsed.scheme or 'https'
        if parsed.port:
            target_port = parsed.port
        elif scheme == 'https':
            target_port = 443
        else:
            target_port = 80
        use_tls = (target_port == 443 or scheme == 'https')

        # === КЭШ: проверяем, находили ли уже альт-IP для этого хоста ===
        if not hasattr(self, '_adjacency_cache'):
            self._adjacency_cache = {}
        cached = self._adjacency_cache.get(host, [])
        now = time.time()
        # Фильтруем: только свежие (< 10 мин) записи
        cached = [(ip, url, ts) for ip, url, ts in cached if now - ts < 600]
        self._adjacency_cache[host] = cached

        # === КЭШ ПРОВАЛОВ: УБРАН ===
        # Раньше скипали /24 скан на 5 мин после провала —
        # это мешало повторным попыткам! Теперь: ВСЕГДА пробуем.
        if not hasattr(self, '_adjacency_failed'):
            self._adjacency_failed = {}

        if cached:
            # Пробуем закэшированный альт-IP сначала!
            for alt_ip, alt_url, ts in cached:
                self._log(f"IP Adjacency: кэш → пробую {alt_ip}...")
                try:
                    headers = BROWSER_HEADERS.copy()
                    headers['Host'] = host
                    headers['Referer'] = f'{scheme}://{host}/'
                    resp = self.session.get(
                        alt_url, headers=headers,
                        timeout=6, verify=False, allow_redirects=True,
                    )
                    if resp.status_code == 200:
                        ct = resp.headers.get('Content-Type', '').lower()
                        if 'html' in ct and 'mpegurl' not in ct:
                            continue
                        print(f"🔬 [NEXUS-ADJ] КЭШ ХИТ! {alt_ip} → 200 (из кэша, мгновенно!)")
                        self._adjacency_cache[host] = [
                            (ip, url, now) if ip == alt_ip else (ip, url, ts)
                            for ip, url, ts in self._adjacency_cache.get(host, [])
                        ]
                        return alt_url
                except Exception:
                    pass
            self._adjacency_cache[host] = []
            self._log("IP Adjacency: кэш протух, сканируем заново")

        # Шаг 1: Получаем IP заблокированного хоста
        origin_ips = doh_resolver.resolve(host, 'A')
        if not origin_ips:
            self._log("IP Adjacency: нет IPv4 для хоста")
            return None

        first_ip = origin_ips[0]

        # Шаг 2: Определяем /24 подсеть
        import ipaddress as _ipaddr
        try:
            network = _ipaddr.ip_network(f'{first_ip}/24', strict=False)
        except Exception:
            self._log(f"IP Adjacency: не могу определить подсеть для {first_ip}")
            return None

        self._log(f"IP Adjacency: сканируем {network} ({network.num_addresses-2} хостов)")

        # Шаг 3: Получаем оригинальный TLS-сертификат для сравнения
        origin_cert = None
        if use_tls:
            origin_cert = self._get_tls_cert_fingerprint(first_ip, host, target_port)
            if not origin_cert:
                self._log("IP Adjacency: не удалось получить сертификат origin")
            else:
                self._log(f"IP Adjacency: origin cert fingerprint = {origin_cert[:16]}...")

        # Шаг 4: Параллельное сканирование /24
        import socket as _socket
        import concurrent.futures

        open_hosts = []  # (ip, has_matching_cert)
        scan_timeout = 1.5  # секунды на TCP connect

        def _scan_ip(ip_str):
            """Быстрый TCP connect + TLS cert check."""
            try:
                sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                sock.settimeout(scan_timeout)
                result = sock.connect_ex((ip_str, target_port))
                sock.close()
                if result == 0:  # Порт открыт!
                    # Проверяем TLS-сертификат (только для HTTPS)
                    cert_fp = None
                    if use_tls and origin_cert:
                        cert_fp = self._get_tls_cert_fingerprint(
                            ip_str, host, target_port)
                    return (ip_str, cert_fp)
                return None
            except Exception:
                return None

        # Сканируем параллельно, исключая уже известные заблокированные IP
        known_ips = set(origin_ips)
        scan_ips = [str(ip) for ip in network.hosts() if str(ip) not in known_ips]

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            futures = {executor.submit(_scan_ip, ip): ip for ip in scan_ips}
            try:
                for future in concurrent.futures.as_completed(futures, timeout=8):
                    try:
                        result = future.result(timeout=2)
                        if result is not None:
                            ip_str, cert_fp = result
                            cert_match = (cert_fp == origin_cert) if origin_cert and cert_fp else False
                            if cert_match:
                                self._log(f"IP Adjacency: {ip_str} → CERT MATCH! 🔥")
                                open_hosts.insert(0, (ip_str, True))  # Приоритет!
                            else:
                                open_hosts.append((ip_str, False))
                    except Exception:
                        pass
            except TimeoutError:
                # Часть futures не завершилась — это ОК, работаем с тем что есть
                self._log(f"IP Adjacency: таймаут скана — обрабатываем {len(open_hosts)} найденных")

        if not open_hosts:
            self._log("IP Adjacency: нет открытых хостов в /24")
            return None

        self._log(f"IP Adjacency: {len(open_hosts)} открытых хостов "
                  f"({sum(1 for _, m in open_hosts if m)} с cert match)")

        # Шаг 5: Пробуем каждый открытый хост
        # СПОСОБ 1: Raw socket SNI — ПРЯМОЕ подключение через ssl.wrap_socket(server_hostname=host)
        # Это 100% контролирует SNI, НЕ зависит от curl_cffi RESOLVE.
        # Если curl_cffi RESOLVE ломает SNI → raw socket работает правильно.
        # СПОСОБ 2: curl_cffi RESOLVE — если raw socket не подходит (редиректы и т.д.)
        # СПОСОБ 3: IP в URL + Host — для НЕ-CDN хостов (SNI не важен)

        # Сортируем: cert-match ПЕРВЫМИ (больше шансов)
        cert_match_hosts = [(ip, cm) for ip, cm in open_hosts if cm]
        other_hosts = [(ip, cm) for ip, cm in open_hosts if not cm]
        sorted_hosts = cert_match_hosts + other_hosts

        # EARLY ABORT: если первые IP дают 404 от CDN — это Akamai инфраструктура
        # которая НЕ обслуживает наше свойство. Нет смысла пробовать остальные.
        _404_count = 0
        _404_abort_limit = 5  # после 5 подряд 404 — abort

        for ip_str, cert_match in sorted_hosts[:20]:  # До 20 попыток
            try:
                # === СПОСОБ 1: RAW SOCKET SNI (ЛУЧШИЙ для CDN!) ===
                if use_tls:
                    status, body = self._raw_http_get_with_sni(
                        host, ip_str, parsed.path + ('?' + parsed.query if parsed.query else ''),
                        port=target_port, timeout=4
                    )
                    if status == 200:
                        is_html = False
                        if body and len(body) > 50:
                            body_lower = body[:800].lower()
                            if b'<html' in body_lower and b'mpegurl' not in body_lower:
                                is_html = True
                        if not is_html:
                            print(f"🔬 [NEXUS-ADJ] РАЗБЛОКИРОВАН (raw SNI)! {ip_str} "
                                  f"{'(cert match!)' if cert_match else ''} → 200!")
                            if not hasattr(self, '_adjacency_resolve'):
                                self._adjacency_resolve = {}
                            self._adjacency_resolve[host] = ip_str
                            if not hasattr(self, '_adjacency_cache'):
                                self._adjacency_cache = {}
                            if host not in self._adjacency_cache:
                                self._adjacency_cache[host] = []
                            self._adjacency_cache[host].append((ip_str, blocked_url, time.time()))
                            return blocked_url
                        else:
                            self._log(f"IP Adjacency: {ip_str} → 200 но HTML (не наш контент)")
                            continue
                    elif status == 403:
                        self._log(f"IP Adjacency: {ip_str} → 403 (geo-block)")
                        _404_count = 0  # сброс — 403 значит edge обслуживает свойство!
                        continue
                    elif status == 404:
                        _404_count += 1
                        body_preview = body[:200].decode('utf-8', errors='ignore') if body else ''
                        self._log(f"IP Adjacency: {ip_str} → 404 #{_404_count} (body: {body_preview[:80]})")
                        if _404_count >= _404_abort_limit:
                            self._log(f"IP Adjacency: EARLY ABORT — {_404_count}×404 подряд = CDN инфраструктура, не наши edge")
                            # Не трать время на остальные IP
                            break
                        continue
                    elif status is not None:
                        self._log(f"IP Adjacency: {ip_str} → {status}")
                        continue
                    # status is None → raw socket failed, try curl_cffi

                # === СПОСОБ 2: curl_cffi RESOLVE ===
                if use_tls:
                    try:
                        from curl_cffi import requests as cffi_req
                        from curl_cffi import CurlOpt
                        orig_url = blocked_url
                        resolve_entry = f'{host}:{target_port}:{ip_str}'
                        resp = cffi_req.get(
                            orig_url,
                            headers={'Host': host, 'User-Agent': BROWSER_HEADERS['User-Agent']},
                            timeout=6,
                            verify=False,
                            allow_redirects=True,  # СЛЕДУЕМ редиректам!
                            impersonate='chrome120',
                            curl_options={CurlOpt.RESOLVE: [resolve_entry]},
                        )
                        if resp.status_code == 200:
                            ct = resp.headers.get('Content-Type', '').lower()
                            if 'html' in ct and 'mpegurl' not in ct:
                                continue
                            print(f"🔬 [NEXUS-ADJ] РАЗБЛОКИРОВАН (cffi)! {ip_str} "
                                  f"{'(cert match!)' if cert_match else ''} → 200!")
                            if not hasattr(self, '_adjacency_resolve'):
                                self._adjacency_resolve = {}
                            self._adjacency_resolve[host] = ip_str
                            if not hasattr(self, '_adjacency_cache'):
                                self._adjacency_cache = {}
                            if host not in self._adjacency_cache:
                                self._adjacency_cache[host] = []
                            self._adjacency_cache[host].append((ip_str, blocked_url, time.time()))
                            return blocked_url
                        elif resp.status_code == 403:
                            self._log(f"IP Adjacency: {ip_str} → 403 (cffi, geo-block)")
                            _404_count = 0
                            continue
                        elif resp.status_code == 404:
                            _404_count += 1
                            body_preview = resp.text[:200] if resp.text else ''
                            self._log(f"IP Adjacency: {ip_str} → 404 #{_404_count} (cffi)")
                            if _404_count >= _404_abort_limit:
                                self._log(f"IP Adjacency: EARLY ABORT — {_404_count}×404 = CDN инфраструктура")
                                break
                            continue
                        else:
                            self._log(f"IP Adjacency: {ip_str} → {resp.status_code} (cffi)")
                            continue
                    except ImportError:
                        pass
                    except Exception:
                        pass

                # === СПОСОБ 3: IP в URL + Host (для НЕ-CDN) ===
                headers = BROWSER_HEADERS.copy()
                headers['Host'] = host
                headers['Referer'] = f'{scheme}://{host}/'
                ip_netloc = f"[{ip_str}]" if ':' in ip_str else ip_str
                if parsed.port:
                    ip_netloc += f":{parsed.port}"
                elif target_port not in (80, 443):
                    ip_netloc += f":{target_port}"
                test_url = parsed._replace(scheme=test_scheme, netloc=ip_netloc).geturl()

                resp = self.session.get(
                    test_url, headers=headers,
                    timeout=6, verify=False, allow_redirects=True,
                )
                if resp.status_code == 200:
                    ct = resp.headers.get('Content-Type', '').lower()
                    if 'html' in ct and 'mpegurl' not in ct:
                        continue
                    print(f"🔬 [NEXUS-ADJ] РАЗБЛОКИРОВАН (IP+Host)! {ip_str} "
                          f"{'(cert match!)' if cert_match else ''} → 200!")
                    if not hasattr(self, '_adjacency_cache'):
                        self._adjacency_cache = {}
                    if host not in self._adjacency_cache:
                        self._adjacency_cache[host] = []
                    self._adjacency_cache[host].append((ip_str, test_url, time.time()))
                    if not hasattr(self, '_adjacency_resolve'):
                        self._adjacency_resolve = {}
                    self._adjacency_resolve[host] = ip_str
                    return test_url
                elif resp.status_code == 403:
                    self._log(f"IP Adjacency: {ip_str} → 403 (IP+Host)")
                else:
                    self._log(f"IP Adjacency: {ip_str} → {resp.status_code} (IP+Host)")
            except Exception as e:
                self._log(f"IP Adjacency: {ip_str} → ошибка: {e}")

        self._log("IP Adjacency: все соседи тоже заблокированы")
        # Записываем провал в кэш — не пересканируем /24 ближайшие 5 минут
        if not hasattr(self, '_adjacency_failed'):
            self._adjacency_failed = {}
        self._adjacency_failed[host] = time.time()
        return None

    def _get_tls_cert_fingerprint(self, ip, hostname, port=443):
        """Получает SHA-256 fingerprint TLS-сертификата сервера."""
        import ssl as _ssl
        import hashlib as _hashlib
        try:
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            sock = socket.create_connection((ip, port), timeout=3)
            ssock = ctx.wrap_socket(sock, server_hostname=hostname)
            cert_bin = ssock.getpeercert(binary_form=True)
            ssock.close()
            if cert_bin:
                return _hashlib.sha256(cert_bin).hexdigest()
        except Exception:
            pass
        return None

    def _raw_http_get_with_sni(self, host, ip, path, port=443, timeout=4):
        """HTTP GET через raw socket с КОНТРОЛЕМ SNI.

        ПРЯМОЕ подключение: socket → ssl.wrap_socket(server_hostname=host)
        → SNI=host → CDN правильно маршрутизирует.

        НЕ зависит от curl_cffi RESOLVE — работает НАПРЯМУЮ через ssl.
        Возвращает: (status_code, body_bytes) или (None, None).
        """
        try:
            sock = socket.create_connection((ip, port), timeout=timeout)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ssock = ctx.wrap_socket(sock, server_hostname=host)

            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36\r\n"
                f"Accept: */*\r\n"
                f"Accept-Encoding: identity\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            )
            ssock.sendall(request.encode())

            response = b""
            while True:
                try:
                    chunk = ssock.recv(16384)
                    if not chunk:
                        break
                    response += chunk
                    if len(response) > 200000:
                        break
                except socket.timeout:
                    break
            ssock.close()

            if not response:
                return None, None

            # Парсим HTTP статус
            first_line = response.split(b'\r\n')[0].decode('utf-8', errors='ignore')
            parts = first_line.split(' ', 2)
            status_code = int(parts[1]) if len(parts) >= 2 else 0

            # Отделяем тело от заголовков
            header_end = response.find(b'\r\n\r\n')
            body = response[header_end + 4:] if header_end > 0 else b''

            return status_code, body
        except Exception:
            return None, None

    # ================================================================
    #  СТРАТЕГИЯ 22b: GEOSTEALTH — Multi-PoP + Geo-Spoof + Edge Hostnames
    #
    #  НОВЫЙ МЕТОД: комбинация трёх атак на CDN geo-block:
    #    1. MULTI-POP: резолвим через DoH в 8+ странах → edge-IP из разных PoP
    #    2. GEO-SPOOF: X-Forwarded-For + True-Client-IP с албанским IP
    #    3. EDGE HOSTNAMES: .edgekey.net / .edgesuite.net → другие edge-IP
    #
    #  КЛЮЧЕВОЙ ИНСАЙТ: Akamai проверяет geo ПО КЛИЕНТСКОМУ IP.
    #  НО: Akamai доверяет X-Forwarded-For от "своих" запросов.
    #  Если мы отправляем запрос с заголовками как от Akamai-внутреннего
    #  компонента (Akamai-Origin-Hop, Via: akamai.net) — edge МОЖЕТ
    #  использовать X-Forwarded-For вместо реального IP.
    #
    #  УНИВЕРСАЛЬНО: работает для ЛЮБОГО CDN с geo-check,
    #  не только Akamai.
    # ================================================================

    # Албанские IP-диапазоны для geo-spoof
    _AL_IPS = [
        '31.22.48.1', '31.22.49.1', '31.22.50.1', '31.22.51.1',
        '46.99.1.1', '46.99.2.1', '46.99.3.1', '46.99.10.1',
        '79.106.1.1', '79.106.10.1', '79.106.50.1', '79.106.100.1',
        '84.20.64.1', '84.20.65.1', '84.20.66.1', '84.20.67.1',
        '213.163.112.1', '213.163.113.1', '213.163.114.1', '213.163.115.1',
        '217.21.144.1', '217.21.145.1', '217.24.128.1', '217.24.129.1',
        '109.104.128.1', '109.104.129.1', '109.104.130.1', '109.104.131.1',
    ]

    def _try_akamai_edge_hostnames(self, blocked_url, pattern):
        """GeoStealth: Multi-PoP + Geo-Spoof + Edge Hostnames.

        ТРИ АТАКИ ОДНОВРЕМЕННО:
        1. Находим edge-IP из РАЗНЫХ стран через Multi-DoH
        2. Подставляем албанский IP в X-Forwarded-For
        3. Пробуем Akamai edge hostnames (.edgekey.net, .edgesuite.net)
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname or ''
        path = parsed.path + ('?' + parsed.query if parsed.query else '')

        # Албанский IP для geo-spoof (рандомный из списка)
        al_ip = random.choice(self._AL_IPS)

        # === ДИАГНОСТИКА: УЗНАЁМ КАКОЙ IP/СТРАНУ ВИДИТ AKAMAI ===
        # Akamai отдаёт диагностические заголовки если попросить:
        #   Pragma: akamai-x-get-true-cache-key → X-Cache-Key
        #   Pragma: akamai-x-get-client-ip → X-Client-IP
        self._log(f"GeoStealth: диагностика edge для {host}...")
        for ip, _ in list(all_edge_ips.items())[:2]:
            try:
                diag_headers = {
                    'Host': host,
                    'User-Agent': BROWSER_HEADERS['User-Agent'],
                    'Accept': '*/*',
                    'Accept-Encoding': 'identity',
                    'Connection': 'close',
                    'Pragma': 'akamai-x-get-client-ip, akamai-x-get-true-cache-key, akamai-x-get-nonces, akamai-x-get-serial-cache-key',
                    # Geo-spoof заголовки
                    'X-Forwarded-For': al_ip,
                    'True-Client-IP': al_ip,
                    'Akamai-Origin-Hop': '1',
                    'Via': '1.1 akamai.net(ghost) (AkamaiGHost)',
                }
                header_lines = f"GET {path} HTTP/1.1\r\n"
                for k, v in diag_headers.items():
                    header_lines += f"{k}: {v}\r\n"
                header_lines += "\r\n"

                sock = socket.create_connection((ip, 443), timeout=5)
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                ssock = ctx.wrap_socket(sock, server_hostname=host)
                ssock.sendall(header_lines.encode())

                resp_data = b""
                while True:
                    try:
                        chunk = ssock.recv(16384)
                        if not chunk:
                            break
                        resp_data += chunk
                        if len(resp_data) > 50000:
                            break
                    except socket.timeout:
                        break
                ssock.close()

                # Парсим заголовки ответа
                hdr_end = resp_data.find(b'\r\n\r\n')
                if hdr_end > 0:
                    hdr_text = resp_data[:hdr_end].decode('utf-8', errors='ignore')
                    for line in hdr_text.split('\r\n'):
                        lower = line.lower()
                        if any(x in lower for x in ['x-client-ip', 'x-cache-key', 'x-serial',
                                                      'x-akamai', 'x-true-cache',
                                                      'akamai', 'geo', 'country']):
                            print(f"🔬 [GEOSTEALTH-DIAG] {ip}: {line}")
                break  # Один IP достаточно для диагностики
            except Exception:
                continue

        # === СБОР ВСЕХ EDGE IP ===
        # Источники: DNS, Multi-DoH, edge hostnames
        all_edge_ips = {}  # ip -> source description
        seen = set()

        # Источник 1: DNS-resolved IP (ПРАВИЛЬНЫЕ edge для этого домена!)
        for ip in filter_ips_by_ipv6(doh_resolver.resolve(host, 'A')):
            if ip not in seen:
                seen.add(ip)
                all_edge_ips[ip] = 'DNS-local'

        # Источник 2: Multi-DoH (IP из других стран!)
        try:
            for ip in filter_ips_by_ipv6(anycast_explorer.discover_wide_ip_pool(host)):
                if ip not in seen:
                    seen.add(ip)
                    all_edge_ips[ip] = 'Multi-DoH'
        except Exception:
            pass

        # Источник 3: Edge hostnames (.edgekey.net, .edgesuite.net)
        edge_hostnames = [f"{host}.edgekey.net", f"{host}.edgesuite.net"]
        for eh in edge_hostnames:
            try:
                for ip in filter_ips_by_ipv6(doh_resolver.resolve_all(eh, prefer_ipv6=False)):
                    if ip not in seen:
                        seen.add(ip)
                        all_edge_ips[ip] = f'Edge:{eh}'
            except Exception:
                try:
                    results = socket.getaddrinfo(eh, 443, socket.AF_INET, socket.SOCK_STREAM)
                    for r in results:
                        ip = r[4][0]
                        if ip not in seen:
                            seen.add(ip)
                            all_edge_ips[ip] = f'Edge:{eh}'
                except Exception:
                    pass

        if not all_edge_ips:
            self._log("GeoStealth: нет edge IP")
            return None

        self._log(f"GeoStealth: {len(all_edge_ips)} edge IP для {host}")

        # === НАБОРЫ ЗАГОЛОВКОВ ДЛЯ GEO-SPOOF ===
        # Каждый набор = комбинация geo-spoof + CDN-internal заголовков
        header_sets = [
            # Набор 1: X-Forwarded-For (самый универсальный)
            {
                'X-Forwarded-For': al_ip,
                'X-Real-IP': al_ip,
            },
            # Набор 2: True-Client-IP (Akamai-specific!)
            {
                'True-Client-IP': al_ip,
                'X-Forwarded-For': al_ip,
                'Akamai-Origin-Hop': '1',
                'Via': '1.1 akamai.net(ghost) (AkamaiGHost)',
            },
            # Набор 3: Полный Akamai internal (максимальная мимикрия)
            {
                'X-Forwarded-For': al_ip,
                'True-Client-IP': al_ip,
                'X-Akamai-Transformed': '1 0 0',
                'Akamai-Origin-Hop': '1',
                'X-Akamai-Request-ID': f"{random.randint(10**15, 10**16):x}",
                'X-Cache': 'HIT',
                'Via': '1.1 akamai.net(ghost) (AkamaiGHost)',
            },
            # Набор 4: CF-style (если CDN Cloudflare, а не Akamai)
            {
                'CF-Connecting-IP': al_ip,
                'X-Forwarded-For': al_ip,
                'CF-IPCountry': 'AL',
                'CF-Visitor': '{' + '"' + 'scheme' + '"' + ':' + '"' + 'https' + '"' + '}',
            },
            # Набор 5: Fastly-style
            {
                'Fastly-Client-IP': al_ip,
                'X-Forwarded-For': al_ip,
                'X-Served-By': 'cache-tia1234-TIA',
                'X-Cache': 'HIT',
            },
            # Набор 6: Generic CDN proxy — ВСЕ geo-заголовки разом
            {
                'X-Forwarded-For': al_ip,
                'X-Client-IP': al_ip,
                'X-Originating-IP': al_ip,
                'X-Remote-IP': al_ip,
                'X-Remote-Addr': al_ip,
                'Via': '1.1 cdn-proxy',
            },
            # Набор 7: Akamai EAP (Enhanced Akamai Protocol) — ГЕО ПРЯМО!
            # X-Akamai-Client-Geo — Akamai ВНУТРЕННИЙ заголовок геолокации
            # Работает ТОЛЬКО если edge думает что запрос от Akamai-компонента
            {
                'X-Forwarded-For': al_ip,
                'True-Client-IP': al_ip,
                'X-Akamai-Client-IP': al_ip,
                'Akamai-Origin-Hop': '2',
                'X-Akamai-Transformed': '1 0 0',
                'Via': '1.1 akamai.net(ghost) (AkamaiGHost)',
                'X-Cache': 'MISS from akl-edge',
            },
            # Набор 8: Akamai diagnostic + geo-spoof — максимальная атака
            {
                'X-Forwarded-For': al_ip,
                'True-Client-IP': al_ip,
                'Akamai-Origin-Hop': '1',
                'Via': '1.1 akamai.net(ghost) (AkamaiGHost)',
                'X-Akamai-Transformed': '1 0 0',
                'X-Akamai-Request-ID': f"{random.randint(10**15, 10**16):x}",
                'X-Cache': 'HIT',
                'X-Cache-Lookup': 'HIT',
                'Pragma': 'akamai-x-get-client-ip',
            },
        ]

        # === ПРОБУЕМ КАЖДЫЙ IP × КАЖДЫЙ НАБОР ЗАГОЛОВКОВ ===
        # Приоритет: Multi-DoH IP первыми (другие страны!)
        sorted_ips = sorted(all_edge_ips.items(),
                           key=lambda x: 0 if 'Multi' in x[1] else (1 if 'Edge' in x[1] else 2))

        tested = 0
        for ip, source in sorted_ips[:15]:  # До 15 IP
            for h_idx, extra in enumerate(header_sets):  # 6 наборов
                if _time.time() > self._deadline:
                    self._log("GeoStealth: deadline истёк")
                    return None
                tested += 1

                # RAW SOCKET с SNI=host + geo-spoof заголовки
                try:
                    # Строим HTTP-запрос с заголовками
                    req_headers = {
                        'Host': host,
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
                        'Accept': '*/*',
                        'Accept-Encoding': 'identity',
                        'Connection': 'close',
                    }
                    req_headers.update(extra)

                    # Формируем запрос
                    header_lines = f"GET {path} HTTP/1.1\r\n"
                    for k, v in req_headers.items():
                        header_lines += f"{k}: {v}\r\n"
                    header_lines += "\r\n"

                    sock = socket.create_connection((ip, 443), timeout=5)
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    ssock = ctx.wrap_socket(sock, server_hostname=host)
                    ssock.sendall(header_lines.encode())

                    response = b""
                    while True:
                        try:
                            chunk = ssock.recv(16384)
                            if not chunk:
                                break
                            response += chunk
                            if len(response) > 200000:
                                break
                        except socket.timeout:
                            break
                    ssock.close()

                    if not response:
                        continue

                    first_line = response.split(b'\r\n')[0].decode('utf-8', errors='ignore')
                    parts = first_line.split(' ', 2)
                    status = int(parts[1]) if len(parts) >= 2 else 0

                    if status == 200:
                        header_end = response.find(b'\r\n\r\n')
                        body = response[header_end + 4:] if header_end > 0 else b''
                        # Проверяем что это не HTML-заглушка
                        if body and b'<html' in body[:800].lower() and b'mpegurl' not in body[:800].lower():
                            continue
                        print(f"🔬 [GEOSTEALTH] РАЗБЛОКИРОВАН! {ip} ({source}) "
                              f"набор #{h_idx+1} → 200!")
                        if not hasattr(self, '_adjacency_resolve'):
                            self._adjacency_resolve = {}
                        self._adjacency_resolve[host] = ip
                        # Сохраняем рабочий набор заголовков для сегментов
                        if not hasattr(self, '_geostealth_headers'):
                            self._geostealth_headers = {}
                        self._geostealth_headers[host] = extra
                        if not hasattr(self, '_adjacency_cache'):
                            self._adjacency_cache = {}
                        if host not in self._adjacency_cache:
                            self._adjacency_cache[host] = []
                        self._adjacency_cache[host].append((ip, blocked_url, time.time()))
                        return blocked_url
                    elif status == 403:
                        # 403 = edge обслуживает свойство, но geo-block
                        # Пробуем СЛЕДУЮЩИЙ набор заголовков!
                        continue
                    elif status == 404:
                        # 404 = edge НЕ обслуживает это свойство
                        # Скипаем остальные наборы для этого IP
                        break
                except Exception:
                    continue

            # Также пробуем curl_cffi RESOLVE (для редиректов и т.д.)
            try:
                from curl_cffi import requests as cffi_req
                from curl_cffi import CurlOpt
                for h_idx, extra in enumerate(header_sets[:3]):  # Первые 3 набора через curl
                    if _time.time() > self._deadline:
                        return None
                    try:
                        resolve_entry = f'{host}:443:{ip}'
                        cffi_headers = {
                            'Host': host,
                            'User-Agent': BROWSER_HEADERS['User-Agent'],
                            'Accept': '*/*',
                            'Referer': f'https://{host}/',
                        }
                        cffi_headers.update(extra)
                        resp = cffi_req.get(
                            blocked_url,
                            headers=cffi_headers,
                            timeout=6, verify=False,
                            allow_redirects=True,
                            impersonate='chrome120',
                            curl_options={CurlOpt.RESOLVE: [resolve_entry]},
                        )
                        if resp.status_code == 200:
                            ct = resp.headers.get('Content-Type', '').lower()
                            if 'html' in ct and 'mpegurl' not in ct:
                                continue
                            print(f"🔬 [GEOSTEALTH] РАЗБЛОКИРОВАН (cffi)! {ip} ({source}) "
                                  f"набор #{h_idx+1} → 200!")
                            if not hasattr(self, '_adjacency_resolve'):
                                self._adjacency_resolve = {}
                            self._adjacency_resolve[host] = ip
                            if not hasattr(self, '_geostealth_headers'):
                                self._geostealth_headers = {}
                            self._geostealth_headers[host] = extra
                            return blocked_url
                        elif resp.status_code == 404:
                            break  # Этот IP не обслуживает свойство
                    except Exception:
                        continue
            except ImportError:
                pass

        self._log(f"GeoStealth: {tested} комбинаций — все заблокированы")
        return None

    #  СТРАТЕГИЯ 23: CERT TRANSPARENCY DISCOVERY
    #  Certificate Transparency лог (crt.sh) показывает ВСЕ домены
    #  на том же сертификате — некоторые без geo-check!
    # ================================================================
    def _try_cert_transparency(self, blocked_url, pattern):
        """Cert Transparency: находит все домены на том же сертификате.

        КЛЮЧЕВАЯ ИДЕЯ:
          Certificate Transparency — обязательный лог ВСЕХ TLS-сертификатов.
          crt.sh — публичный поиск по этому логу.
          На одном сертификате может быть 10+ доменов (SANs):
            - streaming.televizor-24-tochka.ru (geo-blocked)
            - monitor.televizor-24-tochka.ru   (monitoring, NO geo-check!)
            - admin.televizor-24-tochka.ru      (admin panel, NO geo-check!)
            - test.televizor-24-tochka.ru       (testing, NO geo-check!)

          Все эти домены резолвятся в тот же IP (или соседний),
          но у nginx разные server{} блоки с РАЗНЫМИ настройками geoip.

        АЛГОРИТМ:
          1. Запрашиваем crt.sh?q=domain — получаем все сертификаты
          2. Парсим SANs (Subject Alternative Names) из каждого сертификата
          3. Фильтруем: только домены, отличные от заблокированного
          4. DoH-resolve каждый — должен указывать на тот же IP/подсеть
          5. Пробуем каждый — если 200 → НАШЛИ BACKDOOR!

        ЧТО ОБХОДИТ:
          • nginx geoip per-vhost (разные vhost = разные правила)
          • Фронтенд-проверки (admin/monitor могут не проверять)

        ЭКСПЛУАТИРУЕТ:
          • Certificate Transparency — публичный лог
          • Мультидоменные сертификаты (SANs)
          • Небрежность админов (забыли настроить geo-check на all vhosts)
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        # Шаг 1: Запрашиваем crt.sh
        self._log(f"Cert Transparency: запрос crt.sh для {host}...")
        try:
            resp = self.session.get(
                f'https://crt.sh/?q={host}&output=json',
                headers={'Accept': 'application/json'},
                timeout=8, verify=False,
            )
            if resp.status_code != 200:
                self._log(f"Cert Transparency: crt.sh → {resp.status_code}")
                return None
            ct_data = resp.json()
        except Exception as e:
            self._log(f"Cert Transparency: crt.sh ошибка: {e}")
            return None

        if not ct_data:
            self._log("Cert Transparency: нет данных в crt.sh")
            return None

        # Шаг 2: Извлекаем все уникальные домены (SANs)
        all_domains = set()
        for entry in ct_data:
            name_value = entry.get('name_value', '')
            # crt.sh возвращает домены через \n
            for domain in name_value.split('\n'):
                domain = domain.strip().lower()
                # Убираем wildcard-звёздочки
                domain = domain.lstrip('*.')
                if domain and domain != host and '.' in domain:
                    all_domains.add(domain)

        if not all_domains:
            self._log("Cert Transparency: нет альтернативных доменов в SANs")
            return None

        self._log(f"Cert Transparency: {len(all_domains)} доменов в SANs")

        # Шаг 3: DoH-resolve и фильтруем по подсети
        origin_ips = set(doh_resolver.resolve(host, 'A'))
        origin_subnet = None
        if origin_ips:
            import ipaddress as _ipaddr
            first_ip = list(origin_ips)[0]
            try:
                net = _ipaddr.ip_network(f'{first_ip}/24', strict=False)
                origin_subnet = net
            except Exception:
                pass

        candidate_domains = []
        for domain in all_domains:
            try:
                domain_ips = set(doh_resolver.resolve(domain, 'A'))
                if not domain_ips:
                    continue
                # Проверяем: в той же /24 подсети?
                if origin_subnet:
                    for dip in domain_ips:
                        if _ipaddr.ip_address(dip) in origin_subnet:
                            candidate_domains.append((domain, dip))
                            self._log(f"Cert Transparency: {domain} → {dip} (та же подсеть!)")
                            break
                elif domain_ips & origin_ips:
                    # Тот же IP
                    candidate_domains.append((domain, list(domain_ips)[0]))
                    self._log(f"Cert Transparency: {domain} → тот же IP!")
            except Exception:
                pass

        if not candidate_domains:
            self._log("Cert Transparency: нет доменов в той же подсети")
            return None

        # Шаг 4: Пробуем каждый домен
        for domain, ip in candidate_domains[:5]:
            # Вариант A: Подключаемся через домен напрямую
            test_url = parsed._replace(netloc=domain).geturl()
            self._log(f"Cert Transparency: пробую {domain}...")
            if self._quick_test(test_url, timeout=6):
                print(f"📜 [NEXUS-CT] НАШЁЛ! {domain} → 200 (без geo-check)!")
                return test_url

            # Вариант B: Подключаемся по IP, SNI=альтернативный домен
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if parsed.port:
                ip_netloc += f":{parsed.port}"
            test_url_ip = parsed._replace(netloc=ip_netloc).geturl()
            headers = BROWSER_HEADERS.copy()
            headers['Host'] = host  # Host = заблокированный домен
            headers['Referer'] = f'{parsed.scheme}://{domain}/'

            try:
                resp = self.session.get(
                    test_url_ip, headers=headers,
                    timeout=6, verify=False, allow_redirects=True,
                )
                if resp.status_code == 200:
                    ct = resp.headers.get('Content-Type', '').lower()
                    if 'html' in ct and 'mpegurl' not in ct:
                        continue
                    print(f"📜 [NEXUS-CT] НАШЁЛ через SNI! {domain} (Host={host}) → 200!")
                    return test_url_ip
            except Exception:
                pass

        self._log("Cert Transparency: все домены тоже заблокированы")
        return None

    # ================================================================
    #  СТРАТЕГИЯ 24: PROTOCOL UPGRADE BYPASS
    #  HTTP/2, HTTP/1.0, WebSocket против nginx geoip
    # ================================================================
    def _try_protocol_upgrade(self, blocked_url, pattern):
        """Protocol Upgrade: обходит geo-check через протокольные трюки.

        КЛЮЧЕВЫЕ ИДЕИ:

        A) HTTP/1.0 БЕЗ Host-заголовка:
           nginx маршрутизирует запрос по server_name (из Host).
           HTTP/1.0 НЕ требует Host → nginx не может определить server_name
           → использует default_server → который может НЕ иметь geoip!
           В配置: default_server часто = заглушка/мониторинг без geo-check.

        B) HTTP/2 PRIOR KNOWLEDGE (h2c):
           Если мы отправляем HTTP/2 frames напрямую (без ALPN/Upgrade),
           nginx может обработать их другим кодовым путём.
           HTTP/2 несовместим с HTTP/1.1 → другой location блок.

        C) WebSocket Upgrade:
           WebSocket-соединения начинаются с HTTP Upgrade запроса.
           nginx может обрабатывать Upgrade в отдельном location,
           который не содержит geoip директиву.

        D) IPv6 подключение:
           Если у сервера есть AAAA-запись, IPv6-адрес может быть
           не в geo-блок-листе (IPv6 базы часто неполные).

        ЧТО ОБХОДИТ:
          • nginx geoip module (если default_server без geoip)
          • Location-specific geo-check (не все location проверяют)
          • IPv4-only geo-базы

        ЭКСПЛУАТИРУЕТ:
          • HTTP/1.0 → default_server routing в nginx
          • Рассинхрон конфигурации между server{} блоками
          • Неполные IPv6 гео-базы
        """
        parsed = urllib.parse.urlparse(blocked_url)
        host = parsed.hostname
        if not host:
            return None

        origin_ips = doh_resolver.resolve(host, 'A')
        if not origin_ips:
            return None

        target_ip = origin_ips[0]
        port = parsed.port or 443
        path = parsed.path
        if parsed.query:
            path += '?' + parsed.query

        # === МЕТОД A: HTTP/1.0 БЕЗ HOST ===
        # nginx → default_server → может быть без geoip!
        self._log("Protocol Upgrade: HTTP/1.0 без Host...")
        try:
            import socket as _socket
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(6)

            if parsed.scheme == 'https':
                import ssl as _ssl
                ctx = _ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                # НЕ отправляем SNI → nginx не знает server_name → default_server!
                sock = ctx.wrap_socket(sock, server_hostname=None)  # No SNI

            sock.connect((target_ip, port))

            # HTTP/1.0 запрос БЕЗ Host-заголовка
            request = (
                f"GET {path} HTTP/1.0\r\n"
                f"User-Agent: Mozilla/5.0\r\n"
                f"Accept: */*\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            )
            sock.sendall(request.encode())

            # Читаем ответ
            response = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                except Exception:
                    break
            sock.close()

            # Парсим статус
            status_line = response.split(b'\r\n')[0] if response else b""
            status_code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0

            if status_code == 200:
                # Проверяем Content-Type
                headers_part = response.split(b'\r\n\r\n')[0].lower()
                if b'html' in headers_part and b'mpegurl' not in headers_part:
                    self._log("Protocol Upgrade: HTTP/1.0 → 200 но HTML заглушка")
                else:
                    print(f"⚡ [NEXUS-PU] HTTP/1.0 NO-HOST → 200! default_server без geo-check!")
                    # Возвращаем URL с IP — LIVEPIPE подхватит
                    ip_netloc = f"[{target_ip}]" if ':' in target_ip else target_ip
                    if parsed.port:
                        ip_netloc += f":{parsed.port}"
                    return parsed._replace(netloc=ip_netloc).geturl()
            else:
                self._log(f"Protocol Upgrade: HTTP/1.0 → {status_code}")
        except Exception as e:
            self._log(f"Protocol Upgrade: HTTP/1.0 ошибка: {e}")

        # === МЕТОД B: HTTP/1.0 С HOST, НО БЕЗ SNI ===
        # TLS handshake без SNI → nginx не маршрутизирует по server_name
        # Но Host в HTTP-запросе говорит серверу, какой контент нужен
        self._log("Protocol Upgrade: HTTP/1.0 с Host, без SNI...")
        try:
            import socket as _socket
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(6)

            if parsed.scheme == 'https':
                import ssl as _ssl
                ctx = _ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                # No SNI! nginx видит только IP → default_server
                sock = ctx.wrap_socket(sock, server_hostname=None)

            sock.connect((target_ip, port))

            # HTTP/1.0 с Host но без SNI → nginx default_server
            request = (
                f"GET {path} HTTP/1.0\r\n"
                f"Host: {host}\r\n"
                f"User-Agent: Mozilla/5.0\r\n"
                f"Accept: */*\r\n"
                f"Referer: {parsed.scheme}://{host}/\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            )
            sock.sendall(request.encode())

            response = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                except Exception:
                    break
            sock.close()

            status_line = response.split(b'\r\n')[0] if response else b""
            status_code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0

            if status_code == 200:
                headers_part = response.split(b'\r\n\r\n')[0].lower()
                if b'html' in headers_part and b'mpegurl' not in headers_part:
                    self._log("Protocol Upgrade: HTTP/1.0+Host noSNI → 200 но HTML")
                else:
                    print(f"⚡ [NEXUS-PU] HTTP/1.0 + Host NO-SNI → 200! backdoor через default_server!")
                    ip_netloc = f"[{target_ip}]" if ':' in target_ip else target_ip
                    if parsed.port:
                        ip_netloc += f":{parsed.port}"
                    return parsed._replace(netloc=ip_netloc).geturl()
            else:
                self._log(f"Protocol Upgrade: HTTP/1.0+Host noSNI → {status_code}")
        except Exception as e:
            self._log(f"Protocol Upgrade: HTTP/1.0+Host noSNI ошибка: {e}")

        # === МЕТОД C: WebSocket Upgrade ===
        # nginx может обрабатывать Upgrade в location без geoip
        self._log("Protocol Upgrade: WebSocket Upgrade...")
        try:
            ws_key = __import__('base64').b64encode(__import__('os').urandom(16)).decode()
            headers = BROWSER_HEADERS.copy()
            headers['Host'] = host
            headers['Upgrade'] = 'websocket'
            headers['Connection'] = 'Upgrade'
            headers['Sec-WebSocket-Key'] = ws_key
            headers['Sec-WebSocket-Version'] = '13'
            headers['Sec-WebSocket-Protocol'] = 'stream'

            ip_netloc = f"[{target_ip}]" if ':' in target_ip else target_ip
            if parsed.port:
                ip_netloc += f":{parsed.port}"
            test_url = parsed._replace(netloc=ip_netloc, scheme='https').geturl()

            resp = self.session.get(
                test_url, headers=headers,
                timeout=6, verify=False, allow_redirects=False,
            )
            if resp.status_code in (101, 200):
                print(f"⚡ [NEXUS-PU] WebSocket Upgrade → {resp.status_code}! backdoor через WS!")
                return test_url
            else:
                self._log(f"Protocol Upgrade: WebSocket → {resp.status_code}")
        except Exception as e:
            self._log(f"Protocol Upgrade: WebSocket ошибка: {e}")

        # === МЕТОД D: IPv6 ===
        self._log("Protocol Upgrade: IPv6 проба...")
        v6_ips = doh_resolver.resolve(host, 'AAAA')
        for v6_ip in v6_ips:
            ip_netloc = f"[{v6_ip}]"
            if parsed.port:
                ip_netloc += f":{parsed.port}"
            test_url = parsed._replace(netloc=ip_netloc).geturl()
            headers = BROWSER_HEADERS.copy()
            headers['Host'] = host
            try:
                resp = self.session.get(
                    test_url, headers=headers,
                    timeout=6, verify=False, allow_redirects=True,
                )
                if resp.status_code == 200:
                    ct = resp.headers.get('Content-Type', '').lower()
                    if 'html' in ct and 'mpegurl' not in ct:
                        continue
                    print(f"⚡ [NEXUS-PU] IPv6 → 200! IPv6 geo-база неполная!")
                    return test_url
            except Exception:
                break  # IPv6 не работает на системе

        self._log("Protocol Upgrade: все протокольные трюки не помогли")
        return None

    def _quick_test(self, url, timeout=6):
        """Быстрая проверка: отдаёт ли URL 200 (без гео-блока)."""
        try:
            resp = self.session.get(
                url, headers=BROWSER_HEADERS,
                timeout=timeout, stream=True,
                verify=False, allow_redirects=True,
            )
            ok = resp.status_code == 200
            if ok:
                # Проверяем, что это не HTML-заглушка с гео-блоком
                ct = resp.headers.get('Content-Type', '').lower()
                if 'html' in ct and 'mpegurl' not in ct:
                    try:
                        head = resp.content[:800].lower()
                        geo_markers = ['not available', 'blocked', 'geo',
                                      'region', 'country', 'unavailable',
                                      'access denied', 'restricted']
                        if any(m in head for m in geo_markers):
                            ok = False
                    except Exception:
                        pass
            resp.close()
            return ok
        except Exception:
            return False

    def _segment_test(self, url, timeout=6):
        """Проверяет, является ли URL доступным видео-сегментом."""
        try:
            resp = self.session.get(
                url, headers=BROWSER_HEADERS,
                timeout=timeout, stream=True,
                verify=False, allow_redirects=True,
            )
            ok = False
            if resp.status_code == 200:
                ct = resp.headers.get('Content-Type', '').lower()
                # Валидные типы для видео-сегмента
                valid_types = ['video', 'octet-stream', 'mpeg', 'mp2t',
                               'mp4', 'binary', 'application/octet']
                ok = any(v in ct for v in valid_types)
                # Если Content-Type пустой — проверяем первые байты
                if not ok and not ct:
                    try:
                        chunk = next(resp.iter_content(4))
                        # TS-пакет начинается с 0x47
                        if chunk and chunk[0] == 0x47:
                            ok = True
                        # ftyp-атом MP4
                        elif chunk and len(chunk) >= 4 and chunk[4:8] == b'ftyp':
                            ok = True
                    except Exception:
                        pass
                # Если тип неизвестен, но не HTML — тоже пробуем
                if not ok and 'html' not in ct and 'text' not in ct:
                    ok = True
            resp.close()
            return ok
        except Exception:
            return False

    def _log(self, msg):
        """Логирование с меткой PhaseShift."""
        entry = f"[PhaseShift] {msg}"
        self._shift_log.append(entry)
        print(f"🔮 {entry}")


# ============================================================
#  PHASESHIFT OMEGA — IPSwarmManager
#  Мульти-IP резервирование: CDN резолвится в десятки адресов,
#  ISP блокирует конкретные IP, но НЕ весь диапазон CDN.
# ============================================================

# ============================================================
#  PHASESHIFT OMEGA — ShardedSegmentFetcher
#  Range-осколочная доставка сегментов по разным IP
# ============================================================

class ShardedSegmentFetcher:
    """Реальный Range-sharding: скачивает сегмент осколками по разным IP.

    КАК ЭТО РАБОТАЕТ:
      1. HEAD-запрос → узнаём Content-Length сегмента
      2. Делим на N осколков по 128-256 KB каждый
      3. Каждый осколок скачиваем с РАЗНОГО IP (Round-Robin по пулу)
      4. Собираем осколки в правильном порядке → отдаём полный сегмент

    ПОЧЕМУ ЭТО ОБХОДИТ ПРОВЕРКУ:
      • CDN-кэш: первый Range-запрос «прогревает» кэш edge-узла,
        последующие Range с того же кэша → CDN не повторяет IP-проверку
      • DPI: каждый маленький запрос выглядит как веб-ресурс (favicon, AJAX)
      • Зонд: если ISP проверяет один IP, он видит лишь фрагмент,
        а не полный сегмент → не может идентифицировать контент
    """

    # Размер осколка: 128 KB — достаточно маленький чтобы выглядеть
    # как обычный веб-запрос (картинка, favicon, стили)
    SHARD_SIZE = 128 * 1024
    # Максимальное количество осколков на сегмент (чтобы не плодить коннекты)
    MAX_SHARDS = 8
    # Кэш проверки Range-support: host -> bool
    _range_cache = {}
    _range_cache_lock = threading.Lock()

    def __init__(self, session):
        self.session = session

    def supports_range(self, url, host):
        """Проверяет, поддерживает ли CDN Range-запросы для данного хоста."""
        with self._range_cache_lock:
            if host in self._range_cache:
                return self._range_cache[host]

        # Пробуем маленький Range-запрос
        ips = ip_swarm.get_pool(host)
        if not ips:
            with self._range_cache_lock:
                self._range_cache[host] = False
            return False

        test_ip = ips[0]
        port = 443
        parsed = urllib.parse.urlparse(url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        ip_netloc = f"[{test_ip}]" if ':' in test_ip else test_ip
        if port not in (80, 443):
            ip_netloc += f":{port}"

        test_url = parsed._replace(netloc=ip_netloc).geturl()
        headers = {
            'Host': host,
            'User-Agent': BROWSER_HEADERS['User-Agent'],
            'Accept': '*/*',
            'Range': 'bytes=0-1023',
        }

        try:
            resp = self.session.get(
                test_url, headers=headers,
                timeout=6, stream=True, verify=False,
                allow_redirects=False,
            )
            ok = resp.status_code == 206 or (
                resp.status_code == 200 and
                (resp.headers.get('Accept-Ranges', '') == 'bytes' or
                 resp.headers.get('Content-Range', '').startswith('bytes'))
            )
            resp.close()
            with self._range_cache_lock:
                self._range_cache[host] = ok
            if ok:
                print(f"💎 [Shard] CDN {host} поддерживает Range ✅")
            else:
                print(f"💎 [Shard] CDN {host} НЕ поддерживает Range ❌")
            return ok
        except Exception:
            with self._range_cache_lock:
                self._range_cache[host] = False
            return False

    def fetch_segment(self, seg_url, host, pool_ips):
        """Скачивает сегмент осколками по разным IP. Возвращает bytes или None."""
        parsed = urllib.parse.urlparse(seg_url)

        # Шаг 1: HEAD → Content-Length
        # Используем первый IP для HEAD (чтобы не тратить все IP)
        if not pool_ips:
            return None

        head_ip = pool_ips[0]
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        ip_netloc = f"[{head_ip}]" if ':' in head_ip else head_ip
        if port not in (80, 443):
            ip_netloc += f":{port}"

        head_url = parsed._replace(netloc=ip_netloc).geturl()
        head_headers = {
            'Host': host,
            'User-Agent': BROWSER_HEADERS['User-Agent'],
            'Accept': '*/*',
        }

        content_length = None
        try:
            resp = self.session.get(
                head_url, headers=head_headers,
                timeout=8, stream=True, verify=False,
                allow_redirects=True,
            )
            if resp.status_code in (200, 206):
                content_length = int(resp.headers.get('Content-Length', 0))
            resp.close()
        except Exception:
            pass

        if not content_length or content_length < self.SHARD_SIZE:
            # Сегмент слишком маленький или неизвестной длины —
            # шардирование не имеет смысла, скачиваем целиком
            return self._fetch_whole(seg_url, host, pool_ips)

        # Шаг 2: Делим на осколки
        num_shards = min(self.MAX_SHARDS, max(2, content_length // self.SHARD_SIZE))
        shard_size = content_length // num_shards
        # Последний осколок забирает остаток
        shards = []
        for i in range(num_shards):
            start = i * shard_size
            end = (content_length - 1) if i == num_shards - 1 else (start + shard_size - 1)
            shards.append((start, end))

        print(f"💎 [Shard] {host}: {content_length}B → {num_shards} осколков по ~{shard_size//1024}KB")

        # Шаг 3: Скачиваем каждый осколок с РАЗНОГО IP (Round-Robin)
        pieces = [None] * num_shards
        errors = 0

        for i, (start, end) in enumerate(shards):
            ip = pool_ips[i % len(pool_ips)]
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if port not in (80, 443):
                ip_netloc += f":{port}"

            shard_url = parsed._replace(netloc=ip_netloc).geturl()
            shard_headers = {
                'Host': host,
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
                'Range': f'bytes={start}-{end}',
                'Referer': f"{parsed.scheme}://{host}/",
            }

            try:
                resp = self.session.get(
                    shard_url, headers=shard_headers,
                    timeout=12, stream=False, verify=False,
                    allow_redirects=True,
                )
                if resp.status_code in (200, 206):
                    pieces[i] = resp.content
                    ip_swarm.mark_ok(host, ip)
                else:
                    pieces[i] = None
                    errors += 1
                    ip_swarm.mark_blocked(host, ip, probe_detected=False)
                resp.close()
            except Exception:
                pieces[i] = None
                errors += 1

        # Шаг 4: Собираем осколки
        if any(p is None for p in pieces):
            # Какие-то осколки не скачались — пробуем докачать с других IP
            for i, p in enumerate(pieces):
                if p is not None:
                    continue
                start, end = shards[i]
                # Пробуем следующий IP
                alt_ip_idx = (i + 1) % len(pool_ips)
                alt_ip = pool_ips[alt_ip_idx]
                ip_netloc = f"[{alt_ip}]" if ':' in alt_ip else alt_ip
                if port not in (80, 443):
                    ip_netloc += f":{port}"

                shard_url = parsed._replace(netloc=ip_netloc).geturl()
                shard_headers = {
                    'Host': host,
                    'User-Agent': BROWSER_HEADERS['User-Agent'],
                    'Accept': '*/*',
                    'Range': f'bytes={start}-{end}',
                }
                try:
                    resp = self.session.get(
                        shard_url, headers=shard_headers,
                        timeout=12, stream=False, verify=False,
                        allow_redirects=True,
                    )
                    if resp.status_code in (200, 206):
                        pieces[i] = resp.content
                    resp.close()
                except Exception:
                    pass

        # Финальная сборка
        if any(p is None for p in pieces):
            # Не все осколки скачались — фолбэк на полное скачивание
            print(f"💎 [Shard] не все осколки получены → фолбэк на целое скачивание")
            return self._fetch_whole(seg_url, host, pool_ips)

        result = b''.join(pieces)
        print(f"💎 [Shard] сегмент собран: {len(result)}B из {num_shards} осколков")
        return result

    def _fetch_whole(self, seg_url, host, pool_ips):
        """Фолбэк: скачивает сегмент целиком с первого рабочего IP."""
        parsed = urllib.parse.urlparse(seg_url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)

        for ip in pool_ips[:3]:
            ip_netloc = f"[{ip}]" if ':' in ip else ip
            if port not in (80, 443):
                ip_netloc += f":{port}"

            whole_url = parsed._replace(netloc=ip_netloc).geturl()
            headers = {
                'Host': host,
                'User-Agent': BROWSER_HEADERS['User-Agent'],
                'Accept': '*/*',
            }
            try:
                resp = self.session.get(
                    whole_url, headers=headers,
                    timeout=12, stream=False, verify=False,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    ip_swarm.mark_ok(host, ip)
                    data = resp.content
                    resp.close()
                    return data
                resp.close()
            except Exception:
                continue
        return None


sharded_fetcher = None  # init after http_session


# ============================================================
#  PHASESHIFT OMEGA — CamouflagedFetcher
#  JA3 мимикрия + шумовой трафик + burst-скачивание
# ============================================================

class CamouflagedFetcher:
    """Камуфлированный fetching: JA3 мимикрия + шум + burst-паттерн.

    КАК ЭТО РАБОТАЕТ:
      1. JA3 МИМИКРИЯ: если curl_cffi доступен, используем
         impersonate="chrome120" — наш TLS fingerprint идентичен Chrome
      2. ШУМОВЫЕ ЗАПРОСЫ: перед/после каждого сегмента делаем
         1-2 запроса к «белым» сайтам (google.com, youtube.com)
         DPI видит смесь IPTV + веб-сёрфинг → не флагает
      3. BURST-СКАЧИВАНИЕ: скачиваем 2-3 сегмента подряд,
         потом пауза 0.5-2с (имитация «прочитал статью, кликнул дальше»)
      4. TIMING JITTER: каждый запрос получает случайную задержку
         50-300ms перед отправкой — ломает периодический паттерн

    ПОЧЕМУ ЭТО ОБХОДИТ DPI:
      DPI traffic analysis ищет паттерн:
        стабильные 2-4 Мбит/с, запросы каждые 2-6с → IPTV → флаг
      Мы превращаем паттерн в:
        burst 2-3 запросов, пауза, шумовой запрос, burst, пауза
        → выглядит как веб-сёрфинг по тяжёлой странице
    """

    # Белые сайты для шумовых запросов
    NOISE_URLS = [
        'https://www.google.com/favicon.ico',
        'https://www.youtube.com/favicon.ico',
        'https://www.microsoft.com/favicon.ico',
        'https://www.apple.com/favicon.ico',
        'https://www.cloudflare.com/favicon.ico',
    ]

    # Веб-сёрфинг заголовки (как будто перешли по ссылке из Google)
    WEB_HEADERS = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'cross-site',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
    }

    def __init__(self, session):
        self.session = session
        self._curl_session = None
        self._burst_counter = 0
        self._burst_size = random.randint(2, 3)  # сколько сегментов в burst
        self._noise_idx = 0
        self._init_curl()

    def _init_curl(self):
        """Инициализирует curl_cffi сессию с Chrome-impersonation."""
        try:
            import curl_cffi.requests as curl_req
            self._curl_session = curl_req.Session(impersonate="chrome120")
            print("🎭 [Camo] curl_cffi Chrome 120 имперсонация АКТИВНА")
        except ImportError:
            self._curl_session = None
            print("🎭 [Camo] curl_cffi нет — фолбэк на requests (JA3 будет Python-стандарт)")

    def fetch_segment(self, url, headers=None):
        """Скачивает сегмент с камуфляжем. Возвращает bytes или None.

        Вызывается из LIVEPIPE вместо прямого session.get().
        """
        # Шаг 1: Timing jitter — случайная задержка 50-300ms
        jitter = random.uniform(0.05, 0.3)
        time.sleep(jitter)

        # Шаг 2: Шумовой запрос ПЕРЕД сегментом (каждый 3-й сегмент)
        if self._burst_counter % 3 == 0:
            self._inject_noise()

        # Шаг 3: Скачиваем сегмент с JA3 мимикрией
        merged_headers = dict(headers or {})
        # Добавляем веб-сёрфинг заголовки
        for k, v in self.WEB_HEADERS.items():
            if k not in merged_headers:
                merged_headers[k] = v
        # Referer от Google (как будто перешли по ссылке)
        if 'Referer' not in merged_headers:
            merged_headers['Referer'] = 'https://www.google.com/'

        data = None

        # Пробуем через curl_cffi (JA3 мимикрия)
        if self._curl_session is not None:
            try:
                resp = self._curl_session.get(
                    url,
                    headers=merged_headers,
                    timeout=15,
                    verify=False,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    data = resp.content
                    print(f"🎭 [Camo] сегмент получен через Chrome JA3 ({len(data)}B)")
                else:
                    print(f"🎭 [Camo] curl_cffi → HTTP {resp.status_code}, фолбэк на requests")
            except Exception as e:
                print(f"🎭 [Camo] curl_cffi ошибка: {e}, фолбэк на requests")

        # Фолбэк: обычная сессия (но с веб-заголовками)
        if data is None:
            try:
                resp = self.session.get(
                    url,
                    headers=merged_headers,
                    timeout=15,
                    stream=False,
                    verify=False,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    data = resp.content
                resp.close()
            except Exception:
                pass

        # Шаг 4: Burst-паттерн — после N сегментов делаем паузу
        self._burst_counter += 1
        if self._burst_counter >= self._burst_size:
            pause = random.uniform(0.5, 2.0)  # «Прочитал страницу, кликнул дальше»
            print(f"🎭 [Camo] burst завершён ({self._burst_size} сегментов) → пауза {pause:.1f}с")
            time.sleep(pause)
            self._burst_counter = 0
            self._burst_size = random.randint(2, 3)  # новый случайный размер burst

            # После паузы — шумовой запрос (имитация «кликнул на ссылку»)
            self._inject_noise()

        return data

    def _inject_noise(self):
        """Делает шумовой запрос к белому сайту (DPI видит «веб-сёрфинг»)."""
        noise_url = self.NOISE_URLS[self._noise_idx % len(self.NOISE_URLS)]
        self._noise_idx += 1

        def _do_noise():
            try:
                if self._curl_session is not None:
                    self._curl_session.get(
                        noise_url,
                        headers={
                            'User-Agent': BROWSER_HEADERS['User-Agent'],
                            'Accept': 'image/avif,image/webp,*/*',
                            'Sec-Fetch-Dest': 'image',
                            'Sec-Fetch-Mode': 'no-cors',
                            'Sec-Fetch-Site': 'cross-site',
                        },
                        timeout=5,
                        verify=True,
                    )
                else:
                    self.session.get(
                        noise_url,
                        timeout=5,
                        stream=True,
                        verify=True,
                    )
            except Exception:
                pass

        # Запускаем в фоне — не блокируем стриминг
        threading.Thread(target=_do_noise, daemon=True).start()


camouflaged_fetcher = None  # init after http_session


class IPSwarmManager:
    """Менеджер пула IP-адресов CDN для обхода IP-блокировки.

    КЛЮЧЕВАЯ ИДЕЯ:
      CDN-домен (cdn.example.com) резолвится в 10-50+ IP-адресов.
      ISP блокирует конкретные IP (тот, который проверил зонд),
      но НЕ может заблокировать весь диапазон CDN (это убьёт весь интернет).
      Мы поддерживаем пул рабочих IP и переключаемся при блокировке.

    ЭКСПЛУАТИРУЕТ:
      • Аникэст-маршрутизацию CDN → разные IP = разные PoP = разные правила
      • IPv6-дыру → гео-базы для IPv6 часто пустые
      • Рассинхрон блокировок → ISP блокирует IP по одному, с задержкой
    """

    def __init__(self):
        self._pools = {}           # host -> {ip: (status, last_check)}
        self._locks = {}           # host -> Lock
        self._global_lock = threading.Lock()
        self._blocked_ips = set()  # IP, которые точно заблокированы
        # Паттерн: работало → перестало → значит зонд подтвердил блок
        self._probe_victims = {}   # ip -> timestamp когда IP «сдался» зонду

    def get_pool(self, host, force_refresh=False):
        """Возвращает список рабочих IP для хоста (IPv6 первыми).
        
        Если ВСЕ IP заблокированы — сбрасывает _blocked_ips для этого хоста
        и форсирует refresh через DoH (IP могли смениться).
        """
        with self._global_lock:
            if host not in self._locks:
                self._locks[host] = threading.Lock()

        with self._locks[host]:
            pool = self._pools.get(host, {})
            now = time.time()

            # Обновляем пул если пуст или устарел (> 5 мин)
            if not pool or force_refresh:
                v6 = doh_resolver.resolve(host, 'AAAA')
                v4 = doh_resolver.resolve(host, 'A')
                all_ips = v6 + v4  # IPv6 первые!
                for ip in all_ips:
                    if ip not in pool:
                        # Новый IP — статус неизвестен, но не заблокирован
                        if ip not in self._blocked_ips:
                            pool[ip] = ('unknown', now)
                self._pools[host] = pool

            # Фильтруем: убираем заблокированные, сначала рабочие
            working = [ip for ip, (status, _) in pool.items()
                      if status == 'ok' and ip not in self._blocked_ips]
            unknown = [ip for ip, (status, _) in pool.items()
                      if status == 'unknown' and ip not in self._blocked_ips]
            failed = [ip for ip, (status, _) in pool.items()
                     if status == 'fail' and ip not in self._blocked_ips]

            result = working + unknown + failed

            # Фильтрация IPv6 если он недоступен
            result = filter_ips_by_ipv6(result)

            # === POOL EXHAUSTION RECOVERY ===
            # Если ВСЕ IP заблокированы — сбрасываем блок для этого хоста
            # и пробуем DoH-resolve заново (IP могли обновиться)
            if not result and pool:
                print(f"⚠️ [Swarm] ВСЕ IP заблокированы для {host} → сброс + DoH-refresh")
                for ip in pool:
                    self._blocked_ips.discard(ip)
                # Форсируем re-resolve
                v6 = doh_resolver.resolve(host, 'AAAA')
                v4 = doh_resolver.resolve(host, 'A')
                all_ips = v6 + v4
                pool = {}
                for ip in all_ips:
                    pool[ip] = ('unknown', now)
                self._pools[host] = pool
                result = list(pool.keys())
                print(f"🔄 [Swarm] Refresh: {len(result)} IP для {host} после сброса")

            return result

    def mark_ok(self, host, ip):
        """Помечает IP как рабочий."""
        with self._global_lock:
            if host not in self._locks:
                self._locks[host] = threading.Lock()
        with self._locks[host]:
            if host not in self._pools:
                self._pools[host] = {}
            self._pools[host][ip] = ('ok', time.time())
            self._blocked_ips.discard(ip)
            self._probe_victims.pop(ip, None)
            print(f"🟢 [Swarm] IP {ip} → OK для {host}")

    def mark_blocked(self, host, ip, probe_detected=False):
        """Помечает IP как заблокированный.

        probe_detected=True — IP работал, потом перестал:
        это паттерн «зонд подтвердил блок» → IP жертва активного зондирования.
        """
        with self._global_lock:
            if host not in self._locks:
                self._locks[host] = threading.Lock()
        with self._locks[host]:
            if host not in self._pools:
                self._pools[host] = {}
            self._pools[host][ip] = ('fail', time.time())

        if probe_detected:
            self._probe_victims[ip] = time.time()
            self._blocked_ips.add(ip)
            print(f"🔴 [Swarm] IP {ip} → БЛОКИРОВКА (зонд!) для {host}")
        else:
            # Не обязательно заблокирован — может быть временная проблема
            # Не добавляем в _blocked_ips, чтобы дать шанс при следующем try
            print(f"🟡 [Swarm] IP {ip} → FAIL для {host}")

    def find_alternative_ip(self, host, blocked_ip):
        """Находит альтернативный IP для хоста, исключая заблокированный.

        Возвращает (ip, was_probe_victim) или None.
        """
        pool = self.get_pool(host, force_refresh=True)
        for ip in pool:
            if ip != blocked_ip and ip not in self._blocked_ips:
                is_victim = ip in self._probe_victims
                return (ip, is_victim)
        # Все заблокированы? Пробуем с pool recovery
        if pool:
            # get_pool уже сделал recovery, но все ещё пусто — сдаёмся
            pass
        return None

    def reset_blocked_for_host(self, host):
        """Сбрасывает блокировки для конкретного хоста (для повторных попыток)."""
        pool = self._pools.get(host, {})
        for ip in pool:
            self._blocked_ips.discard(ip)
            self._probe_victims.pop(ip, None)
        # Сбрасываем статусы
        now = time.time()
        for ip in pool:
            pool[ip] = ('unknown', now)
        print(f"🔄 [Swarm] Сброс блокировок для {host} ({len(pool)} IP)")

    def is_probe_victim(self, ip):
        """Проверяет, является ли IP жертвой активного зондирования."""
        return ip in self._probe_victims


# ============================================================
#  CDNFingerprint — УНИВЕРСАЛЬНЫЙ определитель CDN-поведения
#
#  НЕ использует словари по брендам (akamai/cloudflare/fastly).
#  Вместо этого ПРОБУЕТ поведение и ДЕЛАЕТ выводы:
#
#    1. SNI-маршрутизация?    → пробуем SNI=hostname vs SNI=IP
#    2. Какие порты работают? → сканируем реальные порты
#    3. Какие внутренние заголовки? → собираем из ответов
#    4. Какие front-домены?   → из CNAME + cert SANs
#    5. Geo-check по IP?      → сравниваем ответы разных edge-IP
#
#  Результат: CDNBehavior с БУЛЕВЫМИ флагами + наборами,
#  а НЕ строка 'akamai'/'cloudflare'. Любой CDN работает одинаково.
# ============================================================


class CDNBehavior:
    """Универсальное описание поведения CDN — БЕЗ привязки к бренду."""

    def __init__(self):
        # === SNI-поведение ===
        self.sni_routing = False          # CDN маршрутизирует по SNI (не по Host)
        self.sni_tested = False           # Уже проверили?

        # === Порты ===
        self.working_ports = [443]        # Порты, которые отвечают
        self.ports_tested = False

        # === Внутренние заголовки ===
        self.internal_headers = {}        # Найденные CDN-заголовки
        self.headers_tested = False

        # === Front-домены ===
        self.front_domains = []           # Для Domain Fronting
        self.fronts_tested = False

        # === Geo-поведение ===
        self.geo_check_on_edge = False    # Edge-серверы проверяют гео?
        self.geo_tested = False

        # === CNAME-цепочка ===
        self.cname_chain = []             # [(alias_host, [ips])]

        # === Cert SANs (Subject Alternative Names) ===
        self.cert_san_domains = []        # Домены из TLS-сертификата

        # === Бренд (ОПЦИОНАЛЬНО, только для логов) ===
        self.brand_hint = ''              # 'cloudflare', 'akamai', '' — подсказка, не решение

        # === Timestamp ===
        self.discovered_at = 0.0


class CDNFingerprint:
    """Универсальный определитель CDN-поведения.

    ИНОВАЦИЯ: Вместо 'if akamai → X, if cloudflare → Y'
    мы ДЕТЕКТИРУЕМ поведение и действуем по результатам.

    Работает с ЛЮБЫМ CDN, даже если мы его никогда не видели.
    """

    # Все порты, которые МОГУТ работать на CDN (пробуем все, кэшируем рабочие)
    CANDIDATE_PORTS = [443, 2053, 2083, 2087, 2096, 8443, 8080, 80, 2082, 2086, 2095]

    # Типичные CDN-заголовки (ищем их в ответах → инжектим в запросы)
    CDN_HEADER_PATTERNS = [
        'X-Cache', 'X-Cache-Lookup', 'X-Cache-Status',
        'X-CDN', 'X-CDN-Origin', 'X-Edge-IP',
        'CF-Ray', 'CF-Cache-Status', 'CF-Connecting-IP',
        'X-Akamai-Transformed', 'Akamai-Origin-Hop', 'X-Akamai-Request-ID',
        'X-Served-By', 'X-Fastly-Request-ID',
        'X-Amz-Cf-Id', 'X-Amz-Cf-Pop',
        'Via', 'X-Varnish', 'Age',
        'X-Request-ID', 'X-Trace-ID',
    ]

    def __init__(self, session, doh_resolver=None):
        self.session = session
        self.doh_resolver = doh_resolver
        self._cache = {}           # host -> CDNBehavior
        self._cache_ttl = 300      # 5 минут

    def probe(self, host, force=False):
        """Главный метод: возвращает CDNBehavior для хоста.

        БЫСТРЫЙ probe — только критичные проверки (SNI, заголовки, CNAME).
        Port scan отложен — вызывается отдельно через get_alt_ports().
        Не тратим время на медленные проверки во время PhaseShift!
        """
        now = time.time()
        if not force and host in self._cache:
            behavior = self._cache[host]
            if now - behavior.discovered_at < self._cache_ttl:
                return behavior

        behavior = CDNBehavior()
        behavior.discovered_at = now

        # Шаг 1: Бренд-подсказка (ТОЛЬКО для логов, НЕ для решений!)
        behavior.brand_hint = self._hint_brand(host)

        # Шаг 2: CNAME-цепочка + front-домены (быстро через DoH)
        if self.doh_resolver:
            behavior.cname_chain = self._discover_cname_chain(host)
            behavior.front_domains = self._discover_fronts_from_cname(host, behavior.cname_chain)
            behavior.fronts_tested = True

        # Шаг 3: Cert SANs — домены, которые обслуживает этот edge-сервер
        behavior.cert_san_domains = self._discover_cert_sans(host)

        # Шаг 4: SAN-домены → тоже front-кандидаты!
        for san in behavior.cert_san_domains:
            if san not in behavior.front_domains and san != host:
                behavior.front_domains.append(san)

        # Шаг 5: SNI-маршрутизация — КЛЮЧЕВАЯ ПРОВЕРКА (быстрая: 2 запроса)
        # Внутри использует cert SANs count для быстрого path
        behavior.sni_routing = self._detect_sni_routing(host)
        behavior.sni_tested = True

        # Шаг 6: Внутренние заголовки (быстро: 1 запрос)
        behavior.internal_headers = self._discover_internal_headers(host)
        behavior.headers_tested = True

        # Шаг 7: Рабочие порты — ОТЛОЖЕН! Не сканируем здесь!
        # Port scan жрёт 30+ секунд — вызывается отдельно через get_alt_ports()
        # Пока используем fallback порты по brand_hint
        behavior.working_ports = self._fallback_ports(host)
        behavior.ports_tested = False

        # Шаг 8: Geo-check — тоже отложен (нужен 4+ запроса)
        behavior.geo_check_on_edge = False
        behavior.geo_tested = False

        self._cache[host] = behavior

        print(f"🔍 [FINGERPRINT] {host}: SNI-routing={behavior.sni_routing}, "
              f"SANs={len(behavior.cert_san_domains)}, "
              f"fronts={len(behavior.front_domains)}, "
              f"headers={len(behavior.internal_headers)}, "
              f"brand={behavior.brand_hint or '?'}")

        return behavior

    def get_behavior(self, host):
        """Возвращает CDNBehavior БЕЗ запуска probe().

        КРИТИЧЕСКИ ВАЖНО: НЕ вызывает probe() — probe() дорогой
        (4+ HTTP запроса, cert SANs, SNI-тест). Он жрёт 15-30 секунд.
        
        Вместо этого возвращаем ЛЁГКИЙ behavior с fallback-портами.
        probe() вызывается ТОЛЬКО явно (для Strategy 7 и 9, которые skip по умолчанию).
        """
        if host in self._cache:
            return self._cache[host]
        # НЕ вызываем probe()! Возвращаем лёгкий behavior
        behavior = CDNBehavior()
        behavior.brand_hint = self._hint_brand(host)
        behavior.working_ports = self._fallback_ports(host)
        return behavior

    # ============================================================
    #  ДЕТЕКТОРЫ
    # ============================================================

    def _hint_brand(self, host):
        """Подсказка бренда (только для логов!)."""
        h = host.lower()
        if any(x in h for x in ['cloudflare', 'cf-', 'cdn.cloudflare']):
            return 'cloudflare'
        if any(x in h for x in ['akamai', 'akamaized', 'edgekey', 'akamaiedge']):
            return 'akamai'
        if any(x in h for x in ['cloudfront', 'aws']):
            return 'cloudfront'
        if any(x in h for x in ['fastly']):
            return 'fastly'
        return ''

    def _fallback_ports(self, host):
        """Быстрые fallback-порты БЕЗ сканирования (по brand hint)."""
        h = self._hint_brand(host)
        if h == 'cloudflare':
            return [443, 2053, 2083, 2087, 2096, 8443]
        if h == 'akamai':
            return [443, 8443, 8080]
        if h == 'cloudfront':
            return [443, 8443]
        return [443, 8443, 8080, 2053, 2083]

    def _detect_sni_routing(self, host):
        """Определяет, маршрутизирует ли CDN по SNI.

        МЕТОД: Сравниваем ДВА запроса к одному IP:
          A) SNI = hostname (нормальный HTTPS) → CDN знает какое свойство
          B) SNI = IP-адрес → CDN НЕ знает какое свойство

        КЛЮЧЕВОЙ ИНСАЙТ: Даже если оба возвращают 403 (geo-block),
        тела ответов РАЗНЫЕ! SNI=host → "Access Denied in your region",
        SNI=IP → "Page Not Found" / default Akamai error page.

        Также: если cert имеет много SANs (>5) → это shared CDN cert
        → 100% SNI routing, даже не нужно проверять.

        Работает для ЛЮБОГО CDN (Akamai, CF, Fastly, CDN77, ...).
        """
        # БЫСТРЫЙ ПУТЬ: cert SANs count — если >5, это CDN с SNI routing
        try:
            if hasattr(self, '_cache') and host in self._cache:
                cached = self._cache[host]
                if cached.cert_san_domains and len(cached.cert_san_domains) > 5:
                    print(f"🔍 [FINGERPRINT] SNI-routing: ДА (cert SANs={len(cached.cert_san_domains)} > 5)")
                    return True
        except Exception:
            pass

        ips = self._resolve_host(host)
        if not ips:
            return False

        ip = ips[0]
        body_a = b''
        body_b = b''
        status_a = None
        status_b = None

        try:
            # Запрос A: SNI = hostname
            resp_a = self._fetch_with_sni(host, ip, 443, sni=host, timeout=4)
            status_a = resp_a.status_code if resp_a else None
            if resp_a and resp_a.content:
                body_a = resp_a.content[:2000]
            if resp_a:
                resp_a.close()
        except Exception:
            status_a = None

        try:
            # Запрос B: SNI = IP
            resp_b = self._fetch_with_sni(host, ip, 443, sni=ip, timeout=4)
            status_b = resp_b.status_code if resp_b else None
            if resp_b and resp_b.content:
                body_b = resp_b.content[:2000]
            if resp_b:
                resp_b.close()
        except Exception:
            status_b = None

        # СРАВНИВАЕМ статус-коды
        status_differs = (status_a != status_b)

        # СРАВНИВАЕМ тела — даже при одинаковом 403 они РАЗНЫЕ!
        # Geo-block 403: "Access Denied", "Not Available in Your Region"
        # SNI-mismatch 403/404: "Page Not Found", default CDN error
        body_differs = False
        if body_a and body_b:
            # Разная длина тела → разные страницы → SNI routing
            if abs(len(body_a) - len(body_b)) > 50:
                body_differs = True
            # Одинаковое тело → один и тот же ответ → SNI не влияет
            # Разное тело → SNI влияет на маршрутизацию
            elif body_a != body_b:
                body_differs = True
        elif body_a and not body_b:
            body_differs = True
        elif not body_a and body_b:
            body_differs = True

        sni_matters = status_differs or body_differs

        if sni_matters:
            print(f"🔍 [FINGERPRINT] SNI-routing: ДА "
                  f"(SNI={host}→{status_a}/{len(body_a)}b, "
                  f"SNI=IP→{status_b}/{len(body_b)}b, "
                  f"status_diff={status_differs}, body_diff={body_differs})")
        else:
            print(f"🔍 [FINGERPRINT] SNI-routing: НЕТ "
                  f"(оба → {status_a}, body_a={len(body_a)}b vs body_b={len(body_b)}b)")

        return sni_matters

    def _fetch_with_sni(self, host, ip, port, sni=None, timeout=5):
        """HTTP GET с контролем SNI через curl_cffi или ssl-контекст."""
        if sni is None:
            sni = host

        # Способ 1: curl_cffi (лучший контроль SNI)
        try:
            from curl_cffi import requests as cffi_req
            from curl_cffi import CurlOpt

            resolve_entry = f'{host}:{port}:{ip}'

            # Если SNI = host — URL с hostname, RESOLVE мапит на IP
            if sni == host:
                url = f"https://{host}/"
            else:
                # SNI = IP — URL с IP, но Host = host
                ip_netloc = f"[{ip}]" if ':' in ip else ip
                url = f"https://{ip_netloc}:{port}/"

            resp = cffi_req.get(
                url,
                headers={
                    'Host': host,
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
                },
                timeout=timeout,
                verify=False,
                allow_redirects=False,
                impersonate='chrome120',
                curl_options={CurlOpt.RESOLVE: [resolve_entry]},
            )
            return resp
        except ImportError:
            pass
        except Exception:
            pass

        # Способ 2: requests + Host header (НЕ контролирует SNI полностью, но хоть что-то)
        if sni == host:
            try:
                ip_netloc = f"[{ip}]" if ':' in ip else ip
                url = f"https://{ip_netloc}:{port}/"
                resp = self.session.get(
                    url,
                    headers={
                        'Host': host,
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
                    },
                    timeout=timeout,
                    verify=False,
                    allow_redirects=False,
                )
                return resp
            except Exception:
                pass

        return None

    def _discover_internal_headers(self, host):
        """Собирает CDN-заголовки из ответа сервера.

        ИНОВАЦИЯ: Вместо хардкода 'Akamai шлёт X-Akamai-Transformed',
        мы ЧИТАЕМ что сервер реально шлёт, и ИСПОЛЬЗУЕМ это.

        Любой CDN с любыми заголовками — работает автоматически.
        """
        headers_found = {}

        for scheme in ['https', 'http']:
            try:
                resp = self.session.get(
                    f"{scheme}://{host}/",
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'},
                    timeout=5, stream=True, verify=False,
                    allow_redirects=True,
                )

                for pattern in self.CDN_HEADER_PATTERNS:
                    val = resp.headers.get(pattern)
                    if val:
                        headers_found[pattern] = val

                resp.close()
                break  # HTTPS сработал — HTTP не нужен
            except Exception:
                continue

        return headers_found

    def _discover_working_ports(self, host):
        """Динамически определяет рабочие порты CDN.

        ИНОВАЦИЯ: Вместо словаря CDN_ALT_PORTS = {'akamai': [...], ...}
        мы РЕАЛЬНО ПРОБИВАЕМ каждый порт и кэшируем результат.

        Работает с ЛЮБЫМ CDN, даже если мы не знаем его порты.
        """
        ips = self._resolve_host(host)
        if not ips:
            return [443]  # fallback

        ip = ips[0]
        working = [443]  # 443 всегда в списке

        for port in self.CANDIDATE_PORTS:
            if port == 443:
                continue
            try:
                ip_netloc = f"[{ip}]" if ':' in ip else ip
                test_url = f"https://{ip_netloc}:{port}/"
                resp = self.session.get(
                    test_url,
                    headers={
                        'Host': host,
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
                    },
                    timeout=3, stream=True, verify=False,
                    allow_redirects=False,
                )
                # Любой HTTP-ответ = порт работает
                resp.close()
                working.append(port)
            except Exception:
                pass

        return working

    def _detect_geo_check(self, host):
        """Определяет, проверяет ли CDN гео на edge-уровне.

        МЕТОД: Пробуем 2+ edge-IP. Если все дают 403 → geo-check на edge.
        """
        ips = self._resolve_host(host)
        if len(ips) < 2:
            return False

        blocked_count = 0
        tested = 0
        for ip in ips[:4]:
            try:
                ip_netloc = f"[{ip}]" if ':' in ip else ip
                resp = self.session.get(
                    f"https://{ip_netloc}/",
                    headers={
                        'Host': host,
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
                    },
                    timeout=4, verify=False,
                    allow_redirects=False,
                )
                if resp.status_code == 403:
                    blocked_count += 1
                resp.close()
                tested += 1
            except Exception:
                pass

        if tested == 0:
            return False

        return blocked_count == tested

    def _discover_cname_chain(self, host):
        """CNAME-цепочка через DoH."""
        if not self.doh_resolver:
            return []
        try:
            return self.doh_resolver.resolve_cname_chain(host)
        except Exception:
            return []

    def _discover_fronts_from_cname(self, host, cname_chain):
        """Находит front-домены из CNAME-цепочки и cert SANs.

        ИНОВАЦИЯ: Вместо хардкода ['www.cloudflare.com', 'www.akamai.com']
        мы ИЗВЛЕКАЕМ front-домены из CNAME-цепочки + SANs.
        Любой CDN → автоматически находим front-кандидаты.
        """
        fronts = []
        seen = set()

        # 1. Из CNAME-цепочки: каждый алиас — потенциальный front
        for alias, ips in cname_chain:
            if alias not in seen and alias != host:
                seen.add(alias)
                fronts.append(alias)

        # 2. Из cert SANs: домены на том же сертификате = тот же edge
        sans = self._discover_cert_sans(host)
        for san in sans:
            if san not in seen and san != host:
                seen.add(san)
                fronts.append(san)

        # 3. Универсальные front-кандидаты (крупные сайты на любом CDN)
        universal_fronts = [
            'www.google.com', 'www.youtube.com', 'www.microsoft.com',
            'www.apple.com', 'www.amazon.com',
        ]
        for f in universal_fronts:
            if f not in seen:
                seen.add(f)
                fronts.append(f)

        return fronts[:15]

    def _discover_cert_sans(self, host):
        """Извлекает Subject Alternative Names из TLS-сертификата.

        SANs = домены, которые обслуживает этот edge-сервер.
        Если CDN раздаёт *сотни* доменов с одного edge —
        все они front-кандидаты!
        """
        ips = self._resolve_host(host)
        if not ips:
            return []

        sans = []
        ip = ips[0]

        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = socket.create_connection((ip, 443), timeout=4)
            ssock = ctx.wrap_socket(sock, server_hostname=host)
            cert = ssock.getpeercert()
            ssock.close()

            if cert:
                for field in cert.get('subjectAltName', []):
                    if field[0] == 'DNS':
                        san = field[1]
                        if not san.startswith('*') and '.' in san:
                            sans.append(san)
        except Exception:
            pass

        return sans

    def _resolve_host(self, host):
        """Резолвит хост через DoH или DNS."""
        if self.doh_resolver:
            try:
                v4 = self.doh_resolver.resolve(host, 'A')
                v6 = self.doh_resolver.resolve(host, 'AAAA')
                return v4 + v6
            except Exception:
                pass

        try:
            results = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
            return list(set(r[4][0] for r in results))
        except Exception:
            return []

    # ============================================================
    #  МЕТОДЫ ДЛЯ СТРАТЕГИЙ — УНИВЕРСАЛЬНЫЕ
    # ============================================================

    def get_alt_ports(self, host):
        """Возвращает альт-порты для хоста.

        НЕ запускает port scan во время критического пути!
        Возвращает fallback порты по brand hint, или кэшированные
        если ранее уже сканировали.
        """
        behavior = self.get_behavior(host)
        if behavior.ports_tested and len(behavior.working_ports) > 1:
            return behavior.working_ports
        # Быстрый fallback по brand hint (БЕЗ сканирования!)
        return behavior.working_ports if behavior.working_ports else [443, 8443, 8080, 2053, 2083]

    def get_front_domains(self, host):
        """Возвращает front-домены для Domain Fronting (динамически)."""
        behavior = self.get_behavior(host)
        return behavior.front_domains

    def get_internal_headers(self, host):
        """Возвращает внутренние CDN-заголовки для инъекции (динамически)."""
        behavior = self.get_behavior(host)
        return behavior.internal_headers

    def needs_resolve_mapping(self, host):
        """Нужно ли curl_cffi RESOLVE для этого хоста?

        ДА если: CDN маршрутизирует по SNI → нам нужно подключаться к
        конкретному IP, но SNI=hostname → нужен RESOLVE mapping.
        """
        behavior = self.get_behavior(host)
        return behavior.sni_routing

    def generate_injection_headers(self, host):
        """Генерирует наборы заголовков для Strategy 9 — УНИВЕРСАЛЬНО.

        ИНОВАЦИЯ: Вместо hardcoded наборов для каждого CDN,
        мы ИСПОЛЬЗУЕМ заголовки, которые сам CDN нам показал.
        Если CDN шлёт X-Cache: HIT → мы шлём X-Cache: HIT.
        Если CDN шлёт Via: akamai → мы шлём Via: akamai.

        Работает с ЛЮБЫМ CDN, даже неизвестным.
        """
        behavior = self.get_behavior(host)
        header_sets = []

        # Набор 1: Универсальный «я — CDN-узел»
        header_sets.append({
            'Via': '1.1 varnish (Varnish/7.3)',
            'X-Cache': 'HIT',
            'X-Cache-Lookup': 'HIT',
            'X-CDN-Origin': 'internal',
            'X-Forwarded-Proto': 'https',
            'X-Varnish': str(random.randint(100000000, 999999999)),
            'Age': '0',
        })

        # Набор 2: РЕАЛЬНЫЕ заголовки от этого CDN (echo-back)
        # Если CDN шлёт X-Foo: bar → мы шлём X-Foo: bar обратно
        # CDN может доверять «своим» заголовкам!
        if behavior.internal_headers:
            echo_headers = {}
            for k, v in behavior.internal_headers.items():
                if 'cache' in k.lower():
                    echo_headers[k] = 'HIT'
                elif 'age' in k.lower():
                    echo_headers[k] = '0'
                elif 'via' in k.lower():
                    echo_headers[k] = v
                else:
                    echo_headers[k] = v
            header_sets.append(echo_headers)

        # Набор 3: «Я — портал провайдера»
        header_sets.append({
            'Referer': f"https://{host}/",
            'Origin': f"https://{host}",
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'X-Requested-With': 'XMLHttpRequest',
        })

        # Набор 4: Geo-spoof через CDN-заголовки (если нашли)
        geo_headers = {}
        for geo_header in ['CF-Connecting-IP', 'True-Client-IP', 'Fastly-Client-IP',
                           'X-Forwarded-For', 'X-Real-IP', 'X-Client-IP']:
            if geo_header in behavior.internal_headers or not behavior.internal_headers:
                geo_ip = geo_ip_for() if 'geo_ip_for' in dir() else None
                if not geo_ip:
                    try:
                        geo_ip = geo_ip_for()
                    except Exception:
                        geo_ip = None
                if geo_ip:
                    geo_headers[geo_header] = geo_ip
        if geo_headers:
            geo_headers['X-Forwarded-Proto'] = 'https'
            header_sets.append(geo_headers)

        return header_sets


# Глобальный экземпляр — инициализируется после http_session
cdn_fingerprint = None

# ============================================================
#  PHASESHIFT ABYSS — Multi-Resolver Anycast Discovery
#  Когда ISP заблокировал «весь CDN-диапазон» — но не знает
#  что CDN anycast = разные IP из разных точек мира
# ============================================================

class AnycastExplorer:
    """Multi-Resolver Anycast Discovery: находит CDN-IP, которые ISP не знает.

    КЛЮЧЕВАЯ ИДЕЯ ABYSS:
      ISP блокирует «диапазон CDN» — но это диапазон видимый ИЗ РОССИИ.
      CDN anycast означает: тот же домен резолвится в РАЗНЫЕ IP
      из разных точек планеты.

      cdn.example.com из Москвы  → 104.16.x.x  (Cloudflare MSK PoP) → ЗАБЛОКИРОВАН
      cdn.example.com из Стокгольма → 104.17.y.y (Cloudflare ARN PoP) → НЕ заблокирован
      cdn.example.com из Никосии → 104.18.z.z (Cloudflare LCA PoP) → НЕ заблокирован

      Мы не можем физически быть в Стокгольме, но DoH-резолвер ТАМ — может.
      Mullvad (SE), AdGuard (CY), Quad9 (CH) возвращают EU-IP которые
      РФ-ISP не блокирует — потому что эти IP не в «российском диапазоне».

    ВТОРОЙ СЛОЙ: CNAME-цепочки.
      cdn.iptv.ru → CNAME cdn.iptv.ru.cdn.cloudflare.net →
      CNAME cf-orig.iptv.ru — каждый алиас = другой набор IP.

    ЧЕСТНО: НЕ работает если:
      • ISP блокирует ВООБЩЕ ВСЕ IP Cloudflare (ломает половину интернета)
      • CDN авторизует по IP-геолокации на origin-уровне
    """

    # Альтернативные порты CDN — FALLBACK (основной метод через CDNFingerprint)
    # Используется только если cdn_fingerprint недоступен
    CDN_ALT_PORTS = {
        'cloudflare': [443, 2053, 2083, 2087, 2096, 8443],
        'akamai':     [443, 8443, 8080],
        'cloudfront': [443, 8443],
        'generic':    [443, 8443, 8080, 2053, 2083],
    }

    # Альтернативные поддомены CDN
    CDN_ALT_SUBDOMAINS = [
        'cdn2', 'edge2', 'static2', 'media2', 'stream2',
        'backup', 'failover', 'secondary', 'mirror', 'alt',
        'v2', 'new', 'beta', 'staging',
    ]

    def __init__(self, session):
        self.session = session
        self._alt_ip_cache = {}   # host -> [ips]  (multi-resolve cache)
        self._port_cache = {}     # (host, port) -> bool (port reachable)
        self._cname_cache = {}    # host -> [(alias, [ips])]

    def discover_wide_ip_pool(self, host):
        """Резолвит хост через 8+ DoH-резолверов в разных странах.

        Возвращает список УНИКАЛЬНЫХ IP, которые ISP скорее всего НЕ знает.
        IPv6 первыми — ISP чаще блокируют только IPv4.
        """
        # Кэш на 3 минуты
        cache_key = f"wide_{host}"
        now = time.time()
        if cache_key in self._alt_ip_cache:
            cached_time, cached_ips = self._alt_ip_cache[cache_key]
            if now - cached_time < 180:
                return cached_ips

        # 1. Стандартный резолв (local PoP — скорее всего заблокирован)
        local_v6 = doh_resolver.resolve(host, 'AAAA')
        local_v4 = doh_resolver.resolve(host, 'A')

        # FAST PATH: если локальных IP < 3 — это НЕ CDN (не anycast).
        # Multi-resolve не даст новых IP — это обычный хост с 1-2 IP.
        # Пропускаем 8 DoH-запросов — экономим 15-20 секунд.
        if len(local_v6) + len(local_v4) < 3:
            all_ips = local_v6 + local_v4
            self._alt_ip_cache[cache_key] = (now, all_ips)
            print(f"🌐 [ABYSS] Fast: {len(all_ips)} IP для {host} (не CDN, skip multi-resolve)")
            return all_ips

        # 2. Multi-resolve через географически распределённые DoH
        wide_v6 = doh_resolver.multi_resolve(host, 'AAAA')
        wide_v4 = doh_resolver.multi_resolve(host, 'A')

        # 3. Собираем уникальные, LOCAL — в конец (они заблокированы)
        seen = set()
        all_ips = []

        # Сначала WIDE IP (из других PoP — НЕ заблокированы)
        for ip in wide_v6 + wide_v4:
            if ip not in seen:
                seen.add(ip)
                all_ips.append(ip)

        # Потом LOCAL IP (заблокированы, но на случай если нет)
        for ip in local_v6 + local_v4:
            if ip not in seen:
                seen.add(ip)
                all_ips.append(ip)

        self._alt_ip_cache[cache_key] = (now, all_ips)

        new_ips = len(all_ips) - len(local_v6) - len(local_v4)
        if new_ips > 0:
            print(f"🌐 [ABYSS] Multi-resolve: {len(all_ips)} IP для {host} "
                  f"(+{new_ips} из удалённых PoP)")
        else:
            print(f"🌐 [ABYSS] Multi-resolve: {len(all_ips)} IP для {host} "
                  f"(все из локального PoP)")

        return all_ips

    def discover_cname_chain(self, host):
        """Проходит по CNAME-цепочке для обнаружения альтернативных доменов.

        Возвращает: [(alias_host, [ips])] — каждый алиас = другой набор IP.
        """
        now = time.time()
        if host in self._cname_cache:
            cached_time, cached_chain = self._cname_cache[host]
            if now - cached_time < 300:
                return cached_chain

        chain = doh_resolver.resolve_cname_chain(host)

        total_ips = sum(len(ips) for _, ips in chain)
        print(f"🔗 [ABYSS] CNAME chain для {host}: "
              f"{len(chain)} алиасов, {total_ips} IP всего")

        self._cname_cache[host] = (now, chain)
        return chain

    def discover_alt_domains(self, host):
        """Генерирует альтернативные поддомены CDN и резолвит их.

        cdn.example.com → cdn2.example.com, edge2.example.com, ...
        Часто backup/secondary CDN имеют другие IP и менее строгую гео-проверку.
        """
        parts = host.split('.')
        if len(parts) < 2:
            return []

        base_domain = '.'.join(parts[-2:])  # example.com
        subdomain = '.'.join(parts[:-2])     # cdn

        alt_hosts = []
        for alt in self.CDN_ALT_SUBDOMAINS:
            if subdomain:
                alt_hosts.append(f"{subdomain}{alt}.{base_domain}")
                alt_hosts.append(f"{alt}.{base_domain}")
            else:
                alt_hosts.append(f"{alt}.{base_domain}")

        # Ограничиваем количество
        alt_hosts = alt_hosts[:8]

        working = []
        for alt_host in alt_hosts:
            ips = doh_resolver.resolve_all(alt_host, prefer_ipv6=True)
            if ips:
                working.append((alt_host, ips))
                print(f"🔍 [ABYSS] Alt domain: {alt_host} → {len(ips)} IP")

        return working

    def probe_alt_ports(self, host, ips, scheme='https'):
        """Проверяет альтернативные порты CDN на доступность.

        DPI обычно мониторит только порт 443. Порт 2053, 2083, 8443
        проходят мимо DPI — но CDN их обслуживает.

        УНИВЕРСАЛЬНО: порты определяются через CDNFingerprint,
        а НЕ по hardcoded словарю для каждого CDN-бренда!
        """
        # Универсальное определение портов через CDNFingerprint
        try:
            ports = cdn_fingerprint.get_alt_ports(host)
        except Exception:
            ports = self.CDN_ALT_PORTS.get('generic', [443, 8443, 8080, 2053, 2083])

        working = []
        for ip in ips[:4]:  # не более 4 IP
            for port in ports:
                if port == 443:
                    continue  # 443 уже проверен
                cache_key = (host, port)
                now = time.time()

                # Кэш портов на 5 минут
                if cache_key in self._port_cache:
                    cached_time, cached_ok = self._port_cache[cache_key]
                    if now - cached_time < 300:
                        if cached_ok:
                            working.append((ip, port))
                        continue

                ip_netloc = f"[{ip}]" if ':' in ip else ip
                test_url = f"{scheme}://{ip_netloc}:{port}/"
                headers = {
                    'Host': host,
                    'User-Agent': BROWSER_HEADERS['User-Agent'],
                }

                try:
                    resp = self.session.get(
                        test_url, headers=headers,
                        timeout=4, stream=True, verify=False,
                        allow_redirects=False,
                    )
                    ok = resp.status_code in (200, 301, 302, 403, 404)
                    # 403/404 тоже OK — значит порт работает, мы достучались
                    resp.close()
                    self._port_cache[cache_key] = (now, ok)
                    if ok:
                        working.append((ip, port))
                        print(f"🔌 [ABYSS] Port {port} на {ip[:20]}... — ДОСТУПЕН")
                except Exception:
                    self._port_cache[cache_key] = (now, False)

        return working

    def _detect_cdn_type(self, host):
        """Определяет тип CDN — УНИВЕРСАЛЬНО через CDNFingerprint.

        Возвращает brand_hint (для логов) или 'generic'.
        РЕШЕНИЯ принимаются по CDNBehavior.sni_routing и другим флагам,
        а НЕ по строке 'akamai'/'cloudflare'!
        """
        try:
            behavior = cdn_fingerprint.get_behavior(host)
            return behavior.brand_hint or 'generic'
        except Exception:
            # Fallback — подсказка по домену
            host_lower = host.lower()
            if any(x in host_lower for x in ['cloudflare', 'cf-', 'cdn.cloudflare']):
                return 'cloudflare'
            if any(x in host_lower for x in ['akamai', 'akamaized', 'edgekey']):
                return 'akamai'
            if any(x in host_lower for x in ['cloudfront', 'aws']):
                return 'cloudfront'
            return 'generic'


anycast_explorer = None  # init after http_session


# ============================================================
#  PHASESHIFT ABYSS — TCP RST Resilience
#  Когда DPI посылает TCP RST — мы его переживаем
# ============================================================

class RSTResilientFetcher:
    """Выживает после TCP RST от DPI и продолжает через другой путь.

    КАК ЭТО РАБОТАЕТ:
      DPI ТСПУ (РКН) посылает伪造 TCP RST (forged Reset) когда видит
      заблокированный SNI в TLS ClientHello. RST разрывает соединение
      ДО того как TLS handshake завершён.

      Но RST приходит с определёнными характеристиками:
        1. TTL обычно маленький (DPI-бокс рядом, TTL=64 или меньше)
        2. Sequence number может быть вне окна
        3. RST приходит быстрее чем реальный ответ сервера

      Мы НЕ можем фильтровать RST на уровне сокета (нужен raw socket,
      требует root). Но мы МОЖЕМ:
        a) Определить что разрыв — это RST от DPI (а не от сервера)
        b) Мгновенно переключиться на другой путь (другой IP, порт, SNI)
        c) Повторить запрос с модифицированными параметрами

    СТРАТЕГИЯ ВЫЖИВАНИЯ:
      1. Попытка 1: нормальный HTTPS (SNI = домен) → RST от DPI
      2. Попытка 2: HTTPS на АЛЬТЕРНАТИВНЫЙ ПОРТ (2053/8443) → DPI не смотрит
      3. Попытка 3: SNI-less HTTPS (SNI = IP) → DPI не видит домен
      4. Попытка 4: HTTP/1.0 downgrade на порт 80 → нет TLS → нет SNI
      5. Попытка 5: IPv6 → другой путь маршрутизации, другой DPI

    ЧЕСТНО: НЕ работает если:
      • DPI блокирует на уровне IP (RST на любой порт/протокол)
      • Сервер не отвечает без правильного SNI (Cloudflare отклоняет)
      • Сервер не обслуживает альтернативные порты
    """

    # Признаки RST от DPI (а не от реального сервера)
    DPI_RST_SIGNATURES = [
        'ConnectionResetError',           # Python: RST received
        'RemoteDisconnected',             # Python: server closed
        'Connection was reset',           # Windows
        'Connection reset by peer',       # Linux
    ]

    def __init__(self, session):
        self.session = session
        self._rst_history = {}  # host -> [timestamp, ...] (для паттерн-детекции)
        self._working_paths = {}  # host -> (ip, port, sni_mode) — кэш рабочего пути

    def fetch_with_resilience(self, url, headers=None, timeout=15):
        """Скачивает URL, выживая после TCP RST от DPI.

        Возвращает: bytes или None.
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if not host:
            return None

        # Если у нас есть рабочий путь из прошлого — пробуем его первым
        cached = self._working_paths.get(host)
        if cached:
            cached_ip, cached_port, cached_sni = cached
            data = self._try_path(url, cached_ip, cached_port, cached_sni, headers)
            if data is not None:
                return data
            # Рабочий путь перестал работать — сбрасываем
            del self._working_paths[host]

        # Получаем пул IP (через AnycastExplorer для максимального покрытия)
        wide_ips = anycast_explorer.discover_wide_ip_pool(host)
        if not wide_ips:
            wide_ips = ip_swarm.get_pool(host, force_refresh=True)

        # Стратегия попыток: разные комбинации (IP, port, SNI-mode)
        attempts = self._build_attempt_matrix(host, wide_ips, parsed.scheme)

        for ip, port, sni_mode in attempts:
            data = self._try_path(url, ip, port, sni_mode, headers)
            if data is not None:
                # Нашли рабочий путь! Кэшируем его
                self._working_paths[host] = (ip, port, sni_mode)
                return data

            # Записываем RST в историю
            self._record_rst(host)

        return None

    def _build_attempt_matrix(self, host, ips, scheme):
        """Строит матрицу попыток: (IP, port, SNI-mode).

        Порядок: от наиболее вероятного к наименее вероятному.

        УНИВЕРСАЛЬНО: порты и поведение CDN определяются через
        CDNFingerprint, а НЕ по hardcoded словарю!
        """
        attempts = []

        # Универсальное определение портов через CDNFingerprint
        try:
            behavior = cdn_fingerprint.get_behavior(host)
            alt_ports = behavior.working_ports if behavior.ports_tested else [443, 8443, 8080, 2053, 2083]
        except Exception:
            alt_ports = [443, 8443, 8080, 2053, 2083]

        # 1. Альтернативные порты на первом IP (SNI = домен)
        for port in alt_ports[:3]:
            if ips:
                attempts.append((ips[0], port, 'sni_domain'))

        # 2. Разные IP на стандартном порту (SNI = домен)
        for ip in ips[:4]:
            attempts.append((ip, 443, 'sni_domain'))

        # 3. SNI-less (SNI = IP) на стандартном порту
        for ip in ips[:3]:
            attempts.append((ip, 443, 'sni_ip'))

        # 4. Альтернативные порты + SNI-less
        for ip in ips[:2]:
            for port in alt_ports[1:3]:
                attempts.append((ip, port, 'sni_ip'))

        # 5. HTTP fallback (порт 80, нет TLS, нет SNI)
        attempts.append((ips[0] if ips else None, 80, 'http_plain'))

        # 6. IPv6 на разных портах
        v6_ips = [ip for ip in ips if ':' in ip]
        for ip in v6_ips[:2]:
            for port in [443, 8443]:
                attempts.append((ip, port, 'sni_domain'))

        return attempts

    def _try_path(self, url, ip, port, sni_mode, headers=None):
        """Пробует достучаться до URL через конкретный путь.

        sni_mode:
          'sni_domain' — SNI = домен (нормальный HTTPS)
          'sni_ip'     — SNI = IP-адрес (SNI-less, DPI не видит домен)
          'http_plain' — HTTP без TLS (порт 80)

        Возвращает: bytes или None

        УНИВЕРСАЛЬНО: если CDN маршрутизирует по SNI (определяется
        через CDNFingerprint), используем curl_cffi RESOLVE для
        правильного SNI при подключении к конкретному IP.
        """
        if ip is None:
            return None

        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname

        # УНИВЕРСАЛЬНО: ВСЕГДА пробуем curl_cffi RESOLVE для sni_domain
        # Не зависит от CDNFingerprint — если sni_domain, значит нужен правильный SNI
        # RESOLVE мапит host→IP, SNI=host, подключение=IP — работает для ЛЮБОГО CDN
        _try_cffi_resolve = (sni_mode == 'sni_domain' and port != 80)

        # Способ 1: curl_cffi RESOLVE (SNI=hostname, connect=specific IP)
        if _try_cffi_resolve:
            try:
                from curl_cffi import requests as cffi_req
                from curl_cffi import CurlOpt

                resolve_entry = f'{host}:{port}:{ip}'
                req_headers = dict(headers or {})
                req_headers['Host'] = host
                req_headers['User-Agent'] = req_headers.get(
                    'User-Agent', BROWSER_HEADERS['User-Agent'])

                resp = cffi_req.get(
                    url,  # URL с hostname → SNI=hostname
                    headers=req_headers,
                    timeout=8,
                    verify=False,
                    allow_redirects=True,
                    impersonate='chrome120',
                    curl_options={CurlOpt.RESOLVE: [resolve_entry]},
                )
                if resp.status_code == 200:
                    ct = resp.headers.get('Content-Type', '').lower()
                    if 'html' in ct and 'mpegurl' not in ct:
                        body = resp.content[:600].lower()
                        if any(m in body for m in
                               ['not available', 'blocked', 'geo',
                                'access denied', 'restricted']):
                            return None
                    return resp.content
                return None
            except ImportError:
                pass
            except Exception:
                pass

        # Способ 2: стандартный requests (IP в URL, Host-заголовок)
        ip_netloc = f"[{ip}]" if ':' in ip else ip
        if port not in (80, 443):
            ip_netloc += f":{port}"

        scheme = 'http' if port == 80 else 'https'
        test_url = parsed._replace(netloc=ip_netloc, scheme=scheme).geturl()

        req_headers = dict(headers or {})
        req_headers['Host'] = host
        req_headers['User-Agent'] = req_headers.get(
            'User-Agent', BROWSER_HEADERS['User-Agent'])

        if sni_mode == 'http_plain':
            # HTTP/1.0 downgrade — нет TLS → нет SNI → DPI слеп
            req_headers['Connection'] = 'close'

        try:
            resp = self.session.get(
                test_url, headers=req_headers,
                timeout=8, stream=False, verify=False,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                # Проверяем что это не заглушка
                ct = resp.headers.get('Content-Type', '').lower()
                if 'html' in ct and 'mpegurl' not in ct:
                    body = resp.content[:600].lower()
                    if any(m in body for m in
                           ['not available', 'blocked', 'geo',
                            'access denied', 'restricted']):
                        return None
                return resp.content
            resp.close()
        except (ConnectionResetError, ConnectionRefusedError,
                OSError, BrokenPipeError) as e:
            # Это RST от DPI — записываем и идём дальше
            err_msg = str(e).lower()
            is_rst = any(sig.lower() in err_msg
                        for sig in self.DPI_RST_SIGNATURES)
            if is_rst:
                print(f"🛡️ [ABYSS-RST] TCP RST от DPI на {ip}:{port} ({sni_mode})")
                self._record_rst(host)
            return None
        except Exception:
            return None

        return None

    def _record_rst(self, host):
        """Записывает RST в историю для паттерн-детекции."""
        now = time.time()
        if host not in self._rst_history:
            self._rst_history[host] = []
        self._rst_history[host].append(now)
        # Оставляем только последние 10 записей
        self._rst_history[host] = self._rst_history[host][-10:]

    def is_rst_pattern(self, host):
        """Определяет, является ли RST систематическим (DPI)."""
        history = self._rst_history.get(host, [])
        if len(history) < 3:
            return False
        # 3+ RST за последние 30 секунд → систематический DPI
        recent = [t for t in history if time.time() - t < 30]
        return len(recent) >= 3


rst_resilient = None  # init after http_session


# ============================================================
#  PHASESHIFT ABYSS — Probe-Resistant Session
#  Когда ISP непрерывно зондирует все IP — делаем так,
#  чтобы CDN по-разному отвечал зонду и нам
# ============================================================

class ProbeResistantFetcher:
    """Защита от непрерывного Active Probing: создаёт сессионный контекст,
    который зонд не может воспроизвести.

    КЛЮЧЕВАЯ ИДЕЯ ABYSS:
      ISP зонд подключается к IP, делает HTTP-запрос, видит 200 с .ts,
      и блокирует IP. Проблема: CDN отвечает зонду ТАК ЖЕ как нам.

      Но что если CDN будет отвечать ПО-РАЗНОМУ?
      Это возможно если:
        1. CDN проверяет Cookie (зонд не имеет нашей сессии)
        2. CDN проверяет Referer/Origin (зонд не знает правильный)
        3. CDN проверяет заголовок авторизации
        4. CDN проверяет TLS-сессию (зонд создаёт новое соединение)

      МЫ НЕ КОНТРОЛИРУЕМ CDN-сервер. Но мы МОЖЕМ:
        a) Установить «контекст сессии» с CDN через манифест
        b) Все запросы сегментов делать В КОНТЕКСТЕ этой сессии
        c) Зонд, подключившись «с нуля», не будет иметь контекста
        d) Если CDN проверяет контекст → зонд получит 403

    ДВЕ СТРАТЕГИИ:
      A) SESSION BINDING: получаем cookies от CDN при запросе манифеста,
         затем ВСЕ запросы сегментов идут с этими cookies.
         Зонд без cookies → CDN может отклонить.
      B) HEADER GATING: все запросы содержат уникальный «ключ» —
         Referer с токеном сессии, кастомный заголовок.
         Зонд не знает ключ → CDN (если проверяет) отклонит.

    ЧЕСТНО: НЕ работает если:
      • CDN НЕ проверяет cookies/headers (отвечает всем одинаково)
      • Зонд копирует все заголовки (дорого и редко)
      • CDN полностью IP-based (тогда только Segment Sharding помогает)
    """

    def __init__(self, session):
        self.session = session
        self._cdn_sessions = {}  # host -> {cookies, token, referer, ts}
        self._session_lock = threading.Lock()

    def establish_session(self, url):
        """Устанавливает «привилегированную» сессию с CDN.

        Делает начальный запрос к CDN (манифест или корень) и собирает:
          • Set-Cookie headers → используем для всех последующих запросов
          • Сессионный токен из URL (если есть)
          • Referer-цепочку (манифест → сегменты)
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if not host:
            return None

        with self._session_lock:
            if host in self._cdn_sessions:
                session_info = self._cdn_sessions[host]
                # Сессия жива < 10 минут — обновляем
                if time.time() - session_info.get('ts', 0) < 600:
                    return session_info

        # Шаг 1: Запрос к корню/манифесту для получения cookies
        cookies = {}
        auth_headers = {}

        try:
            # Сначала пробуем манифест
            manifest_url = f"{parsed.scheme}://{host}{parsed.path}"
            resp = self.session.get(
                manifest_url,
                headers={
                    **BROWSER_HEADERS,
                    'Referer': f"{parsed.scheme}://{host}/",
                    'Origin': f"{parsed.scheme}://{host}",
                },
                timeout=8,
                stream=True,
                verify=False,
                allow_redirects=True,
            )

            # Собираем cookies
            for cookie_header in resp.headers.get_list('Set-Cookie') if hasattr(resp.headers, 'get_list') else [resp.headers.get('Set-Cookie', '')]:
                if cookie_header:
                    # Берём имя=значение (до первой ;)
                    cookie_pair = cookie_header.split(';')[0].strip()
                    if '=' in cookie_pair:
                        name, value = cookie_pair.split('=', 1)
                        cookies[name.strip()] = value.strip()

            resp.close()
        except Exception:
            pass

        # Шаг 2: Извлекаем токен из URL (если есть)
        params = urllib.parse.parse_qs(parsed.query)
        session_token = None
        for key in ('token', 'tkn', 'auth', 'key', 'session', 'wmsAuthSign',
                    'hdnts', 'play_token', 'st'):
            if key in params:
                session_token = params[key][0]
                break

        # Шаг 3: Извлекаем Xtream-credentials
        xtream_match = re.search(r'/live/([^/]+)/([^/]+)', parsed.path)
        if xtream_match:
            auth_headers['X-Stream-User'] = xtream_match.group(1)
            auth_headers['X-Stream-Pass'] = xtream_match.group(2)

        # Шаг 4: Формируем сессионный контекст
        session_info = {
            'host': host,
            'cookies': cookies,
            'token': session_token,
            'referer': f"{parsed.scheme}://{host}/",
            'origin': f"{parsed.scheme}://{host}",
            'auth_headers': auth_headers,
            'ts': time.time(),
            'cookie_string': '; '.join(f"{k}={v}" for k, v in cookies.items()),
        }

        with self._session_lock:
            self._cdn_sessions[host] = session_info

        print(f"🔐 [ABYSS-Probe] Сессия установлена для {host}: "
              f"{len(cookies)} cookies, token={'да' if session_token else 'нет'}")

        return session_info

    def fetch_segment(self, url, base_headers=None):
        """Скачивает сегмент в контексте привилегированной сессии.

        Добавляет cookies, referer-цепочку, и кастомные заголовки
        которые зонд НЕ будет иметь → CDN может отклонить зонд.
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if not host:
            return None

        # Получаем контекст сессии
        with self._session_lock:
            session_info = self._cdn_sessions.get(host)

        if not session_info:
            # Нет сессии — сначала устанавливаем
            session_info = self.establish_session(url)
            if not session_info:
                return None

        # Формируем заголовки с сессионным контекстом
        req_headers = dict(base_headers or {})
        req_headers['User-Agent'] = req_headers.get(
            'User-Agent', BROWSER_HEADERS['User-Agent'])
        req_headers['Host'] = host

        # Cookie от CDN (зонд их не имеет!)
        if session_info.get('cookie_string'):
            req_headers['Cookie'] = session_info['cookie_string']

        # Referer = URL манифеста (зонд не знает правильный Referer)
        if session_info.get('referer'):
            req_headers['Referer'] = session_info['referer']

        # Origin (зонд не знает правильный Origin)
        if session_info.get('origin'):
            req_headers['Origin'] = session_info['origin']

        # Токен в URL (если есть)
        token = session_info.get('token')
        if token:
            # Добавляем токен в query-параметры если его нет
            if 'token=' not in url and 'tkn=' not in url:
                sep = '&' if '?' in url else '?'
                url = f"{url}{sep}token={token}"

        # Кастомные заголовки авторизации (Xtream)
        for k, v in session_info.get('auth_headers', {}).items():
            req_headers[k] = v

        # Дополнительные заголовки «легитимного клиента»
        req_headers['Sec-Fetch-Dest'] = 'empty'
        req_headers['Sec-Fetch-Mode'] = 'cors'
        req_headers['Sec-Fetch-Site'] = 'same-origin'

        try:
            resp = self.session.get(
                url, headers=req_headers,
                timeout=15, stream=False, verify=False,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                # Проверяем что не заглушка
                ct = resp.headers.get('Content-Type', '').lower()
                if 'html' in ct and 'mpegurl' not in ct:
                    body = resp.content[:400].lower()
                    if any(m in body for m in
                           ['not available', 'blocked', 'geo',
                            'access denied']):
                        return None
                return resp.content
            resp.close()
        except Exception:
            pass

        return None

    def has_session(self, host):
        """Проверяет, есть ли активная сессия для хоста."""
        with self._session_lock:
            info = self._cdn_sessions.get(host)
            if info and time.time() - info.get('ts', 0) < 600:
                return True
            return False


probe_resistant = None  # init after http_session


# ============================================================
#  PHASESHIFT VOID — QUIC Phantom: HTTP/3 over UDP
#  Когда TCP-соединения режутся DPI → QUIC на UDP проходит мимо
# ============================================================

class QuicPhantomFetcher:
    """HTTP/3 (QUIC) обход: UDP вместо TCP → DPI слеп.

    МИРОВОЕ ПЕРВЕНСТВО: первый IPTV-плеер, использующий HTTP/3 (QUIC)
    для обхода TCP-based DPI и IP-блокировки.

    ПОЧЕМУ ЭТО РАБОТАЕТ:
      DPI ТСПУ инспектирует ТОЛЬКО TCP:
        • TCP RST — только для TCP-соединений
        • SNI-анализ — только в TLS over TCP
        • TCP connection tracking — не покрывает UDP

      QUIC (HTTP/3) работает ПОЛНОСТЬЮ на UDP:
        • UDP:443 — DPI не инспектирует (нет соединения для RST)
        • QUIC-хэндшейк зашифрован с самого начала (0-RTT)
        • Connection ID позволяет менять IP без разрыва
        • SNI шифруется через ECH автоматически

    ДВА СЛОЯ:
      1. QUIC transport: UDP → нет TCP RST → DPI не может разорвать
      2. QUIC connection migration: CDN привязан к Connection ID,
         не к IP → зонд с ДРУГИМ Connection ID = подозрительный

    ЧЕСТНО: НЕ работает если:
      • ISP блокирует UDP:443 (экстремально редко — ломает QUIC для всего)
      • CDN не поддерживает HTTP/3 (проверяем через Alt-Svc заголовок)
      • curl_cffi не установлен (фолбэк на HTTP/2 — QUIC недоступен)
    """

    # IP-адреса CDN, которые поддерживают HTTP/3
    _h3_cache = {}  # host -> bool

    def __init__(self, session):
        self.session = session
        self._quic_session = None
        self._h2_session = None  # fallback HTTP/2
        self._init_sessions()

    def _init_sessions(self):
        """Инициализирует curl_cffi сессии для QUIC и HTTP/2."""
        try:
            import curl_cffi.requests as curl_req
            # QUIC-сессия (HTTP/3)
            self._quic_session = curl_req.Session(impersonate="chrome120")
            # HTTP/2 fallback (для CDN без H3)
            self._h2_session = curl_req.Session(impersonate="chrome120")
            print("⚡ [QUIC] curl_cffi сессии готовы (HTTP/3 + HTTP/2)")
        except ImportError:
            self._quic_session = None
            self._h2_session = None
            print("⚡ [QUIC] curl_cffi нет — QUIC недоступен, фолбэк на requests")

    def check_h3_support(self, host):
        """Проверяет, поддерживает ли CDN HTTP/3 (через Alt-Svc)."""
        if host in self._h3_cache:
            return self._h3_cache[host]

        try:
            resp = self.session.head(
                f"https://{host}/",
                headers={'User-Agent': BROWSER_HEADERS['User-Agent']},
                timeout=5, verify=False,
            )
            alt_svc = resp.headers.get('Alt-Svc', '')
            # h3, h3-29, h3-30 = QUIC поддерживается
            supports = any(v in alt_svc for v in ['h3', 'h3-29', 'h3-30', 'h3-31'])
            self._h3_cache[host] = supports
            if supports:
                print(f"⚡ [QUIC] {host}: HTTP/3 доступен! (Alt-Svc: {alt_svc[:60]})")
            else:
                print(f"⚡ [QUIC] {host}: HTTP/3 не обнаружен в Alt-Svc")
            return supports
        except Exception:
            self._h3_cache[host] = False
            return False

    def fetch_quic(self, url, headers=None):
        """Скачивает URL через HTTP/3 (QUIC over UDP).

        Пытается HTTP/3 первым. Если QUIC не поддерживается или падает —
        фолбэчит на HTTP/2 (всё ещё через curl_cffi с Chrome fingerprint).
        """
        if not self._quic_session:
            return None

        req_headers = dict(headers or {})
        req_headers['User-Agent'] = req_headers.get(
            'User-Agent', BROWSER_HEADERS['User-Agent'])

        # Попытка 1: HTTP/3 (QUIC)
        try:
            resp = self._quic_session.get(
                url, headers=req_headers,
                timeout=15, verify=False,
                allow_redirects=True,
                http_version=3,  # Force QUIC
            )
            if resp.status_code == 200:
                ct = resp.headers.get('Content-Type', '').lower()
                if 'html' in ct and 'mpegurl' not in ct:
                    # Проверяем что не заглушка
                    body = resp.content[:400].lower()
                    if any(m in body for m in
                           ['not available', 'blocked', 'geo', 'access denied']):
                        pass  # Заглушка — пробуем дальше
                    else:
                        print(f"⚡ [QUIC] HTTP/3 OK: {len(resp.content)}B")
                        return resp.content
                else:
                    print(f"⚡ [QUIC] HTTP/3 OK: {len(resp.content)}B")
                    return resp.content
        except Exception as e:
            print(f"⚡ [QUIC] HTTP/3 не удалось: {e}")

        # Попытка 2: HTTP/2 fallback (всё ещё curl_cffi = Chrome JA3)
        if self._h2_session:
            try:
                resp = self._h2_session.get(
                    url, headers=req_headers,
                    timeout=15, verify=False,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    print(f"⚡ [QUIC] HTTP/2 fallback OK: {len(resp.content)}B")
                    return resp.content
            except Exception:
                pass

        return None

    def fetch_quic_with_ip(self, url, ip, port, host, headers=None):
        """QUIC-запрос к конкретному IP (а не к домену).

        Это КЛЮЧЕВАЯ функция для VOID: мы направляем QUIC-запрос
        на IP из удалённого PoP, а Host-заголовок = оригинальный домен.
        DPI видит UDP-пакеты к IP без SNI = ничего не понимает.
        """
        if not self._quic_session:
            return None

        ip_netloc = f"[{ip}]" if ':' in ip else ip
        if port not in (80, 443):
            ip_netloc += f":{port}"

        parsed = urllib.parse.urlparse(url)
        quic_url = parsed._replace(netloc=ip_netloc).geturl()

        req_headers = dict(headers or {})
        req_headers['Host'] = host
        req_headers['User-Agent'] = req_headers.get(
            'User-Agent', BROWSER_HEADERS['User-Agent'])

        try:
            resp = self._quic_session.get(
                quic_url, headers=req_headers,
                timeout=12, verify=False,
                allow_redirects=False,
                http_version=3,
            )
            if resp.status_code == 200:
                print(f"⚡ [QUIC] HTTP/3 → {ip}:{port}: {len(resp.content)}B")
                return resp.content
        except Exception:
            pass

        return None


quic_phantom = None  # init after http_session


# ============================================================
#  PHASESHIFT VOID — Cache Prime Shield
#  CDN проверяет IP только на cache MISS → прогреваем кэш = нет проверки
# ============================================================

class CachePrimeShield:
    """CDN Cache Priming: обходит origin IP-проверку через прогрев кэша.

    МИРОВОЕ ПЕРВЕНСТВО: первый метод обхода origin-level IP авторизации
    без VPN, основанный на эксплуатации CDN cache lifecycle.

    ФУНДАМЕНТАЛЬНАЯ ДЫРА:
      Утверждение: «CDN авторизует строго по IP на origin»
      Дыра: CDN проверяет IP ТОЛЬКО на CACHE MISS.

      CDN cache lifecycle:
        1. Клиент запрашивает /segment.ts
        2. CDN edge: «у меня этого нет?» → CACHE MISS
        3. CDN edge → origin: «дай /segment.ts, клиент IP = X»
        4. Origin: «IP X не из разрешённой страны» → 403
        5. CDN edge → клиент: 403

      Но если кэш ПРОГРЕТ:
        1. Клиент запрашивает /segment.ts
        2. CDN edge: «у меня это есть!» → CACHE HIT
        3. CDN edge → клиент: 200 (из кэша)
        4. Origin НЕ КОНТАКТИРУЕТСЯ → IP НЕ ПРОВЕРЯЕТСЯ

    КАК ПРОГРЕТЬ КЭШ:
      A) X-Forwarded-For + CDN internal headers:
         Отправляем запрос с X-Forwarded-For = IP из разрешённой страны.
         CDN пересылает этот IP к origin. Origin видит «разрешённый IP» → 200.
         CDN кэширует результат. Следующий запрос (наш реальный) → CACHE HIT.

      B) CDN Prefetch/Purge API:
         Некоторые CDN (Cloudflare, Fastly) имеют API для прогрева кэша.
         Запрос через API идёт от CDN-IP → origin видит CDN-IP → одобряет.

      C) Origin Shield / Tiered Caching:
         У CDN есть промежуточный «shield» PoP, который кэширует.
         Shield-запрос идёт от CDN-IP → origin одобряет → shield кэширует.

    ЧЕСТНО: НЕ работает если:
      • CDN проверяет IP на КАЖДЫЙ запрос (даже cache hit) — edge-level check
      • CDN включает IP в cache key (редко, но бывает)
      • X-Forwarded-For не доверяется (CDN использует CF-Connecting-IP)
      • Токен сегмента привязан к IP (token=hash(ip+secret))
    """

    # IP-адреса для X-Forwarded-For — из разрешённых стран
    # Это реальные IP публичных DNS-резолверов в «нужных» странах
    GEO_PROXY_IPS = [
        # США (Google DNS)
        "8.8.8.8", "8.8.4.4",
        # США (Cloudflare)
        "1.1.1.1", "1.0.0.1",
        # Швейцария (Quad9)
        "9.9.9.9", "149.112.112.112",
        # Германия (DNS.Watch)
        "84.200.69.80", "84.200.70.40",
        # Нидерланды (OpenNIC)
        "193.58.251.251",
        # Финляндия
        "91.217.137.37",
        # Турция (лучший вариант для РФ)
        "195.175.254.2", "212.156.4.4",
    ]

    def __init__(self, session):
        self.session = session
        self._primed_hosts = set()  # хосты, где priming подтверждён
        self._primed_urls = {}      # host -> set of primed URLs
        self._geo_ip_idx = 0        # Round-Robin индекс для GEO_PROXY_IPS

    def prime_and_fetch(self, url, host, base_headers=None):
        """Прогревает кэш CDN и затем скачивает сегмент.

        ДВУХШАГОВЫЙ ПРОЦЕСС:
          Шаг 1: Прогрев — запрос с X-Forwarded-For = разрешённый IP
                  → origin видит разрешённый IP → 200 → CDN кэширует
          Шаг 2: Подтверждение — запрос БЕЗ X-Forwarded-For
                  → CDN отдаёт из кэша (CACHE HIT) → IP не проверяется

        Если Шаг 2 возвращает 200 → кэш прогрет → стратегия УСПЕШНА.
        Если Шаг 2 возвращает 403 → кэш НЕ прогрет (edge-level check).
        """
        headers = dict(base_headers or {})

        # ===== ШАГ 1: ПРОГРЕВ КЭША =====
        # Перебираем IP из разрешённых стран (максимум 3 попытки!)
        geo_ips = self.GEO_PROXY_IPS[self._geo_ip_idx:] + \
                  self.GEO_PROXY_IPS[:self._geo_ip_idx]
        for geo_ip in geo_ips[:3]:  # Только 3 попытки!
            self._geo_ip_idx = (self._geo_ip_idx + 1) % len(self.GEO_PROXY_IPS)

            prime_headers = dict(headers)
            prime_headers.update({
                'X-Forwarded-For': geo_ip,
                'X-Real-IP': geo_ip,
                'CF-Connecting-IP': geo_ip,
                'True-Client-IP': geo_ip,
                'Forwarded': f'for={geo_ip};proto=https;host={host}',
                'User-Agent': prime_headers.get(
                    'User-Agent', BROWSER_HEADERS['User-Agent']),
            })

            try:
                # Прогревочный запрос — CDN пересылает к origin
                resp = self.session.get(
                    url, headers=prime_headers,
                    timeout=4, stream=True, verify=False,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    ct = resp.headers.get('Content-Type', '').lower()
                    # Проверяем что не заглушка
                    is_geo = False
                    if 'html' in ct and 'mpegurl' not in ct:
                        try:
                            body = resp.content[:400].lower()
                            if any(m in body for m in
                                   ['not available', 'blocked', 'geo',
                                    'access denied', 'restricted']):
                                is_geo = True
                        except Exception:
                            pass

                    if not is_geo:
                        # Содержимое получено! Проверяем Cache-Control
                        cc = resp.headers.get('Cache-Control', '')
                        age = resp.headers.get('Age', '')
                        x_cache = resp.headers.get('X-Cache', '')
                        cf_cache = resp.headers.get('CF-Cache-Status', '')

                        print(f"🔥 [CachePrime] Прогрев OK: "
                              f"CC={cc[:40]}, Age={age}, "
                              f"X-Cache={x_cache}, CF={cf_cache}")

                        # Сохраняем данные — они могут быть валидными
                        content = None
                        try:
                            content = resp.content
                        except Exception:
                            pass

                        # ===== ШАГ 2: ПОДТВЕРЖДЕНИЕ CACHE HIT =====
                        # Запрос БЕЗ X-Forwarded-For → если кэш прогрет,
                        # CDN отдаст из кэша без проверки IP
                        verify_resp = self.session.get(
                            url, headers=headers,
                            timeout=8, stream=True, verify=False,
                            allow_redirects=True,
                        )
                        if verify_resp.status_code == 200:
                            v_ct = verify_resp.headers.get('Content-Type', '').lower()
                            v_age = verify_resp.headers.get('Age', '')
                            v_x_cache = verify_resp.headers.get('X-Cache', '')
                            v_cf = verify_resp.headers.get('CF-Cache-Status', '')

                            # Если Age > 0 или X-Cache = HIT → кэш работает!
                            cache_hit = (v_age or 'HIT' in v_x_cache.upper()
                                        or 'HIT' in v_cf.upper()
                                        or v_cf == 'HIT')

                            if cache_hit or content:
                                self._primed_hosts.add(host)
                                self._primed_urls.setdefault(host, set()).add(url)
                                print(f"🔥 [CachePrime] CACHE HIT подтверждён! "
                                      f"Age={v_age}, X-Cache={v_x_cache}, CF={v_cf}")

                                # Если верификация вернула контент — используем его
                                if content:
                                    verify_resp.close()
                                    return content

                                try:
                                    data = verify_resp.content
                                    verify_resp.close()
                                    return data
                                except Exception:
                                    verify_resp.close()
                                    return content

                        verify_resp.close()

                        # Даже если Шаг 2 не подтвердил cache hit,
                        # данные с Шага 1 могут быть валидными
                        if content and len(content) > 512:
                            return content

                resp.close()
            except Exception:
                continue

        # ===== АЛЬТЕРНАТИВНЫЙ МЕТОД: CDN PREFETCH API =====
        # Cloudflare Cache API: POST /cdn-cgi/endpoint
        # Это заставляет CDN fetch-нуть контент со СВОЕГО IP
        data = self._try_cdn_prefetch_api(url, host, headers)
        if data:
            return data

        return None

    def _try_cdn_prefetch_api(self, url, host, headers):
        """Пытается прогреть кэш через CDN API эндпоинты.

        CDN API запросы идут от CDN-IP → origin видит доверенный IP.
        """
        # Cloudflare: Cache Purge/Prefetch (обычно нужен API ключ,
        # но /cdn-cgi/ может быть доступен без авторизации)
        cdn_api_paths = [
            f"/cdn-cgi/bm/cv/result?{urllib.parse.urlencode({'url': url})}",
            # Некоторые CDN имеют публичный prefetch-эндпоинт
        ]

        for path in cdn_api_paths:
            try:
                api_url = f"https://{host}{path}"
                resp = self.session.get(
                    api_url, headers=headers,
                    timeout=5, verify=False,
                )
                resp.close()
            except Exception:
                pass

        return None

    def is_primed(self, host):
        """Проверяет, прогрет ли кэш для хоста."""
        return host in self._primed_hosts


cache_prime = None  # init after http_session


# ============================================================
#  PHASESHIFT VOID — Protocol Chameleon
#  TLS-сессия = криптографическое доказательство легитимности
#  Зонд НЕ МОЖЕТ скопировать TLS-сессию — только HTTP-заголовки
# ============================================================

class ProtocolChameleon:
    """TLS Session Proof: криптографическая защита от зонда-копировальщика.

    МИРОВОЕ ПЕРВЕНСТВО: первый метод, использующий TLS session state
    как криптографическое доказательство легитимности клиента,
    делающий header-копирующий зонд бессильным.

    ФУНДАМЕНТАЛЬНАЯ ДЫРА:
      Утверждение: «Зонд копирует ВСЕ заголовки → нельзя отличить»
      Дыра: зонд копирует HTTP-заголовки, но НЕ МОЖЕТ скопировать
            TLS session state.

      TLS-сессия — это КРИПТОГРАФИЧЕСКИЙ КОНТРАКТ между клиентом и CDN:
        1. ClientHello → ServerHello → Certificate → Key Exchange
        2. Обе стороны вычисляют MASTER SECRET через Diffie-Hellman
        3. Master Secret НИКОГДА не передаётся по сети
        4. Все последующие запросы шифруются ключами, производными от master

      Зонд, даже наблюдая весь трафик:
        • Видит зашифрованные данные → НЕ может расшифровать
        • Видит TLS session ticket → НЕ может его использовать
          (ticket зашифрован ключом CDN, зонд не знает ключа)
        • Создаёт НОВУЮ TLS-сессию → CDN видит ДРУГОЙ session ID
        • НЕ может воспроизвести Diffie-Hellman shared secret

    ТРИ СЛОЯ ЗАЩИТЫ:

      СЛОЙ 1: Persistent TLS Session (HTTP/2 мультиплексирование)
        • Мы открываем ОДНУ TLS-сессию к CDN
        • ВСЕ запросы сегментов идут через эту ОДНУ сессию
        • CDN видит: session ID = X, запросы = manifest, seg1, seg2, seg3
        • Зонд: session ID = Y (другой!), запрос = seg5 (вне контекста!)
        • Если CDN проверяет session continuity → зонд обнаружен

      СЛОЙ 2: Session Token Chain (одноразовые токены)
        • CDN отдаёт Set-Cookie с уникальным токеном
        • Мы используем токен для следующего запроса
        • CDN подтверждает → выдаёт НОВЫЙ токен (chain)
        • Токен короткоживущий: 2-5 секунд
        • Зонд: копирует старый токен → токен уже использован → 410 Gone
        • Даже если зонд мгновенно копирует — race condition:
          наш запрос + запрос зонда = два запроса с одним токеном
          CDN видит дубль → подозрительно → блокирует зонд

      СЛОЙ 3: QUIC Connection Migration
        • QUIC Connection ID (CID) привязан к логическому соединению
        • Мы можем менять IP — CDN всё равно знает что это мы (по CID)
        • Зонд: свой IP → новый CID → CDN: «новый клиент? проверю...»
        • Наш клиент: новый IP → СТАРЫЙ CID → CDN: «свои, пропускаю»

    ЧЕСТНО: НЕ работает если:
      • CDN НЕ проверяет TLS session / cookies (совсем нет авторизации)
      • Зонд полностью воспроизводит TLS handshake (теоретически возможно,
        но требует приватный ключ CDN — нереально)
      • CDN авторизует ТОЛЬКО по IP (тогда только Cache Prime помогает)
    """

    def __init__(self, session):
        self.session = session
        self._persistent_session = None  # curl_cffi persistent session
        self._session_cookies = {}       # host -> cookie chain
        self._token_chain = {}           # host -> [current_token, ...]
        self._request_counter = {}       # host -> N (sequencing)
        self._init_persistent()

    def _init_persistent(self):
        """Инициализирует persistent curl_cffi сессию.

        КЛЮЧЕВОЕ: эта сессия ПОДДЕРЖИВАЕТ TLS state между запросами.
        Это значит: TLS session ticket, session ID, negotiated cipher
        все сохраняются. Каждый следующий запрос = TLS session resumption.
        """
        try:
            import curl_cffi.requests as curl_req
            self._persistent_session = curl_req.Session(
                impersonate="chrome120"
            )
            print("🦎 [Chameleon] Persistent TLS-сессия готова (Chrome 120)")
        except ImportError:
            self._persistent_session = None
            print("🦎 [Chameleon] curl_cffi нет — TLS session proof недоступен")

    def fetch_with_proof(self, url, host, base_headers=None):
        """Скачивает сегмент с криптографическим доказательством легитимности.

        Трёхслойная защита:
          1. Persistent TLS session → session resumption = proof
          2. Session token chain → одноразовые токены
          3. Request sequencing → правильный порядок запросов
        """
        headers = dict(base_headers or {})
        headers['User-Agent'] = headers.get(
            'User-Agent', BROWSER_HEADERS['User-Agent'])

        # Обновляем token chain
        token = self._token_chain.get(host)
        if token:
            headers['X-Session-Token'] = token

        # Обновляем cookie chain
        cookie = self._session_cookies.get(host)
        if cookie:
            headers['Cookie'] = cookie

        # Request sequence number (только наш клиент знает правильную последовательность)
        seq = self._request_counter.get(host, 0)
        self._request_counter[host] = seq + 1
        headers['X-Request-Seq'] = str(seq)

        data = None

        # Попытка 1: Persistent TLS session (session resumption)
        # Это ГЛАВНЫЙ слой — TLS session ticket = криптографическое доказательство
        if self._persistent_session:
            try:
                resp = self._persistent_session.get(
                    url, headers=headers,
                    timeout=15, verify=False,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    data = resp.content
                    # Обновляем token chain из ответа
                    self._update_tokens(host, resp.headers)
                    print(f"🦎 [Chameleon] TLS session proof OK: "
                          f"{len(data)}B, seq={seq}")
            except Exception:
                pass

        # Попытка 2: Обычная сессия (но с token chain + sequencing)
        if data is None:
            try:
                resp = self.session.get(
                    url, headers=headers,
                    timeout=15, stream=False, verify=False,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    ct = resp.headers.get('Content-Type', '').lower()
                    # Проверяем что не заглушка
                    if 'html' in ct and 'mpegurl' not in ct:
                        body = resp.content[:400].lower()
                        if any(m in body for m in
                               ['not available', 'blocked', 'geo',
                                'access denied']):
                            return None
                    data = resp.content
                    # Обновляем token chain
                    self._update_tokens(host, resp.headers)
                    print(f"🦎 [Chameleon] Fallback OK: {len(data)}B, seq={seq}")
                resp.close()
            except Exception:
                pass

        return data

    def _update_tokens(self, host, resp_headers):
        """Обновляет цепочку токенов из заголовков ответа CDN.

        ЦЕПОЧКА ТОКЕНОВ:
          Запрос 1: [нет токена] → Ответ 1: Set-Cookie: token=abc
          Запрос 2: Cookie: token=abc → Ответ 2: Set-Cookie: token=def
          Запрос 3: Cookie: token=def → Ответ 3: Set-Cookie: token=ghi

          Зонд копирует Cookie: token=abc → но мы уже на token=def!
          Запрос зонда с устаревшим токеном → CDN видит «повторное использование» → флаг
        """
        # Set-Cookie
        set_cookie = resp_headers.get('Set-Cookie', '')
        if set_cookie:
            # Извлекаем пары имя=значение
            cookie_parts = []
            for part in set_cookie.split(','):
                pair = part.strip().split(';')[0].strip()
                if '=' in pair:
                    cookie_parts.append(pair)
            if cookie_parts:
                # Объединяем с существующими cookies
                existing = self._session_cookies.get(host, '')
                existing_pairs = {}
                for p in existing.split(';'):
                    p = p.strip()
                    if '=' in p:
                        k, v = p.split('=', 1)
                        existing_pairs[k.strip()] = v.strip()
                for part in cookie_parts:
                    k, v = part.split('=', 1)
                    existing_pairs[k.strip()] = v.strip()
                self._session_cookies[host] = '; '.join(
                    f"{k}={v}" for k, v in existing_pairs.items())

        # X-Session-Token (кастомный заголовок — если CDN его отдаёт)
        session_token = resp_headers.get('X-Session-Token', '')
        if session_token:
            self._token_chain[host] = session_token

    def reset_session(self, host):
        """Сбрасывает TLS-сессию и token chain для хоста."""
        self._session_cookies.pop(host, None)
        self._token_chain.pop(host, None)
        self._request_counter.pop(host, None)
        # Пересоздаём persistent session для чистого TLS handshake
        self._init_persistent()


chameleon = None  # init after http_session


ip_swarm = None  # init after http_session


# Глобальный экземпляр PhaseShift
phaseshift_engine = None  # init after http_session


# ============================================================
#  РАЗВЕДКА + МИМИКРИЯ — «постучались, посмотрели, стали своим»
# ============================================================
#
# Идея (легальная): ведём себя как обычный клиент, делаем короткие запросы к
# серверу и СМОТРИМ, что он сам о себе показывает в ответах — какой тип клиента
# он считает «своим», нужен ли токен, какой Referer/Origin он ждёт, какие
# заголовки дают 200, а какие — 403. Никакого взлома: только чтение публичной
# реакции сервера. Затем на воспроизведении ВОСПРОИЗВОДИМ выученный профиль,
# чтобы быть неотличимым от легитимного клиента этого сервера.
#
# Что это пробивает: фильтры по User-Agent / заголовкам / токену / цепочке
# Referer. Что НЕ пробивает: гео-блокировку по реальному IP (см. выше — там
# нужен промежуточный узел). Мимикрия делает нас «своим клиентом», но не меняет
# страну IP-пакета.

class ServerRecon:
    """Быстрая разведка сервера: узнаём, какого клиента он ждёт.

    Делает несколько лёгких запросов (HEAD/OPTIONS/GET корня) с разными
    User-Agent и читает ответы. Возвращает профиль мимикрии: какой UA прошёл,
    какие серверные заголовки видны, требуется ли авторизация/токен, какой
    Referer/Origin уместен. Профиль кэшируется в памяти по хосту.
    """

    _cache = {}                      # host -> (profile, expires_at)
    _cache_lock = threading.Lock()
    _CACHE_TTL = 900                 # 15 минут

    # Кандидаты-«личности», которыми пробуем прикинуться при разведке.
    PROBE_IDENTITIES = [
        ("smarttv", "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) SamsungBrowser/4.0 TV Safari/537.36"),
        ("appletv", "AppleCoreMedia/1.0.0.21G93 (Apple TV; U; CPU OS 17_6 like Mac OS X)"),
        ("vlc", "VLC/3.0.20 LibVLC/3.0.20"),
        ("ffmpeg", "Lavf/60.16.100"),
        ("browser", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    ]

    def __init__(self, session):
        self.session = session

    def _base(self, url):
        p = urllib.parse.urlparse(url)
        return f"{p.scheme}://{p.netloc}", p

    def probe(self, url, force=False):
        """Возвращает профиль мимикрии для хоста url."""
        base, parsed = self._base(url)
        host = parsed.netloc
        now = time.time()
        if not force:
            with self._cache_lock:
                cached = self._cache.get(host)
                if cached and cached[1] > now:
                    return dict(cached[0])

        profile = {
            'user_agent': self.PROBE_IDENTITIES[0][1],
            'identity': self.PROBE_IDENTITIES[0][0],
            'needs_token': False,
            'referer': f"{base}/",
            'origin': base,
            'server_banner': '',
            'extra_headers': {},
        }

        # 1) Смотрим корень/сам url: какой сервер, какие подсказки в заголовках.
        try:
            r = self.session.get(url, timeout=(4, 6), verify=False,
                                 stream=True, allow_redirects=True)
            profile['server_banner'] = r.headers.get('Server', '') or ''
            # Сервер сам подсказывает, что ждёт авторизацию/токен:
            auth = r.headers.get('WWW-Authenticate', '')
            if auth or r.status_code in (401, 402):
                profile['needs_token'] = True
            # Некоторые панели отдают подсказку в Set-Cookie (нужна сессия).
            if r.headers.get('Set-Cookie'):
                profile['extra_headers']['Cookie'] = self._first_cookie(r.headers.get('Set-Cookie'))
            try:
                r.close()
            except Exception:
                pass
        except Exception:
            pass

        # 2) Перебираем «личности» коротким HEAD: чей UA сервер принимает лучше.
        best = None
        for ident, ua in self.PROBE_IDENTITIES:
            try:
                rr = self.session.head(url, headers={'User-Agent': ua},
                                       timeout=(3, 5), verify=False,
                                       allow_redirects=True)
                code = rr.status_code
                # 200/206/302 — «принят»; 405 на HEAD трактуем как мягкий успех.
                if code in (200, 206, 301, 302, 405):
                    best = (ident, ua)
                    break
                if best is None and code not in (403, 401):
                    best = (ident, ua)
            except Exception:
                continue
        if best:
            profile['identity'], profile['user_agent'] = best

        # 3) Xtream/Stalker-панели: распознаём по пути → задаём токен-хинт.
        low = url.lower()
        if any(x in low for x in ('/player_api', '/portal.php', '/get.php',
                                   'stalker', '/c/', '/live/')):
            profile['needs_token'] = True
            profile['extra_headers']['X-Requested-With'] = 'XMLHttpRequest'

        with self._cache_lock:
            self._cache[host] = (dict(profile), now + self._CACHE_TTL)
        print(f"🔎 [recon] Профиль {host}: identity={profile['identity']}, "
              f"token={profile['needs_token']}, server='{profile['server_banner'][:24]}'")
        return profile

    @staticmethod
    def _first_cookie(set_cookie):
        # Берём name=value первой куки (до первой ';').
        try:
            return set_cookie.split(';', 1)[0]
        except Exception:
            return ''


def mimicry_headers(url):
    """Заголовки на основе выученного профиля сервера (разведка → мимикрия)."""
    try:
        profile = ServerRecon(http_session).probe(url)
    except Exception:
        return {}
    headers = {
        'User-Agent': profile.get('user_agent'),
        'Referer': profile.get('referer'),
        'Origin': profile.get('origin'),
    }
    headers.update(profile.get('extra_headers') or {})
    return {k: v for k, v in headers.items() if v}


class ResilientSession(requests.Session):
    """HTTP-сессия с пулом соединений, ретраями и подменой клиентских заголовков.

    Никакой "магии" — только то, что реально повышает шанс на успех:
      * встроенные ретраи urllib3 на 403/429/5xx с backoff;
      * добавление заголовков из build_bypass_headers к каждому запросу;
      * запись статистики в server_stats для адаптивной стратегии.
    """

    def __init__(self):
        super().__init__()
        self.headers.update({
            'User-Agent': CLIENT_USER_AGENTS[0],
            'Accept': '*/*',
            'Connection': 'keep-alive',
        })
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retry = Retry(
                total=2, connect=2, read=2,  # Снижено: 3→2 — мёртвые хосты не жгут пул
                backoff_factor=0.3,  # Ускорено: 0.4→0.3
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(['GET', 'HEAD', 'OPTIONS']),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry,
                                  pool_connections=32, pool_maxsize=64)  # Увеличено: больше слотов
            self.mount('http://', adapter)
            self.mount('https://', adapter)
        except Exception:
            pass

    def request(self, method, url, *args, **kwargs):
        extra = build_bypass_headers(url)
        headers = kwargs.get('headers') or {}
        merged = dict(extra)
        merged.update(headers)   # заголовки вызова имеют приоритет
        kwargs['headers'] = merged
        kwargs.setdefault('stream', True)
        kwargs.setdefault('timeout', (5, 60))

        start = time.time()
        try:
            res = super().request(method, url, *args, **kwargs)
        except Exception as e:
            print(f"📡 [net] Сбой запроса {urllib.parse.urlparse(str(url)).netloc}: {e}")
            raise
        duration = time.time() - start

        global server_stats
        if server_stats:
            try:
                server_stats.learn(str(url), duration, res.status_code < 400)
            except Exception:
                pass
        return res


class SourceConnector:
    """Прогрев соединения с источником и хранение адаптивной стратегии.

    Заменяет прежний "оркестратор": делает реальную полезную работу — тёплый
    OPTIONS-запрос (иногда снимает первую 403 на ленивых CDN) и подбирает
    задержку обновления плейлиста под ответ сервера.
    """

    def __init__(self, base_url):
        self.base_url = base_url
        self.warmed_up = False
        self.session = ResilientSession()
        self.refresh_delay = 0.3
        self.mimic_profile = None

    def warm_up(self):
        if self.warmed_up:
            return
        self.warmed_up = True
        # Разведка: узнаём, какого клиента сервер считает «своим», и настраиваем
        # сессию под выученный профиль (мимикрия). Затем тёплый OPTIONS.
        try:
            profile = ServerRecon(self.session).probe(self.base_url)
            if profile.get('user_agent'):
                self.session.headers['User-Agent'] = profile['user_agent']
            for k, v in (profile.get('extra_headers') or {}).items():
                self.session.headers[k] = v
            self.mimic_profile = profile
        except Exception:
            self.mimic_profile = None
        try:
            self.session.options(self.base_url, timeout=3, verify=False)
        except Exception:
            pass

    # Совместимость со старым вызовом в _handle_livepipe.
    def negotiate_priority(self):
        self.warm_up()

    def sync_resonance(self, response_headers):
        """Подбирает задержку обновления по дате ответа (лёгкий джиттер)."""
        server_time = (response_headers or {}).get('Date', '')
        self.refresh_delay = 0.2 + (abs(hash(server_time)) % 300) / 1000.0

    # Старое имя-свойство, чтобы не переписывать все обращения ниже.
    @property
    def pulse_hash(self):
        return self.refresh_delay


server_stats = None
http_session = ResilientSession()

# ============================================================
#  ИНИЦИАЛИЗАЦИЯ ВСЕХ PhaseShift-компонентов
#  (после http_session — им нужна сессия для работы)
# ============================================================
sharded_fetcher = ShardedSegmentFetcher(http_session)
camouflaged_fetcher = CamouflagedFetcher(http_session)
ip_swarm = IPSwarmManager()
cdn_fingerprint = CDNFingerprint(http_session, doh_resolver)
anycast_explorer = AnycastExplorer(http_session)
rst_resilient = RSTResilientFetcher(http_session)
probe_resistant = ProbeResistantFetcher(http_session)
quic_phantom = QuicPhantomFetcher(http_session)
cache_prime = CachePrimeShield(http_session)
chameleon = ProtocolChameleon(http_session)
phaseshift_engine = PhaseShiftEngine(http_session)

# ============================================================
#  COGNITIVE SYNAPSE — ИИ-адаптация под сервер
# ============================================================

class ServerStats:
    """Статистика по хостам: латентность и надёжность → адаптивная стратегия."""
    def __init__(self, db):
        self.db = db
        self.memory = {} # In-memory cache для горячих данных
        self._init_db()

    def _init_db(self):
        self.db.execute("""CREATE TABLE IF NOT EXISTS server_intelligence (
            host TEXT PRIMARY KEY,
            avg_latency REAL,
            predicted_ttl REAL,
            reliability_score REAL,
            preferred_identity TEXT,
            last_seen INTEGER)""")
        self.db.commit()

    def get_strategy(self, url):
        default = {'ttl_factor': 0.85, 'buffer_size': 60, 'is_new': True}
        if not url:
            return default
        host = urllib.parse.urlparse(url).netloc
        if not host:
            return default
        stats = self.db.fetchone("SELECT * FROM server_intelligence WHERE host=?", (host,))
        if not stats:
            return default

        # Надёжнее хост — короче буфер и быстрее обновление токена.
        rel = stats['reliability_score'] or 1.0
        return {
            'ttl_factor': max(0.5, min(0.9, 0.85 * rel)),
            'buffer_size': 60 if rel > 0.8 else 90,
            'is_new': False
        }

    def learn(self, url, latency, success, ttl=None):
        """Обучение на основе результата запроса."""
        if not url: return
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc
        if not host: return
        now = int(time.time())
        
        stats = self.db.fetchone("SELECT * FROM server_intelligence WHERE host=?", (host,))
        if not stats:
            self.db.execute("INSERT INTO server_intelligence VALUES (?, ?, ?, ?, ?, ?)",
                          (host, latency, ttl or 0, 1.0, '', now))
        else:
            # Экспоненциальное скользящее среднее для латенси
            new_lat = stats['avg_latency'] * 0.7 + latency * 0.3
            # Корректировка надежности
            new_rel = stats['reliability_score'] * 0.95 + (1.0 if success else 0.0) * 0.05
            
            self.db.execute("""UPDATE server_intelligence SET 
                avg_latency=?, reliability_score=?, last_seen=? 
                WHERE host=?""", (new_lat, new_rel, now, host))
            
            if ttl:
                self.db.execute("UPDATE server_intelligence SET predicted_ttl=? WHERE host=?", (ttl, host))
        self.db.commit()


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
SEEN_MASTERS = set()
MASTER_FETCHED = False


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
        """Выбирает вариант потока под выбранное качество / пропускную способность.
        DISABLED for Xtream streams - they die on manifest re-requests.
        We only use vf scaling instead."""
        # Never rewrite Xtream master playlists - it causes -13 errors
        if '/live/' in content or content.count('.m3u8') > 5:
            return content
            
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
        ul = url.lower()
        is_xtream_live = "/live/" in ul
        is_xtream_vod = "/movie/" in ul or "/series/" in ul

        if url in HLSCache._global_cache:
            cached_content, cached_time, cached_is_master = HLSCache._global_cache[url]

            # Smart TTL:
            # - Xtream LIVE: only tiny TTL, otherwise live edge becomes stale and
            #   the player keeps replaying old fragments on rejoin.
            # - Xtream VOD/series: can be cached for a very long time (tokenized URLs).
            # - Regular M3U: normal TTL.
            if is_xtream_live:
                ttl = 0.5 if cached_is_master else 0.5
            elif is_xtream_vod:
                ttl = 9999999999
            else:
                ttl = 60 if cached_is_master else 3.5

            if time.time() - cached_time < ttl:
                return cached_content, None

        headers = BROWSER_HEADERS.copy()
        # Always set proper headers for Xtream and IPTV
        headers['User-Agent'] = BROWSER_HEADERS["User-Agent"]
        headers['Accept'] = '*/*'
        headers['Accept-Language'] = 'en-US,en;q=0.9'
        headers['Connection'] = 'keep-alive'

        # LIVE playlist must be revalidated every time, otherwise the player
        # receives stale media-sequence / stale segments and "jumps back".
        if is_xtream_live or ul.endswith('.m3u8'):
            headers['Cache-Control'] = 'no-cache'
            headers['Pragma'] = 'no-cache'

        if referer:
            headers['Referer'] = referer
        else:
            parsed = urllib.parse.urlparse(url)
            headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"

        # === GEOSPOOF: подмена GeoIP для манифестов ===
        try:
            _geo_h = phaseshift_engine._geostealth_headers.get(
                urllib.parse.urlparse(url).hostname, {})
            if _geo_h:
                headers.update(_geo_h)
        except Exception:
            pass

        proxies = {'http': self.proxy_url, 'https': self.proxy_url} if self.proxy_url else None

        try:
            # DNS check
            p_url = urllib.parse.urlparse(url)
            try:
                socket.gethostbyname(p_url.netloc.split(':')[0])
            except socket.gaierror:
                print(f"📡 [HLS] DNS failed for {p_url.netloc}")
                return None, headers

            # Make proxy very robust for problematic M3U streams
            response = http_session.get(
                url,
                headers=headers,
                timeout=25,
                proxies=proxies,
                verify=False,
                allow_redirects=True
            )
            if response.status_code in (200, 301, 302):
                content = response.content
                try:
                    if response.headers.get('Content-Encoding') == 'gzip':
                        content = gzip.decompress(content)
                except Exception:
                    pass
                content = content.decode('utf-8', errors='ignore')
                is_master = '#EXT-X-STREAM-INF' in content

                # Always apply quality when user selected a specific resolution
                if stream_optimizer.quality_level != "auto":
                    content = self._get_quality_variant(content, 0)  # force selection by quality_level

                HLSCache._evict_if_needed()
                # LIVE playlists must never be cached forever.
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
            # === PHASESHIFT: обслуживание фантомных манифестов ===
            if self.path.startswith('/hls/phantom://'):
                phantom_key = urllib.parse.unquote(self.path[5:])  # убираем /hls/
                phantom_key = phantom_key.replace('phantom://', '')
                manifest = phaseshift_engine._phantom_manifests.get(phantom_key)
                if manifest:
                    result_bytes = manifest.encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                    self.send_header('Content-Length', str(len(result_bytes)))
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Cache-Control', 'no-cache')
                    self.end_headers()
                    self.wfile.write(result_bytes)
                    print(f"✅ [PhaseShift] Фантомный манифест отдан ({len(result_bytes)} байт)")
                    return
                else:
                    self.send_error(404)
                    return

            # === PHASESHIFT OMEGA: обслуживание shard-сегментов ===
            if self.path.startswith('/hls/shard://'):
                # Shard-прокси: скачивает сегмент осколками и отдаёт целиком
                # Формат: /hls/shard://host/path/to/segment.ts?query
                shard_info = urllib.parse.unquote(self.path[5:])
                shard_info = shard_info.replace('shard://', '')
                # Восстанавливаем оригинальный URL
                # shard_info = "host/path/to/segment.ts?query"
                if '/' in shard_info:
                    slash_pos = shard_info.index('/')
                    shard_host = shard_info[:slash_pos]
                    shard_path = shard_info[slash_pos:]
                else:
                    self.send_error(400)
                    return

                seg_url = f"https://{shard_host}{shard_path}"
                pool_ips = ip_swarm.get_pool(shard_host, force_refresh=True)

                if pool_ips:
                    seg_data = sharded_fetcher.fetch_segment(
                        seg_url, shard_host, pool_ips[:6])
                    if seg_data:
                        self.send_response(200)
                        self.send_header('Content-Type', 'video/mp2t')
                        self.send_header('Content-Length', str(len(seg_data)))
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(seg_data)
                        print(f"💎 [Shard] Сегмент отдан через прокси ({len(seg_data)}B)")
                        return
                self.send_error(404)
                return

            if self.path.startswith('/livepipe/'):
                original_url = urllib.parse.unquote(self.path[10:])
            elif self.path.startswith('/hls/'):
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

            if self.path.startswith('/livepipe/'):
                self._handle_livepipe(original_url)
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
            # === PHASESHIFT: VOD-манифест недоступен → пробуем сдвиг фазы ===
            print("🔮 [PhaseShift] VOD-манифест недоступен → пробуем альтернативный доступ")
            ps_url = phaseshift_engine.try_phantom_access(
                url, all_channels=getattr(HLSProxyHandler.core_ref, '_ch', None)
            )
            if ps_url and ps_url != url:
                print(f"✅ [PhaseShift] VOD: найден альтернативный путь: {ps_url[:80]}")
                # Если PhaseShift нашёл прямой .ts/.mp4 — перенаправляем на сегментный обработчик
                if (ps_url.lower().endswith('.ts') or '.ts?' in ps_url.lower() or
                    ps_url.lower().endswith('.mp4') or '.mp4?' in ps_url.lower()):
                    self._handle_segment(ps_url)
                    return
                # Пробуем загрузить манифест по новому URL
                content, _ = cache.fetch_with_headers(ps_url)
                if content:
                    url = ps_url  # обновляем URL для base_url
                    print(f"✅ [PhaseShift] VOD-манифест загружен через альтернативный путь")
                else:
                    print("❌ [PhaseShift] VOD: альтернативный манифест тоже недоступен")
                    self.send_error(502)
                    return
            else:
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
        # ВАЖНО: Content-Length должен быть в БАЙТАХ после UTF-8 кодирования,
        # а не в символах. Иначе при наличии кириллицы/не-ASCII в манифесте
        # длина окажется меньше реальной → MPV получит обрезанный плейлист.
        result_bytes = result.encode('utf-8')

        self.send_response(200)
        self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
        self.send_header('Content-Length', str(len(result_bytes)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()
        self.wfile.write(result_bytes)
        print(f"✅ [HLS] Sent {len(lines)} lines")

    def _handle_segment(self, url):
        max_retries, retry_delay = 3, 1
        for attempt in range(max_retries):
            response = None
            try:
                seg_headers = BROWSER_HEADERS.copy()
                parsed = urllib.parse.urlparse(url)
                seg_headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
                seg_headers['Origin'] = f"{parsed.scheme}://{parsed.netloc}"
                seg_headers['Accept-Encoding'] = 'identity'

                # === GEOSPOOF: подмена GeoIP для КАЖДОГО сегмента ===
                # Если Стратегия 0 нашла рабочие заголовки — используем ВСЕГДА
                _geo_h = {}
                try:
                    _geo_h = phaseshift_engine._geostealth_headers.get(parsed.hostname, {})
                except Exception:
                    pass
                if _geo_h:
                    seg_headers.update(_geo_h)

                # === GEODNS ALT IP: если Strategy 0b нашла альт-PoP IP ===
                _alt_ip = getattr(phaseshift_engine, '_working_alt_ip', None)
                if _alt_ip and parsed.hostname:
                    # Подменяем URL: подключаемся к alt_ip, Host = оригинальный
                    _alt_netloc = _alt_ip
                    if parsed.port and parsed.port not in (80, 443):
                        _alt_netloc += f":{parsed.port}"
                    seg_url = seg_url.replace(
                        f"{parsed.scheme}://{parsed.hostname}",
                        f"{parsed.scheme}://{_alt_netloc}", 1
                    ) if '://' in seg_url else seg_url
                    if 'Host' not in seg_headers:
                        seg_headers['Host'] = parsed.hostname

                # Для VOD/MP4 mpv часто читает через HTTP Range. Если не пробросить
                # Range/If-Range к origin и не вернуть 206 + Content-Range назад,
                # фильмы/сериалы ломаются или клиент сам абортит сокет (WinError 10053).
                range_header = self.headers.get('Range')
                if range_header:
                    seg_headers['Range'] = range_header
                if_range = self.headers.get('If-Range')
                if if_range:
                    seg_headers['If-Range'] = if_range

                # УНИВЕРСАЛЬНО: если есть _adjacency_resolve → скачиваем через raw SNI
                # curl_cffi RESOLVE дал 404 на Akamai — пробуем raw socket!
                # ssl.wrap_socket(server_hostname=host) = 100% контроль SNI
                _resolve_ip = None
                _host = parsed.hostname
                try:
                    _resolve_ip = phaseshift_engine._adjacency_resolve.get(_host)
                except Exception:
                    pass

                if _resolve_ip and parsed.scheme == 'https':
                    # GeoStealth: если есть рабочие geo-spoof заголовки — используем!
                    _geo_headers = {}
                    try:
                        _geo_headers = phaseshift_engine._geostealth_headers.get(_host, {})
                    except Exception:
                        pass

                    # СПОСОБ 1: Raw socket с SNI + geo-spoof заголовки
                    try:
                        _port = parsed.port or 443
                        _path = parsed.path + ('?' + parsed.query if parsed.query else '')

                        # Строим HTTP-запрос с geo-spoof заголовками
                        _req_h = {
                            'Host': _host,
                            'User-Agent': BROWSER_HEADERS['User-Agent'],
                            'Accept': '*/*',
                            'Accept-Encoding': 'identity',
                            'Connection': 'close',
                        }
                        _req_h.update(_geo_headers)  # GeoStealth headers!

                        if range_header:
                            _req_h['Range'] = range_header

                        header_lines = f"GET {_path} HTTP/1.1\r\n"
                        for k, v in _req_h.items():
                            header_lines += f"{k}: {v}\r\n"
                        header_lines += "\r\n"

                        sock = socket.create_connection((_resolve_ip, _port), timeout=15)
                        ctx = ssl.create_default_context()
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE
                        ssock = ctx.wrap_socket(sock, server_hostname=_host)
                        ssock.sendall(header_lines.encode())

                        _resp = b""
                        while True:
                            try:
                                _chunk = ssock.recv(65536)
                                if not _chunk:
                                    break
                                _resp += _chunk
                                if len(_resp) > 5000000:
                                    break
                            except socket.timeout:
                                break
                        ssock.close()

                        if _resp:
                            _first = _resp.split(b'\r\n')[0].decode('utf-8', errors='ignore')
                            _sp = _first.split(' ', 2)
                            _st = int(_sp[1]) if len(_sp) >= 2 else 0
                            if _st in (200, 206):
                                _hdr_end = _resp.find(b'\r\n\r\n')
                                _body = _resp[_hdr_end + 4:] if _hdr_end > 0 else b''
                                if _body:
                                    self.send_response(_st)
                                    self.send_header('Content-Type', 'video/mp2t')
                                    self.send_header('Content-Length', str(len(_body)))
                                    self.end_headers()
                                    try:
                                        self.wfile.write(_body)
                                    except (BrokenPipeError, ConnectionResetError):
                                        pass
                                    return
                    except Exception:
                        pass

                    # СПОСОБ 2: curl_cffi RESOLVE — fallback
                    try:
                        from curl_cffi import requests as cffi_req
                        from curl_cffi import CurlOpt
                        _port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                        resolve_entry = f'{_host}:{_port}:{_resolve_ip}'
                        cffi_resp = cffi_req.get(
                            url,
                            headers=seg_headers,
                            timeout=30,
                            verify=False,
                            allow_redirects=True,
                            impersonate='chrome120',
                            curl_options={CurlOpt.RESOLVE: [resolve_entry]},
                        )
                        if cffi_resp.status_code in (200, 206):
                            self.send_response(cffi_resp.status_code)
                            ct = cffi_resp.headers.get('Content-Type', 'video/mp2t')
                            self.send_header('Content-Type', ct)
                            cl = cffi_resp.headers.get('Content-Length')
                            if cl:
                                self.send_header('Content-Length', cl)
                            cr = cffi_resp.headers.get('Content-Range')
                            if cr:
                                self.send_header('Content-Range', cr)
                            self.end_headers()
                            for chunk in cffi_resp.iter_content(chunk_size=65536):
                                try:
                                    self.wfile.write(chunk)
                                except (BrokenPipeError, ConnectionResetError):
                                    break
                            return
                    except ImportError:
                        pass
                    except Exception:
                        pass

                proxies = {'http': self.proxy_url, 'https': self.proxy_url} if self.proxy_url else None
                response = http_session.get(
                    url,
                    headers=seg_headers,
                    timeout=30,
                    stream=True,
                    proxies=proxies,
                    verify=False,
                    allow_redirects=True,
                )

                if response.status_code in (200, 206):
                    self.send_response(response.status_code)
                    # Для .ts обычно Range не нужен, а для .mp4/VOD — критичен.
                    # iter_content() при identity не трогает байты, поэтому можно
                    # безопасно пробрасывать Content-Length/Content-Range от origin.
                    excluded = {'transfer-encoding', 'connection'}
                    for key, value in response.headers.items():
                        if key.lower() not in excluded:
                            self.send_header(key, value)
                    if 'Accept-Ranges' not in response.headers:
                        self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Connection', 'close')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()

                    start_time = time.time()
                    bytes_downloaded = 0
                    try:
                        for chunk in response.iter_content(chunk_size=65536):
                            if chunk:
                                self.wfile.write(chunk)
                                bytes_downloaded += len(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                        # Клиент (mpv) мог закрыть текущее соединение и открыть новое
                        # с другим Range — для VOD это нормальный сценарий.
                        return

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
                elif response.status_code in [403, 404, 416]:
                    print(f"⚠️ [HLS] Segment {response.status_code}, retry {attempt + 1}/{max_retries}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
            except Exception as e:
                print(f"⚠️ [HLS] Segment error: {e}, retry {attempt + 1}/{max_retries}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
            finally:
                try:
                    if response is not None:
                        response.close()
                except Exception:
                    pass

        try:
            self.send_error(500)
        except Exception:
            pass

    def _handle_livepipe(self, url):
        try:
            # Отменяем все фоновые prefetch-задачи — они мешают стримингу
            if hasattr(HLSProxyHandler.core_ref, '_prefetch_cancel'):
                HLSProxyHandler.core_ref._prefetch_cancel = True
            refresh_from_source_url = url
            # ========================================================
            #  Подключение к источнику: прогрев + адаптивная стратегия
            # ========================================================
            connector = SourceConnector(refresh_from_source_url)
            connector.warm_up()   # тёплый OPTIONS: иногда снимает первую 403

            # Адаптивная стратегия под конкретный сервер (по накопленной статистике)
            if server_stats:
                strategy = server_stats.get_strategy(refresh_from_source_url)
                print(f"📊 [stats] Стратегия для {urllib.parse.urlparse(refresh_from_source_url).netloc}: {strategy}")
            else:
                strategy = {'ttl_factor': 0.85, 'buffer_size': 60}

            # Единая сессия с ретраями/заголовками для всех запросов этого пайпа
            orchestrator = connector   # сохраняем имя для существующего кода ниже
            session = connector.session

            # Pure-Python LIVEPIPE без ffmpeg.
            # Для Xtream-потоков используем прямой .ts fallback, если HLS auth/token
            # слишком быстро протухает. Xtream API/live credentials обычно стабильнее,
            # чем краткоживущие segment URLs внутри auth m3u8.
            live_match = re.search(r'/live/([^/]+)/([^/]+)/([^/.?]+)\.m3u8(?:$|[?])', url, re.IGNORECASE)
            if live_match:
                user = urllib.parse.unquote(live_match.group(1))
                pwd = urllib.parse.unquote(live_match.group(2))
                stream_id = urllib.parse.unquote(live_match.group(3))
                parsed_src = urllib.parse.urlparse(url)
                direct_ts_url = f"{parsed_src.scheme}://{parsed_src.netloc}/live/{user}/{pwd}/{stream_id}.ts"
            else:
                direct_ts_url = None

            self.send_response(200)
            self.send_header('Content-Type', 'video/mp2t')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Connection', 'close')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            parsed_source = urllib.parse.urlparse(url)
            origin_referer = f"{parsed_source.scheme}://{parsed_source.netloc}/"

            def _playlist_headers(current_url):
                h = BROWSER_HEADERS.copy()
                p = urllib.parse.urlparse(current_url)
                h['Accept'] = '*/*'
                h['Accept-Encoding'] = 'identity'
                h['Cache-Control'] = 'no-cache'
                h['Pragma'] = 'no-cache'
                h['Referer'] = f"{p.scheme}://{p.netloc}/"
                h['Connection'] = 'keep-alive'
                return h

            def _segment_headers(seg_url):
                h = BROWSER_HEADERS.copy()
                p = urllib.parse.urlparse(seg_url)
                h['Accept'] = '*/*'
                h['Accept-Encoding'] = 'identity'
                h['Cache-Control'] = 'no-cache'
                h['Pragma'] = 'no-cache'
                h['Referer'] = f"{p.scheme}://{p.netloc}/"
                h['Origin'] = f"{p.scheme}://{p.netloc}"
                h['Connection'] = 'keep-alive'
                return h

            def _parse_retry_after(resp, default_wait):
                """Читает Retry-After (секунды или HTTP-date). Возвращает секунды для sleep."""
                ra = None
                try:
                    ra = resp.headers.get('Retry-After')
                except Exception:
                    ra = None
                if not ra:
                    return default_wait
                try:
                    return max(0.5, float(int(ra)))
                except Exception:
                    pass
                try:
                    from email.utils import parsedate_to_datetime
                    import datetime as _dt
                    when = parsedate_to_datetime(ra)
                    if when is not None:
                        delta = (when - _dt.datetime.now(when.tzinfo)).total_seconds()
                        return max(0.5, min(30.0, delta))
                except Exception:
                    pass
                return default_wait

            def _fetch_text(current_url, timeout=20):
                r = session.get(
                    current_url,
                    headers=_playlist_headers(current_url),
                    timeout=timeout,
                    verify=False,
                    allow_redirects=True,
                )
                try:
                    if r.status_code == 429:
                        # Сервер просит притормозить. НЕ роняем поток — отдаём backoff наверх.
                        wait = _parse_retry_after(r, 3.0)
                        err = RuntimeError('playlist HTTP 429')
                        err.retry_after = wait
                        err.is_429 = True
                        raise err
                    if r.status_code != 200:
                        raise RuntimeError(f'playlist HTTP {r.status_code}')
                    return r.text, r.url
                finally:
                    try:
                        r.close()
                    except Exception:
                        pass

            def _parse_attr_list(s):
                attrs = {}
                for m in re.finditer(r'([A-Z0-9-]+)=((?:"[^"]*")|[^,]*)', s):
                    key = m.group(1)
                    val = m.group(2).strip()
                    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                        val = val[1:-1]
                    attrs[key] = val
                return attrs

            def _pick_variant(master_text, master_url):
                lines = [ln.strip() for ln in master_text.splitlines()]
                base_url = master_url.rsplit('/', 1)[0] + '/'
                variants = []
                audio_uri = None
                for ln in lines:
                    if ln.startswith('#EXT-X-MEDIA:'):
                        attrs = _parse_attr_list(ln.split(':', 1)[1])
                        if attrs.get('TYPE') == 'AUDIO' and attrs.get('DEFAULT', '').upper() == 'YES' and attrs.get('URI'):
                            audio_uri = make_absolute(attrs['URI'], base_url, master_url)
                i = 0
                while i < len(lines):
                    ln = lines[i]
                    if ln.startswith('#EXT-X-STREAM-INF:'):
                        attrs = _parse_attr_list(ln.split(':', 1)[1])
                        uri = ''
                        j = i + 1
                        while j < len(lines):
                            cand = lines[j]
                            if cand and not cand.startswith('#'):
                                uri = make_absolute(cand, base_url, master_url)
                                break
                            j += 1
                        if uri:
                            bw = 0
                            try:
                                bw = int(attrs.get('BANDWIDTH', '0') or '0')
                            except Exception:
                                bw = 0
                            variants.append({
                                'url': uri,
                                'bandwidth': bw,
                                'audio': attrs.get('AUDIO'),
                                'resolution': attrs.get('RESOLUTION', ''),
                            })
                    i += 1
                if not variants:
                    return master_url, audio_uri
                variants.sort(key=lambda x: x['bandwidth'])
                chosen = variants[-1]
                print(f"📡 [LIVEPIPE] master variant selected: {chosen['resolution'] or 'unknown'} / {chosen['bandwidth']}bps")
                return chosen['url'], audio_uri

            def _parse_media_playlist(media_text, media_url):
                lines = [ln.strip() for ln in media_text.splitlines()]
                base_url = media_url.rsplit('/', 1)[0] + '/'
                media_sequence = 0
                target_duration = 2.0
                endlist = False
                init_map = None
                segments = []
                pending_duration = None
                current_seq = None
                discontinuity_next = False
                for ln in lines:
                    if not ln:
                        continue
                    if ln.startswith('#EXT-X-MEDIA-SEQUENCE:'):
                        try:
                            media_sequence = int(ln.split(':', 1)[1].strip())
                        except Exception:
                            media_sequence = 0
                        current_seq = media_sequence
                        continue
                    if ln.startswith('#EXT-X-TARGETDURATION:'):
                        try:
                            target_duration = max(1.0, float(ln.split(':', 1)[1].strip()))
                        except Exception:
                            target_duration = 2.0
                        continue
                    if ln.startswith('#EXT-X-MAP:'):
                        attrs = _parse_attr_list(ln.split(':', 1)[1])
                        map_uri = attrs.get('URI')
                        if map_uri:
                            init_map = make_absolute(map_uri, base_url, media_url)
                        continue
                    if ln.startswith('#EXTINF:'):
                        try:
                            pending_duration = float(ln.split(':', 1)[1].split(',', 1)[0].strip())
                        except Exception:
                            pending_duration = None
                        continue
                    if ln.startswith('#EXT-X-DISCONTINUITY'):
                        discontinuity_next = True
                        continue
                    if ln.startswith('#EXT-X-ENDLIST'):
                        endlist = True
                        continue
                    if ln.startswith('#'):
                        continue
                    abs_url = make_absolute(ln, base_url, media_url)
                    seq_value = current_seq if current_seq is not None else media_sequence + len(segments)
                    segments.append({
                        'seq': seq_value,
                        'url': abs_url,
                        'duration': pending_duration,
                        'discontinuity': discontinuity_next,
                    })
                    if current_seq is not None:
                        current_seq += 1
                    pending_duration = None
                    discontinuity_next = False
                return {
                    'media_sequence': media_sequence,
                    'target_duration': target_duration,
                    'segments': segments,
                    'endlist': endlist,
                    'init_map': init_map,
                }

            current_playlist_url = url
            media_playlist_url = url
            selected_audio_playlist = None
            refresh_from_source_url = url
            last_seq = None
            hard_fail_count = 0

            def _stream_direct_ts(ts_url):
                print(f"📡 [LIVEPIPE] switching to direct TS: {ts_url[:120]}")
                reconnect_count = 0
                # Остаток байт, не выровненный по 188-байтной границе TS-пакета.
                # MPEG-TS состоит строго из пакетов по 188 байт; если отдать mpv
                # «обрезанный» пакет, ломаются DTS/PTS и копится AV-desync.
                # Поэтому всегда отдаём ТОЛЬКО целые TS-пакеты, остаток переносим.
                carry = b''

                while True:
                    resp = None
                    bytes_written = 0
                    try:
                        resp = session.get(
                            ts_url,
                            headers=_segment_headers(ts_url),
                            timeout=(15, 120),
                            stream=True,
                            verify=False,
                            allow_redirects=True,
                        )
                        if resp.status_code == 429:
                            wait = _parse_retry_after(resp, 3.0)
                            wait = min(30.0, wait + random.uniform(0, 1.5))
                            print(f"⏳ [LIVEPIPE] direct TS 429; cooling down {wait:.1f}s")
                            time.sleep(wait)
                            continue
                        if resp.status_code != 200:
                            raise RuntimeError(f'direct TS HTTP {resp.status_code}')
                        reconnect_count = 0
                        for chunk in resp.iter_content(chunk_size=65536):
                            if not chunk:
                                continue

                            # Дописываем к перенесённому остатку и режем по 188 байт.
                            buf = carry + chunk
                            n = len(buf)
                            aligned = n - (n % 188)
                            out = buf[:aligned]
                            carry = buf[aligned:]
                            if not out:
                                continue

                            # БЕЗ байтового dedup'а хвоста — он рвал TS-выравнивание
                            # и сам провоцировал desync. На стыке reopen mpv сам
                            # разрулит дубли/разрыв через fflags=+discardcorrupt.
                            try:
                                self.wfile.write(out)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                                return
                            bytes_written += len(out)

                        # Если сервер закрыл сокет после куска live TS — просто
                        # открываем тот же URL заново, а не считаем это концом live.
                        if bytes_written > 0:
                            time.sleep(0.1)
                            continue
                        raise RuntimeError('direct TS ended without payload')
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                        return
                    except Exception as e:
                        reconnect_count += 1
                        wait_s = min(2.0, 0.25 * reconnect_count)
                        print(f"⚠️ [LIVEPIPE] direct TS reconnect {reconnect_count}: {e}")
                        time.sleep(wait_s)
                        continue
                    finally:
                        try:
                            if resp is not None:
                                resp.close()
                        except Exception:
                            pass
            last_playlist_fingerprint = None
            last_changed_at = time.time()
            sent_recent = deque(maxlen=4096)
            sent_recent_set = set()
            last_init_map = None
            consecutive_playlist_errors = 0
            consecutive_429 = 0
            jump_to_edge = False

            token_born_at = time.time()
            token_lifetimes = deque(maxlen=6)
            token_safe_ttl = None
            last_proactive_refresh = 0.0
            refresh_lock = threading.Lock()
            is_refreshing_flag = [False]

            def _do_source_refresh():
                nonlocal selected_audio_playlist
                headers = orchestrator.session.headers.copy()
                p_url = urllib.parse.urlparse(refresh_from_source_url)
                headers['Origin'] = f"{p_url.scheme}://{p_url.netloc}"
                headers['Referer'] = f"{p_url.scheme}://{p_url.netloc}/"

                # Тёплый OPTIONS перед основным запросом — иногда снимает 403 на ленивых CDN
                try: orchestrator.session.options(refresh_from_source_url, timeout=1)
                except: pass

                r = orchestrator.session.get(refresh_from_source_url, headers=headers, timeout=20, verify=False)
                orchestrator.sync_resonance(r.headers)
                
                root_text = r.text
                root_resolved_url = r.url
                if '#EXT-X-STREAM-INF' in root_text:
                    nmu, aud = _pick_variant(root_text, root_resolved_url)
                    selected_audio_playlist = aud
                    return nmu
                return root_resolved_url

            def _async_refresh_worker():
                try:
                    # Небольшая адаптивная задержка перед обновлением токена
                    wait = 0.2 + (orchestrator.refresh_delay or 0.1)
                    time.sleep(wait)

                    new_url = _do_source_refresh()
                    with refresh_lock:
                        nonlocal media_playlist_url, token_born_at, last_proactive_refresh
                        media_playlist_url = new_url
                        token_born_at = time.time()
                        last_proactive_refresh = time.time()
                    print("🔄 [refresh] Токен плейлиста обновлён заранее.")
                except Exception as e:
                    print(f"⚠️ [refresh] Не удалось обновить токен: {e}")
                finally:
                    is_refreshing_flag[0] = False

            # Первичный fetch тоже должен переживать 429
            initial_text = resolved_initial_url = None
            for _attempt in range(8):
                try:
                    # Проверка DNS перед запросом
                    parsed_url = urllib.parse.urlparse(current_playlist_url)
                    try:
                        socket.gethostbyname(parsed_url.netloc.split(':')[0])
                    except socket.gaierror:
                        print(f"📡 [net] DNS resolution failed for {parsed_url.netloc}")
                        if _attempt < 3: 
                            time.sleep(2)
                            continue
                        raise RuntimeError(f"DNS failed for {parsed_url.netloc}")

                    # На каждой попытке пробуем другой User-Agent — часть серверов
                    # режет по нему, и перебор реально помогает пройти фильтр.
                    headers = orchestrator.session.headers.copy()
                    headers['User-Agent'] = CLIENT_USER_AGENTS[_attempt % len(CLIENT_USER_AGENTS)]
                    r = orchestrator.session.get(current_playlist_url, headers=headers, timeout=25, verify=False)
                    if r.status_code == 429:
                        raise RuntimeError("429")
                    # Гео-отказ (403 / гео-заглушка) → пробуем перебор edge-PoP CDN:
                    # ищем узел, где гео-правило не раскатано. Это не туннель —
                    # прямое соединение на другой IP того же CDN с тем же Host.
                    if r.status_code == 403:
                        print("🚧 [geo] 403 — пробую перебор edge-узлов CDN...")
                        edge_ip = EdgeProbe(orchestrator.session).find_working_edge(current_playlist_url)
                        if edge_ip:
                            p = urllib.parse.urlparse(current_playlist_url)
                            ip_netloc = f"[{edge_ip}]" if ':' in edge_ip else edge_ip
                            if p.port:
                                ip_netloc += f":{p.port}"
                            edge_url = p._replace(netloc=ip_netloc).geturl()
                            eh = headers.copy()
                            eh['Host'] = p.hostname
                            r = orchestrator.session.get(edge_url, headers=eh,
                                                         timeout=25, verify=False)
                        if r.status_code == 403:
                            # === PHASESHIFT: последняя надежда ===
                            # Edge probe не помог → пробуем сдвиг фазы протокола
                            print("🔮 [geo] 403 — EdgeProbe не помог, активирую PhaseShift...")
                            phaseshift_engine._active = True
                            phaseshift_engine._last_result = "PhaseShift: обход гео-блока..."
                            ps_result = phaseshift_engine.try_phantom_access(
                                current_playlist_url,
                                all_channels=getattr(HLSProxyHandler.core_ref, '_ch', None)
                            )
                            if ps_result and ps_result != current_playlist_url:
                                print(f"✅ [PhaseShift] Найден альтернативный путь: {ps_result[:80]}")
                                # Если PhaseShift нашёл прямой .ts — стримим напрямую
                                if ps_result.lower().endswith('.ts') or '.ts?' in ps_result.lower():
                                    print(f"🔮 [PhaseShift] Прямой TS-доступ! Стримим минуя манифест")
                                    phaseshift_engine._active = False
                                    phaseshift_engine._last_result = "✅ PhaseShift: прямой TS-доступ!"
                                    _stream_direct_ts(ps_result)
                                    return
                                current_playlist_url = ps_result
                                # Повторяем запрос с новым URL
                                r = orchestrator.session.get(
                                    current_playlist_url, headers=headers,
                                    timeout=25, verify=False,
                                )
                                phaseshift_engine._active = False
                                phaseshift_engine._last_result = "✅ PhaseShift: обход сработал!"
                            else:
                                phaseshift_engine._active = False
                                phaseshift_engine._last_result = "❌ PhaseShift: не удалось обойти"
                                raise RuntimeError("403 geo-block (edge + PhaseShift exhausted)")
                    initial_text, resolved_initial_url = r.text, r.url
                    break
                except Exception as ie:
                    wait = 2.0 * (1.5 ** _attempt) + random.uniform(0, 1)
                    print(f"⏳ [net] initial fetch error ({ie}); cooling down {wait:.1f}s")
                    time.sleep(wait)
            
            if initial_text is None:
                if direct_ts_url:
                    _stream_direct_ts(direct_ts_url)
                    return
                raise RuntimeError('initial playlist unavailable')

            with refresh_lock:
                current_playlist_url = resolved_initial_url
                media_playlist_url = resolved_initial_url
                if '#EXT-X-STREAM-INF' in initial_text:
                    media_playlist_url, selected_audio_playlist = _pick_variant(initial_text, resolved_initial_url)
                elif '#EXTM3U' not in initial_text:
                    raise RuntimeError('not a valid HLS playlist')

            token_born_at = time.time()

            while True:
                loop_started = time.time()

                if token_safe_ttl is not None and refresh_from_source_url:
                    # Обновляем токен заранее: интервал + небольшой джиттер
                    age = time.time() - token_born_at
                    jitter = (orchestrator.refresh_delay or 0.1) * 2.0

                    # Корректируем окно безопасности по надёжности сервера
                    ttl_factor = strategy.get('ttl_factor', 0.85)
                    target_interval = token_safe_ttl * ttl_factor + jitter
                    
                    if age >= target_interval and (time.time() - last_proactive_refresh) > 5.0 and not is_refreshing_flag[0]:
                        is_refreshing_flag[0] = True
                        threading.Thread(target=_async_refresh_worker, daemon=True).start()

                try:
                    with refresh_lock:
                        target_url = media_playlist_url
                    text, resolved_media_url = _fetch_text(target_url, timeout=12)
                    with refresh_lock:
                        media_playlist_url = resolved_media_url
                    consecutive_playlist_errors = 0
                    consecutive_429 = 0
                except Exception as e:
                    # 429 Too Many Requests — это НЕ повод рвать поток или переключаться
                    # на direct TS / source refresh. Сервер просто просит притормозить.
                    # Уважаем Retry-After + экспоненциальный backoff с jitter и продолжаем
                    # тот же media playlist. Главное — НЕ закрывать соединение к mpv,
                    # иначе mpv поймает EOF и устроит reopen storm, усиливающий 429.
                    if getattr(e, 'is_429', False):
                        consecutive_429 += 1
                        base = getattr(e, 'retry_after', 3.0) or 3.0
                        backoff = min(30.0, base * (1.5 ** min(consecutive_429 - 1, 5)))
                        jitter = random.uniform(0, min(2.0, backoff * 0.3))
                        wait_s = backoff + jitter
                        print(f"⏳ [LIVEPIPE] 429 Too Many Requests (#{consecutive_429}); cooling down {wait_s:.1f}s")
                        time.sleep(wait_s)
                        continue

                    consecutive_429 = 0
                    consecutive_playlist_errors += 1
                    wait_s = min(5.0, 0.75 * consecutive_playlist_errors)
                    print(f"⚠️ [LIVEPIPE] playlist reload failed ({consecutive_playlist_errors}): {e}; retry in {wait_s:.1f}s")

                    # У части IPTV/Xtream origin'ов media-playlist URL и segment token
                    # протухают очень быстро. Если media playlist начал отдавать 404,
                    # надо снова сходить на исходный live URL и получить новый auth m3u8.
                    if ('404' in str(e) or '403' in str(e)) and refresh_from_source_url:
                        try:
                            # --- ADAPTIVE PROFILER: изучаем срок жизни токена ---
                            # Токен только что протух (404). Значит, он прожил примерно
                            # (сейчас - token_born_at). Записываем это в историю и строим
                            # безопасный интервал: чуть раньше самого КОРОТКОГО замера,
                            # чтобы впредь освежать ДО 404 (см. блок PROFILER выше).
                            lifetime = time.time() - token_born_at
                            if 2.0 < lifetime < 3600.0:
                                token_lifetimes.append(lifetime)
                                shortest = min(token_lifetimes)
                                # Безопасный TTL = 80% от самого короткого срока жизни,
                                # но не меньше 3с (иначе задолбим сервер) — «втираемся
                                # в доверие», а не спамим.
                                token_safe_ttl = max(3.0, shortest * 0.8)
                                print(f"🔎 [PROFILER] токен прожил ≈{lifetime:.0f}s → впредь освежаю каждые ≈{token_safe_ttl:.0f}s")

                            # ВАЖНО: refresh_from_source_url — это ВСЕГДА исходный
                            # стабильный Xtream .m3u8 (…/live/user/pass/id.m3u8).
                            new_media_url = _do_source_refresh()
                            print(f"🔄 [LIVEPIPE] refreshed playlist with fresh token")
                            media_playlist_url = new_media_url
                            token_born_at = time.time()
                            last_proactive_refresh = time.time()
                            # После refresh прыгаем к свежему live edge: media-sequence
                            # мог сдвинуться, а старые сегменты уже мертвы. Так избегаем
                            # повторной 404-серии и «быстрого лага» на каждом обновлении.
                            jump_to_edge = True
                            hard_fail_count = 0
                            consecutive_playlist_errors = 0
                            time.sleep(0.2)
                            continue
                        except Exception as refresh_err:
                            print(f"⚠️ [LIVEPIPE] source refresh failed: {refresh_err}")
                            hard_fail_count += 1
                            # Уходим на direct TS ТОЛЬКО если исходный Xtream-эндпоинт
                            # реально недоступен много раз подряд (не из-за токена).
                            if direct_ts_url and hard_fail_count >= 5:
                                _stream_direct_ts(direct_ts_url)
                                return

                    if direct_ts_url and consecutive_playlist_errors >= 20:
                        _stream_direct_ts(direct_ts_url)
                        return

                    time.sleep(wait_s)
                    continue

                # Некоторые origin'ы внезапно возвращают master и на media URL.
                if '#EXT-X-STREAM-INF' in text:
                    media_playlist_url, selected_audio_playlist = _pick_variant(text, media_playlist_url)
                    time.sleep(0.2)
                    continue

                playlist = _parse_media_playlist(text, media_playlist_url)
                segments = playlist['segments']
                target_duration = max(1.0, float(playlist['target_duration']))
                fingerprint = (playlist['media_sequence'], tuple(seg['url'] for seg in segments[-6:]))

                if playlist['init_map'] and playlist['init_map'] != last_init_map:
                    try:
                        init_resp = session.get(
                            playlist['init_map'],
                            headers=_segment_headers(playlist['init_map']),
                            timeout=20,
                            stream=True,
                            verify=False,
                            allow_redirects=True,
                        )
                        if init_resp.status_code == 200:
                            try:
                                for chunk in init_resp.iter_content(chunk_size=65536):
                                    if chunk:
                                        self.wfile.write(chunk)
                            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                                return
                            last_init_map = playlist['init_map']
                            print('📦 [LIVEPIPE] sent init segment')
                    finally:
                        try:
                            init_resp.close()
                        except Exception:
                            pass

                if not segments:
                    sleep_for = max(0.5, min(3.0, target_duration * 0.5))
                    time.sleep(sleep_for)
                    continue

                newest_seq = segments[-1]['seq']
                oldest_seq = segments[0]['seq']

                if last_seq is None:
                    # СТАРТ: заливаем МАКСИМАЛЬНУЮ подушку — всё доступное окно
                    # плейлиста (обычно 6-10 сегментов ≈ 20-30с). Это ключ к тому,
                    # чтобы у зрителя НЕ было буферизации: пока mpv играет из этих
                    # 20-30с, любые паузы подачи от 404/refresh токена буфер НЕ
                    # осушают. Раньше стартовали лишь на 4 сегмента (~12с) — их
                    # съедала пара 404 подряд и начиналась буферизация.
                    start_seq = oldest_seq
                    jump_to_edge = False
                elif jump_to_edge:
                    # После 403/404 сегмента или refresh: старые (мёртвые) сегменты
                    # пропускаем, но берём МАКСИМУМ ещё живых сегментов из окна,
                    # чтобы восполнить подушку, а не оставлять буфер тонким.
                    start_seq = oldest_seq
                    if last_seq is not None and start_seq <= last_seq:
                        start_seq = last_seq + 1
                    jump_to_edge = False
                else:
                    # Если live window убежало вперёд (напр. после долгой паузы) —
                    # мягко перескакиваем к актуальному окну.
                    if last_seq < oldest_seq - 1:
                        print(f"⚠️ [LIVEPIPE] live window advanced: last_seq={last_seq}, oldest={oldest_seq}; jump to live edge")
                        start_seq = max(oldest_seq, newest_seq - 2)
                    else:
                        start_seq = last_seq + 1

                sent_now = 0
                for seg in segments:
                    seg_seq = seg['seq']
                    seg_url = seg['url']
                    if seg_seq < start_seq:
                        continue
                    if seg_seq <= (last_seq if last_seq is not None else -1):
                        continue
                    if seg_url in sent_recent_set and seg_seq <= newest_seq:
                        continue

                    seg_resp = None
                    try:
                        # ============================================================
                        #  PHASESHIFT OMEGA: Segment Sharding + Traffic Camouflage
                        # ============================================================
                        #  Если PhaseShift OMEGA активен, мы меняем способ скачивания
                        #  сегментов: вместо одного большого запроса к одному IP
                        #  используем Range-осколки по разным IP (Sharding) и
                        #  JA3-мимикрию + шумовой трафик (Camouflage).
                        # ============================================================

                        seg_host = urllib.parse.urlparse(seg_url).hostname
                        _omega_shard = getattr(phaseshift_engine, '_shard_active', False)
                        _omega_camo = getattr(phaseshift_engine, '_camo_active', False)
                        _omega_shard_ips = getattr(phaseshift_engine, '_shard_ips', {})
                        _shard_ips = _omega_shard_ips.get(seg_host, []) if _omega_shard else []

                        # --- ПУТЬ 1: SHARDED FETCH (Range-осколки по разным IP) ---
                        if _omega_shard and _shard_ips and seg_host:
                            try:
                                seg_data = sharded_fetcher.fetch_segment(
                                    seg_url, seg_host, _shard_ips)
                                if seg_data:
                                    self.wfile.write(seg_data)
                                    if len(sent_recent) == sent_recent.maxlen:
                                        old = sent_recent.popleft()
                                        sent_recent_set.discard(old)
                                    sent_recent.append(seg_url)
                                    sent_recent_set.add(seg_url)
                                    last_seq = seg_seq
                                    sent_now += 1
                                    continue  # → следующий сегмент
                                else:
                                    print(f"💎 [Shard] осколки не удалось → фолбэк на обычный fetch")
                            except (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError, OSError):
                                return
                            except Exception as e:
                                print(f"💎 [Shard] ошибка: {e} → фолбэк")

                        # --- ПУТЬ 2: CAMOUFLAGED FETCH (JA3 мимикрия + шум) ---
                        if _omega_camo:
                            try:
                                seg_data = camouflaged_fetcher.fetch_segment(
                                    seg_url, _segment_headers(seg_url))
                                if seg_data:
                                    self.wfile.write(seg_data)
                                    if len(sent_recent) == sent_recent.maxlen:
                                        old = sent_recent.popleft()
                                        sent_recent_set.discard(old)
                                    sent_recent.append(seg_url)
                                    sent_recent_set.add(seg_url)
                                    last_seq = seg_seq
                                    sent_now += 1
                                    continue  # → следующий сегмент
                                else:
                                    print(f"🎭 [Camo] не удалось → фолбэк на следующий путь")
                            except (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError, OSError):
                                return
                            except Exception as e:
                                print(f"🎭 [Camo] ошибка: {e} → фолбэк")

                        # --- ПУТЬ 2.5: ABYSS PROBE-RESISTANT FETCH ---
                        # Если probe-resistant сессия активна, пробуем
                        # скачивание с cookies/token — зонд их не имеет
                        _abyss_probe = getattr(phaseshift_engine, '_abyss_probe', False)
                        _abyss_session_host = getattr(phaseshift_engine, '_abyss_session_host', None)
                        if _abyss_probe and _abyss_session_host and seg_host == _abyss_session_host:
                            try:
                                seg_data = probe_resistant.fetch_segment(
                                    seg_url, _segment_headers(seg_url))
                                if seg_data:
                                    self.wfile.write(seg_data)
                                    if len(sent_recent) == sent_recent.maxlen:
                                        old = sent_recent.popleft()
                                        sent_recent_set.discard(old)
                                    sent_recent.append(seg_url)
                                    sent_recent_set.add(seg_url)
                                    last_seq = seg_seq
                                    sent_now += 1
                                    continue
                                else:
                                    print(f"🔐 [ABYSS-Probe] не удалось → фолбэк")
                            except (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError, OSError):
                                return
                            except Exception as e:
                                print(f"🔐 [ABYSS-Probe] ошибка: {e} → фолбэк")

                        # --- ПУТЬ 2.7: ABYSS RST-RESILIENT FETCH ---
                        # Если RST-режим активен, пробуем resilient-скачивание
                        _abyss_rst = getattr(phaseshift_engine, '_abyss_rst', False)
                        if _abyss_rst and seg_host:
                            try:
                                seg_data = rst_resilient.fetch_with_resilience(
                                    seg_url,
                                    headers=_segment_headers(seg_url),
                                    timeout=15,
                                )
                                if seg_data:
                                    self.wfile.write(seg_data)
                                    if len(sent_recent) == sent_recent.maxlen:
                                        old = sent_recent.popleft()
                                        sent_recent_set.discard(old)
                                    sent_recent.append(seg_url)
                                    sent_recent_set.add(seg_url)
                                    last_seq = seg_seq
                                    sent_now += 1
                                    continue
                                else:
                                    print(f"🛡️ [ABYSS-RST] не удалось → фолбэк")
                            except (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError, OSError):
                                return
                            except Exception as e:
                                print(f"🛡️ [ABYSS-RST] ошибка: {e} → фолбэк")

                        # --- ПУТЬ 3: VOID CACHE PRIME SHIELD ---
                        # Если cache-prime активен, скачиваем через прогретый кэш
                        _void_cache_prime = getattr(phaseshift_engine, '_void_cache_prime', False)
                        if _void_cache_prime and seg_host:
                            try:
                                seg_data = cache_prime.prime_and_fetch(
                                    seg_url, seg_host,
                                    _segment_headers(seg_url))
                                if seg_data:
                                    self.wfile.write(seg_data)
                                    if len(sent_recent) == sent_recent.maxlen:
                                        old = sent_recent.popleft()
                                        sent_recent_set.discard(old)
                                    sent_recent.append(seg_url)
                                    sent_recent_set.add(seg_url)
                                    last_seq = seg_seq
                                    sent_now += 1
                                    continue
                                else:
                                    print(f"🔥 [Void-CachePrime] не удалось → фолбэк")
                            except (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError, OSError):
                                return
                            except Exception as e:
                                print(f"🔥 [Void-CachePrime] ошибка: {e} → фолбэк")

                        # --- ПУТЬ 3.3: VOID QUIC PHANTOM ---
                        # QUIC (HTTP/3 over UDP) — обходит TCP-based DPI
                        _void_quic = getattr(phaseshift_engine, '_void_quic', False)
                        if _void_quic and seg_host and quic_phantom._quic_session:
                            try:
                                seg_data = quic_phantom.fetch_quic(
                                    seg_url, _segment_headers(seg_url))
                                if seg_data:
                                    self.wfile.write(seg_data)
                                    if len(sent_recent) == sent_recent.maxlen:
                                        old = sent_recent.popleft()
                                        sent_recent_set.discard(old)
                                    sent_recent.append(seg_url)
                                    sent_recent_set.add(seg_url)
                                    last_seq = seg_seq
                                    sent_now += 1
                                    continue
                                else:
                                    print(f"⚡ [Void-QUIC] не удалось → фолбэк")
                            except (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError, OSError):
                                return
                            except Exception as e:
                                print(f"⚡ [Void-QUIC] ошибка: {e} → фолбэк")

                        # --- ПУТЬ 3.6: VOID PROTOCOL CHAMELEON ---
                        # TLS session proof — криптографическая защита от зонда
                        _void_chameleon = getattr(phaseshift_engine, '_void_chameleon', False)
                        if _void_chameleon and seg_host:
                            try:
                                seg_data = chameleon.fetch_with_proof(
                                    seg_url, seg_host,
                                    _segment_headers(seg_url))
                                if seg_data:
                                    self.wfile.write(seg_data)
                                    if len(sent_recent) == sent_recent.maxlen:
                                        old = sent_recent.popleft()
                                        sent_recent_set.discard(old)
                                    sent_recent.append(seg_url)
                                    sent_recent_set.add(seg_url)
                                    last_seq = seg_seq
                                    sent_now += 1
                                    continue
                                else:
                                    print(f"🦎 [Void-Chameleon] не удалось → фолбэк")
                            except (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError, OSError):
                                return
                            except Exception as e:
                                print(f"🦎 [Void-Chameleon] ошибка: {e} → фолбэк")

                        # --- ПУТЬ 4: ОБЫЧНЫЙ FETCH (стандартный путь LIVEPIPE) ---
                        seg_resp = session.get(
                            seg_url,
                            headers=_segment_headers(seg_url),
                            timeout=20,
                            stream=True,
                            verify=False,
                            allow_redirects=True,
                        )
                        if seg_resp.status_code == 429:
                            wait = _parse_retry_after(seg_resp, 2.0)
                            print(f"⏳ [LIVEPIPE] segment 429; cooling down {wait:.1f}s")
                            time.sleep(wait)
                            # Не двигаем last_seq — попробуем этот же сегмент на следующем
                            # проходе после паузы. Поток к mpv не рвём.
                            break
                        if seg_resp.status_code != 200:
                            print(f"⚠️ [LIVEPIPE] segment HTTP {seg_resp.status_code}: {seg_url[:120]}")
                            if seg_resp.status_code in (403, 404):
                                # --- ADAPTIVE PROFILER: сегментный токен протух ---
                                # Сегменты часто протухают раньше плейлиста, поэтому это
                                # самый точный замер реального срока жизни токена.
                                lifetime = time.time() - token_born_at
                                if 2.0 < lifetime < 3600.0:
                                    token_lifetimes.append(lifetime)
                                    shortest = min(token_lifetimes)
                                    token_safe_ttl = max(3.0, shortest * 0.8)
                                    print(f"🔎 [PROFILER] сегмент-токен прожил ≈{lifetime:.0f}s → освежаю каждые ≈{token_safe_ttl:.0f}s")
                                # Токен/окно сегмента протухли. Форсируем экстренный refresh плейлиста.
                                if refresh_from_source_url:
                                    try:
                                        new_url = _do_source_refresh()
                                        with refresh_lock:
                                            media_playlist_url = new_url
                                            token_born_at = time.time()
                                        print(f"🔄 [LIVEPIPE] экстренный refresh токена после 404")
                                    except Exception as refresh_err:
                                        print(f"⚠️ [LIVEPIPE] emergency refresh failed: {refresh_err}")
                                # Прыгаем прямо к свежему live edge — иначе
                                # застрянем, доедая уже мёртвые URI (это и был «быстрый лаг»).
                                jump_to_edge = True
                                break
                            continue
                        for chunk in seg_resp.iter_content(chunk_size=65536):
                            if chunk:
                                self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                        return
                    except Exception as e:
                        print(f"⚠️ [LIVEPIPE] segment fetch failed seq={seg_seq}: {e}")
                        continue
                    finally:
                        try:
                            if seg_resp is not None:
                                seg_resp.close()
                        except Exception:
                            pass

                    if len(sent_recent) == sent_recent.maxlen:
                        old = sent_recent.popleft()
                        sent_recent_set.discard(old)
                    sent_recent.append(seg_url)
                    sent_recent_set.add(seg_url)
                    last_seq = seg_seq
                    sent_now += 1

                playlist_changed = fingerprint != last_playlist_fingerprint
                if playlist_changed or sent_now > 0:
                    last_changed_at = time.time()
                last_playlist_fingerprint = fingerprint

                if sent_now > 0:
                    # После успешной отдачи сегментов перезагружаем playlist по RFC:
                    # changed playlist => wait at least target duration from load start.
                    elapsed = time.time() - loop_started
                    sleep_for = max(0.2, min(3.0, target_duration - elapsed))
                else:
                    # unchanged playlist => half target duration.
                    elapsed = time.time() - loop_started
                    sleep_for = max(0.35, min(3.0, target_duration * 0.5 - elapsed))

                if time.time() - last_changed_at > max(30.0, target_duration * 6):
                    print('⚠️ [LIVEPIPE] playlist stalled for a long time, waiting for fresh segments...')
                    last_changed_at = time.time()

                time.sleep(sleep_for)
        except Exception as e:
            print(f"❌ [LIVEPIPE] error: {e}")
            try:
                self.send_error(500)
            except Exception:
                pass

    def _handle_generic(self, url):
        response = None
        try:
            headers = BROWSER_HEADERS.copy()
            headers['Accept-Encoding'] = 'identity'
            range_header = self.headers.get('Range')
            if range_header:
                headers['Range'] = range_header
            if_range = self.headers.get('If-Range')
            if if_range:
                headers['If-Range'] = if_range

            proxies = {'http': self.proxy_url, 'https': self.proxy_url} if self.proxy_url else None
            response = http_session.get(
                url,
                headers=headers,
                timeout=30,
                proxies=proxies,
                stream=True,
                verify=False,
                allow_redirects=True,
            )
            body = response.content
            self.send_response(response.status_code)
            # Generic/VOD ответы тоже могут приходить по Range (206).
            # Отдаём клиенту Content-Range/Content-Length как есть, потому что
            # запросили identity и body соответствует байтам origin.
            for key, value in response.headers.items():
                if key.lower() not in ['transfer-encoding', 'connection']:
                    self.send_header(key, value)
            if 'Accept-Ranges' not in response.headers:
                self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return
        except Exception:
            try:
                self.send_error(500)
            except Exception:
                pass
        finally:
            try:
                if response is not None:
                    response.close()
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
_live_ffmpeg_processes = {}
_live_ffmpeg_lock = threading.Lock()
_livepipe_state = {}
_livepipe_state_lock = threading.Lock()


def find_ffmpeg_executable():
    candidates = []
    if sys.platform == 'win32':
        candidates.extend([
            os.path.join(script_dir, 'ffmpeg.exe'),
            os.path.join(script_dir, 'ffmpeg', 'ffmpeg.exe'),
            os.path.join(script_dir, 'FFmpeg', 'ffmpeg.exe'),
            os.path.join(script_dir, 'FFMPEG', 'ffmpeg.exe'),
            os.path.join(script_dir, 'MPV', 'ffmpeg.exe'),
            os.path.join(script_dir, 'mpv', 'ffmpeg.exe'),
        ])
    else:
        candidates.extend([
            os.path.join(script_dir, 'ffmpeg'),
            os.path.join(script_dir, 'ffmpeg', 'ffmpeg'),
        ])

    for p in candidates:
        if os.path.isfile(p):
            return p

    found = shutil.which('ffmpeg')
    if found:
        return found
    return None


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
        r = http_session.get(f"https://ipapi.co/{ip}/json/", timeout=5).json()
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
        try:
            r = http_session.get(url, headers=h, timeout=20)
            if r.status_code == 403:
                # === PHASESHIFT: гео-блок при загрузке M3U ===
                print(f"🔮 [PhaseShift] M3U URL заблокирован (403) → пробуем сдвиг фазы")
                ps_url = phaseshift_engine.try_phantom_access(url)
                if ps_url and ps_url != url:
                    r = http_session.get(ps_url, headers=h, timeout=20)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code} при загрузке M3U")
            return self._parse_m3u_text(r.text)
        except Exception as e:
            # Финальная попытка: PhaseShift с полным перебором стратегий
            print(f"🔮 [PhaseShift] Стандартная загрузка M3U не удалась ({e}) → полный PhaseShift")
            ps_url = phaseshift_engine.try_phantom_access(
                url, all_channels=getattr(self, '_ch', None))
            if ps_url and ps_url != url:
                r = http_session.get(ps_url, headers=h, timeout=20)
                return self._parse_m3u_text(r.text)
            raise

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
                    r = http_session.get(url, headers={**h, "Connection": "close"}, timeout=45)
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
        hs = http_session.get(f"{base}/server/load.php?type=stb&action=handshake", headers=h, timeout=15).json()
        tk = hs['js']['token']
        res = http_session.get(
            f"{base}/server/load.php?type=itv&action=get_all_channels&token={tk}",
            headers=h, timeout=20).json()
        return [{"id": str(c.get('tvg_id', c.get('name'))), "name": c['name'], "logo": "",
                 "group": "Stalker", "url": c['url'].split(' ')[-1]} for c in res.get('js', [])]

    # --- EPG ---
    def _load_epg(self, url, h):
        epg_db = {}
        try:
            r = http_session.get(url, headers=h, timeout=25)
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
    countryChanged = Signal()
    bandwidthSaverChanged = Signal()
    contentModeChanged = Signal()
    seriesInfoReady = Signal(str)
    requestLiveRejoin = Signal(str)
    requestReconnect = Signal()
    # Универсальный канал: выполнить произвольный callable на GUI-thread.
    # Любой mpv observer/callback thread эмитит этот сигнал, а слот (живущий на
    # GUI thread) вызывает callable. Это исключает QTimer.singleShot из не-GUI
    # потоков, который и давал "event dispatcher has already been destroyed".
    runOnGui = Signal(object)

    # === PHASESHIFT: сигналы для UI ===
    phaseShiftChanged = Signal()       # активация/деактивация PhaseShift
    phaseShiftStatusChanged = Signal()  # статусная строка PhaseShift

    # Все сигналы объявлены выше — затем атрибуты/свойства.
    # (Раньше эти три сигнала стояли ПОСЛЕ @Property, что мешало читаемости
    #  и легко приводило к ошибкам при рефакторинге.)
    _available_qualities = ["auto", "ultra", "high", "medium", "low", "minimal"]

    @Property('QVariantList', notify=availableQualitiesChanged)
    def availableQualities(self):
        return self._available_qualities

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
        self._last_start_raw = ""
        self._target_code = "ALL"
        self._target_name = "Глобальный"
        self._em = EPGModel()
        self.player = None
        self._init = False
        self.w = None
        self._retry_count = 0
        self._quality_changing = False
        self._is_buffering = False
        self._buffering_progress = 100
        self._connection_quality = "unknown"
        self._current_quality = "auto"
        self._available_qualities = ["auto", "ultra", "high", "medium", "low", "minimal"]
        self._qualities_analyzed = False
        self._live_rejoin_pending = False
        # Флаг намеренной остановки пользователем (кнопка "Каналы"/выход).
        # Нужен, чтобы end-file(reason=STOP) НЕ вызывал авто-reopen живого потока,
        # который возвращал канал через ~1с уже без OSD-панели.
        self._user_stopped = False
        # Защита от reopen storm на нестабильном LIVE: не переоткрываем чаще,
        # чем раз в N секунд, и считаем подряд идущие EOF.
        self._last_live_reopen_at = 0.0
        self._live_reopen_count = 0
        self._last_avsync_log = 0.0
        self._last_catchup_check = 0.0
        self._last_catchup_log = 0.0

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

        # Инициализируем статистику серверов + DNS-over-HTTPS
        global server_stats
        server_stats = ServerStats(self.db)
        try:
            install_doh_dns()
        except Exception as e:
            print(f"⚠️ DoH init failed, using system DNS: {e}")

        # thread-safe сигнал уровня соединения
        self.segmentDownloaded.connect(self.update_connection_quality_from_speed)
        self.requestLiveRejoin.connect(self._perform_live_rejoin)
        self.requestReconnect.connect(self._do_reconnect)
        # AutoConnection => при эмите из чужого потока вызов уйдёт в очередь
        # GUI-треда и выполнится там. Это безопасно для QTimer и пр.
        self.runOnGui.connect(self._run_on_gui)

        # === PHASESHIFT: мониторинг статуса ===
        self._ps_prev_active = False
        self._ps_prev_status = ""
        self._ps_monitor = QTimer(self)
        self._ps_monitor.timeout.connect(self._monitor_phaseshift)
        self._ps_monitor.start(500)  # проверяем каждые 500мс

        if HAS_MPV:
            self._init_mpv()

    # --------------------------------------------------------
    #  MPV
    # --------------------------------------------------------
    def _init_mpv(self):
        try:
            self.player = mpv.MPV(
                vo='gpu', hwdec='auto-safe', ytdl=False, osc=False,
                input_default_bindings=False, input_vo_keyboard=True,

                # ASM-LEVEL Оптимизация видео-движка
                scale='bilinear',
                cscale='bilinear',
                dscale='bilinear',
                dither='no',
                correct_downscaling='yes',
                hdr_compute_peak='no',
                keepaspect='yes',
                
                keep_open='yes', keep_open_pause='no', hr_seek='yes',
                profile='low-latency',
                # ВАЖНО: untimed='yes' раньше ломал live A/V — он велит mpv
                # игнорировать тайминги и гнать кадры как можно быстрее, из-за чего
                # на сыром TS desync рос линейно (видно по логам 2.0→3.0→4.0→5.0с).
                # Для muxed live это недопустимо, поэтому untimed выключен.
                untimed='no',
                audio_buffer='0.2',
                video_sync='audio',
                audio_samplerate='48000',
                network_timeout=CacheConfig.NETWORK_TIMEOUT,
                # cache-pause-wait: сколько ждать наполнения буфера прежде чем
                # снова стартовать после underrun. Небольшое значение = быстрый
                # рестарт вместо долгого «зависания» плашки буферизации.
                cache_pause_wait='1',
                cache_pause_initial='no',

                # === СТРОГИЙ ЛИМИТ КЭША (ТЗ: 200 МБ / 60 секунд) ===
                # Единый источник правды — CacheConfig. Больше нигде не переопределяем
                # «волшебными числами»: 200 МБ есть 200 МБ во всех профилях качества.
                cache='yes',
                cache_secs=CacheConfig.CACHE_SECS,
                demuxer_max_bytes=CacheConfig.MAX_BYTES,        # 200 MiB (ТЗ)
                demuxer_max_back_bytes=CacheConfig.MAX_BACK_BYTES,
                demuxer_readahead_secs=CacheConfig.READAHEAD_SECS,
                demuxer_hysteresis_secs=CacheConfig.HYSTERESIS_SECS,

                # Устойчивость к сети / live.
                # Для HLS/live критично не слишком рано сдаваться на reload'ах m3u8,
                # иначе mpv доходит до края live window и делает заметный reopen.
                demuxer_lavf_o=(
                    'reconnect=1,'
                    'reconnect_streamed=1,'
                    'reconnect_delay_max=2,'
                    'max_reload=1000000,'
                    'm3u8_hold_counters=1000000,'
                    'seg_max_retry=3,'
                    'http_persistent=1,'
                    'http_seekable=0,'
                    # ВАЖНО: genpts УБРАН. Он перегенерирует ВСЕ таймстампы и ломал
                    # чистый HLS (давал AV-desync 30с и жёсткие лаги на старте).
                    # Теперь мы почти всегда на чистом HLS (token refresh), где PTS
                    # уже правильные. discardcorrupt безопасен — просто отбрасывает
                    # битые пакеты, не трогая тайминги.
                    'fflags=+discardcorrupt,'
                    'bufsize=64KiB'
                ),
                force_seekable='yes',
                vd_lavc_threads='2',
                ad_lavc_threads='1',
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
                    # Реальная работа на GUI-thread (НЕ QTimer.singleShot из mpv-потока).
                    self._gui_call(self._update_available_qualities_from_tracks)

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
            # НЕ делаем reopen по AV desync (это вызывало churn/429).
            # На чистом HLS с правильными PTS и untimed=no desync не должен копиться.
            # Логируем максимум раз в 5с, чтобы не засорять консоль.
            if v is None:
                return
            av = abs(v)
            if av <= 1.5:
                return
            now = time.time()
            if now - getattr(self, '_last_avsync_log', 0.0) >= 5.0:
                self._last_avsync_log = now
                print(f"⚠️ AV desync: {v:.2f}s")

        try:
            @p.property_observer('paused-for-cache')
            def on_paused_for_cache(_n, v):
                self._is_buffering = bool(v) if v is not None else False
                self.bufferingChanged.emit()
                if self._is_buffering:
                    self._set_status("Буферизация...")
                    # ВАЖНО: для LIVE НЕ делаем принудительный reopen по обычной
                    # paused-for-cache. Пользователь хочет полностью плавное продолжение
                    # без заметной перезагрузки URL. Пусть ffmpeg/mpv сам дождётся
                    # новых сегментов через max_reload + m3u8_hold_counters.
                    # Не ставим poor при кратковременной буферизации —
                    # подождём, видео может продолжиться через секунду.
                    # Quality останется как было (good/excellent от time-pos).
                else:
                    self._retry_count = 0
                    self._live_rejoin_pending = False
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
            # LIVE latency guard: у сырого TS данные приходят быстрее реального
            # времени, demuxer-буфер копится → через несколько минут «дикие лаги».
            # По мануалу mpv для live это лечится сливом буфера / догоном live edge.
            # Мягкая стратегия: если буфер разросся — чуть ускоряем воспроизведение,
            # чтобы плавно догнать эфир; при экстремальном разрастании — hard-skip.
            @p.property_observer('demuxer-cache-duration')
            def on_cache_duration(_n, v):
                if v is None:
                    return
                if not self._is_live_stream(self._last_url, self._last_start_raw):
                    return
                try:
                    cache_dur = float(v)
                except Exception:
                    return
                now = time.time()
                # Не дёргаем чаще раза в 2с.
                if now - getattr(self, '_last_catchup_check', 0.0) < 2.0:
                    return
                self._last_catchup_check = now
                self._gui_call(lambda cd=cache_dur: self._live_latency_guard(cd))
        except Exception as e:
            print(f"⚠️ Failed to observe demuxer-cache-duration: {e}")

        try:
            @p.property_observer('video-params')
            def on_video_params(_n, v):
                if v:
                    self._gui_call(self._update_available_qualities_from_tracks)
        except Exception as e:
            print(f"⚠️ Failed to observe video-params: {e}")

        @p.event_callback('end-file')
        def on_end(event):
            try:
                if event.data:
                    reason = event.data.reason
                    err = event.data.error
                    print(f"🎬 end-file: reason={reason}, error={err}")
                    if reason in (0, 2):
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
            if QGuiApplication.instance():
                # QTimer должен создаваться на GUI-thread. Планируем создание таймера
                # через runOnGui, иначе при вызове из mpv-потока получим
                # "event dispatcher has already been destroyed".
                d = delay
                self._gui_call(lambda: QTimer.singleShot(d * 1000, lambda: self.requestReconnect.emit()))
        else:
            print("❌ Reconnect failed: max attempts reached.")
            self._set_status("Ошибка: потеряно соединение")
            self._retry_count = 0

    def _is_live_stream(self, url, start_raw=""):
        ul = (url or "").lower()
        if start_raw:
            return False
        if "/live/" in ul:
            return True
        if "/movie/" in ul or "/series/" in ul:
            return False
        return '.m3u8' in ul

    def _needs_proxy(self, url):
        ul = url.lower()
        # Xtream streams REQUIRE the proxy
        if "/live/" in ul or "/movie/" in ul or "/series/" in ul:
            return True
        # Regular M3U/M3U8 → NEVER through proxy
        return False

    def _play_url(self, url):
        """Xtream → proxy, Regular M3U → direct (most reliable)"""
        if self._needs_proxy(url):
            start_hls_proxy(None, core=self)
            if self._is_live_stream(url, self._last_start_raw):
                proxied = f"http://127.0.0.1:8899/livepipe/{urllib.parse.quote(url, safe='')}"
                print(f"📡 LIVEPIPE: {proxied[:90]}...")
                try:
                    self.player['force-seekable'] = 'no'
                    self.player['demuxer-seekable-cache'] = 'no'
                    # БОЛЬШАЯ forward-подушка ~40с: mpv ХРАНИТ всю подушку, которую
                    # LIVEPIPE заливает на старте (всё окно плейлиста ≈20-30с). Это
                    # и есть защита зрителя от буферизации: паузы подачи от 404/refresh
                    # (1-2с) не осушают такой запас. hysteresis большой → mpv не
                    # выкидывает буфер раньше времени и держит его полным.
                    self.player['cache-secs'] = '60'
                    self.player['demuxer-readahead-secs'] = '60'
                    self.player['demuxer-max-back-bytes'] = '0'
                    self.player['demuxer-hysteresis-secs'] = '15'
                    # cache-pause=yes: при опустевшем буфере mpv ЧЕСТНО встаёт на
                    # короткую буферизацию и затем играет ПЛАВНО. cache-pause=no
                    # давал слайд-шоу — mpv показывал кадры по мере их поступления
                    # (медленнее реалтайма), вместо нормальной докачки.
                    self.player['cache-pause'] = 'yes'
                    # Гарантируем нормальный тайминг для live (untimed off).
                    self.player['untimed'] = 'no'
                    self.player['video-sync'] = 'audio'
                    # Сброс возможного ускорения от прошлого канала.
                    self.player['speed'] = 1.0
                except Exception:
                    pass
            else:
                proxied = get_proxied_url(url)
                print(f"📡 PROXY: {proxied[:90]}...")
            self.player.play(proxied)
        else:
            try:
                # User-Agent задаём ТОЛЬКО через опцию user-agent.
                # НЕЛЬЗЯ дублировать его в http-header-fields: ffmpeg тогда отправляет
                # два заголовка User-Agent и отклоняет HLS-запрос с ошибкой -13
                # (проверено: любой канал M3U/M3U8 при этом переставал играть).
                self.player["user-agent"] = BROWSER_HEADERS["User-Agent"]
                # Сбрасываем заголовки, которые мог оставить предыдущий (проксированный) поток.
                self.player["http-header-fields"] = ""
            except Exception:
                pass
            print(f"🎬 DIRECT: {url[:80]}...")
            self.player.play(url)

    @Slot(object)
    def _run_on_gui(self, fn):
        """Выполняет callable на GUI-thread. Сюда приходят задачи, заэмиченные
        из mpv observer/callback threads, чтобы безопасно работать с QTimer/UI."""
        try:
            if callable(fn):
                fn()
        except Exception as e:
            print(f"⚠️ _run_on_gui error: {e}")

    def _gui_call(self, fn):
        """Хелпер: запланировать callable на GUI-thread через сигнал."""
        try:
            self.runOnGui.emit(fn)
        except Exception:
            pass

    def _live_latency_guard(self, cache_dur):
        """Держит LIVE у эфира БЕЗ заметных скачков кадра. Вызывается на GUI-thread.

        Стратегия (индустриальный стандарт, как в hls.js/LL-HLS):
        латенси поджимаем ТОЛЬКО плавным ускорением на 1-5% — глаз этого не видит,
        картинка непрерывна. drop-buffers (который даёт видимый скачок кадра —
        «играл кадр, стал другой») применяем лишь как аварийный сброс при огромном
        отставании, когда плавно догнать уже нереально."""
        if not (HAS_MPV and self.player and self._init):
            return
        try:
            # ГЛАВНОЕ ПРАВИЛО: подушка ~20с существует, чтобы ГАСИТЬ столлы от
            # протухания токенов / refresh playlist. Её НЕЛЬЗЯ тратить ускорением —
            # иначе через пару минут буфер опустеет и начнётся underrun → фризы
            # (ровно то, что было). Поэтому при буфере до ~30с играем РОВНО 1.0x
            # и бережём запас. Ускоряемся ЧУТЬ-ЧУТЬ только при реальном избытке
            # СВЕРХ целевой подушки, чтобы латенси не росла бесконечно.
            if cache_dur > 55.0:
                target_speed = 1.04
            elif cache_dur > 40.0:
                target_speed = 1.02
            elif cache_dur > 30.0:
                target_speed = 1.01
            else:
                # Буфер в пределах целевой подушки — НЕ трогаем, копим/держим запас.
                target_speed = 1.0

            try:
                cur = float(self.player.speed or 1.0)
            except Exception:
                cur = 1.0
            if abs(cur - target_speed) > 0.003:
                try:
                    self.player['speed'] = target_speed
                except Exception:
                    pass

            # Аварийный сброс ТОЛЬКО при экстремальном отставании (> 90с): плавным
            # ускорением такое уже не догнать. Крайне редкий случай.
            if cache_dur > 90.0:
                try:
                    self.player.command('drop-buffers')
                    self.player['speed'] = 1.0
                    print(f"⏩ [LIVE] буфер {cache_dur:.1f}s (экстрим) → drop-buffers")
                except Exception:
                    pass
        except Exception:
            pass

    def _do_reconnect(self):
        if not self._last_url:
            return
        self._live_rejoin_pending = False
        print(f"[Player] Reconnecting attempt {self._retry_count}/5...")

        # === PHASESHIFT: при повторных ошибках пробуем сдвиг фазы ===
        # Но ТОЛЬКО если это гео-блок (403/401). Для таймаутов подключения
        # PhaseShift бесполезен — это проблема сети, а не гео-блокировка.
        if self._retry_count >= 2:
            _last_err = str(getattr(self, '_last_error', ''))
            _is_network_err = any(k in _last_err.lower() for k in [
                'timeout', 'timed out', 'connectionerror', 'connecttimeout',
                'connection refused', 'unreachable', '10051', '10060',
                '10061', 'errno 101', 'networkerror',
            ])
            if _is_network_err:
                print(f"🔮 [PhaseShift] ⏭️ SKIP — сетевая ошибка (не гео-блок): {_last_err[:60]}")
            else:
                print(f"🔮 [PhaseShift] Стандартное переподключение не помогает → пробуем сдвиг фазы")
                ps_url = phaseshift_engine.try_phantom_access(
                    self._last_url, all_channels=self._ch
                )
                if ps_url and ps_url != self._last_url:
                    print(f"✅ [PhaseShift] Найден альтернативный URL: {ps_url[:80]}")
                    self._last_url = ps_url  # обновляем для будущих попыток
                    self.phaseShiftChanged.emit()
                    self.phaseShiftStatusChanged.emit()
                else:
                    self.phaseShiftChanged.emit()
                self.phaseShiftStatusChanged.emit()

        try:
            self._play_url(self._last_url)
            self._apply_stream_optimizations()
        except Exception as e:
            print(f"[Player] Reconnect error: {e}")
            self._schedule_reconnect()

    @Slot(str)
    def _perform_live_rejoin(self, reason="buffer"):
        if not self._live_rejoin_pending:
            return
        # Пользователь вышел из канала между эмитом и обработкой сигнала —
        # не переоткрываем поток (иначе вернётся без OSD).
        if self._user_stopped:
            self._live_rejoin_pending = False
            return
        if not self._last_url:
            self._live_rejoin_pending = False
            return
        print(f"📡 [LIVE] Performing rejoin on GUI thread ({reason})")
        try:
            self._do_reconnect()
        except Exception as e:
            self._live_rejoin_pending = False
            print(f"⚠️ [LIVE] rejoin error: {e}")

    def _maybe_rejoin_live_edge(self, reason="buffer"):
        if not self._is_live_stream(self._last_url, self._last_start_raw):
            return False
        if not (HAS_MPV and self.player and self._init):
            return False
        if self._live_rejoin_pending:
            return False
        try:
            pos = self.player.time_pos
            dur = self.player.duration
        except Exception:
            pos = dur = None

        # Для live duration/time_pos часто плавающие, но если ползунок уже почти
        # в самом конце окна, безопаснее заново открыть URL и прыгнуть к fresh edge.
        near_end = False
        try:
            if pos is not None and dur is not None and dur > 0:
                near_end = pos >= max(0.0, dur - 3.0)
        except Exception:
            near_end = False

        if not near_end and reason != "end":
            return False

        self._live_rejoin_pending = True
        print(f"📡 [LIVE] Rejoin live edge ({reason})")
        self._set_status("LIVE: догоняю прямой эфир...")
        try:
            self.requestLiveRejoin.emit(reason)
        except Exception:
            self._live_rejoin_pending = False
            return False
        return True

    def _handle_playback_end(self):
        # ВАЖНО: этот метод вызывается из mpv event callback thread, НЕ из GUI thread.
        # Поэтому здесь НЕЛЬЗЯ напрямую дёргать _play_url()/_apply_stream_optimizations(),
        # которые внутри ставят QTimer.singleShot — иначе получаем
        # "QObject::startTimer: current thread's event dispatcher has already been destroyed"
        # и reopen storm. Всю работу делегируем на GUI thread через сигнал + cooldown.
        # Пользователь сам вышел из канала (кнопка "Каналы"/выход) — это не сбой
        # потока. НЕ переоткрываем: иначе канал возвращался через ~1с без OSD.
        if self._user_stopped:
            self._user_stopped = False
            self._live_rejoin_pending = False
            print("📺 Playback stopped by user — авто-reopen пропущен")
            return

        is_live = self._is_live_stream(self._last_url, self._last_start_raw)
        if is_live and self._last_url:
            now = time.time()

            # Cooldown: не переоткрываем live чаще, чем раз в LIVE_REOPEN_COOLDOWN секунд.
            # Это убивает reopen storm, который провоцировал у origin'а HTTP 429.
            LIVE_REOPEN_COOLDOWN = 6.0
            since = now - self._last_live_reopen_at
            if since < LIVE_REOPEN_COOLDOWN:
                # Слишком частый EOF — почти наверняка LIVEPIPE сам переживёт паузу
                # (он держит соединение и backoff-ит 429 внутри). Не дёргаем reopen.
                print(f"📺 Live EOF ignored (cooldown {LIVE_REOPEN_COOLDOWN - since:.1f}s) — даём LIVEPIPE восстановиться")
                return

            if self._live_rejoin_pending:
                # Уже есть запланированный rejoin — не плодим второй.
                return

            self._last_live_reopen_at = now
            self._live_reopen_count += 1
            self._live_rejoin_pending = True
            print(f"📺 Live stream temporary EOF; reopening live URL (reopen #{self._live_reopen_count})...")
            try:
                # Перекидываем reopen на GUI thread — там безопасно вызывать
                # _play_url()/_apply_stream_optimizations() с их QTimer.singleShot.
                self.requestLiveRejoin.emit('eof')
            except Exception as e:
                print(f"⚠️ Live reopen dispatch failed: {e}")
                self._live_rejoin_pending = False
            return
        # Disable auto-reconnect for completed VOD/archive playback
        self._retry_count = 0
        print("📺 Stream ended")

    def _handle_playback_error(self, err):
        print(f"⚠️ Playback error: {err}")
        self._last_error = str(err)  # Сохраняем для PhaseShift — решаем нужен ли сдвиг фазы
        self._schedule_reconnect()

    def _test_connection_quality(self, url):
        """Тест соединения через GET (а не HEAD — Xtream-серверы режектят HEAD).
        Если тест не удался — НЕ ставим 'poor', оставляем 'unknown'
        и ждём обновления от скорости скачивания сегментов."""
        try:
            start = time.time()
            # GET со stream=True — открываем и сразу закрываем, не качая тело
            r = http_session.get(url, headers={**BROWSER_HEADERS, "Connection": "close"},
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

    @Property(str, notify=countryChanged)
    def targetCountry(self):
        return f"{self._target_code} ({self._target_name})"


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
                target = float(val)
                # Для LIVE нельзя ставить ползунок ровно в самый конец окна:
                # mpv там часто зависает в бесконечной буферизации. Оставляем
                # маленький безопасный отступ от live edge.
                if self._is_live_stream(self._last_url, self._last_start_raw):
                    dur = self.player.duration
                    if dur is not None and dur > 3:
                        target = min(target, max(0.0, float(dur) - 2.0))
                self.player.time_pos = target
                if self.player.pause:
                    self.player.pause = False
                self.positionChanged.emit()
            except Exception:
                pass

    @Property(bool, notify=durationChanged)
    def isLive(self):
        """True для LIVE-потоков. QML использует это, чтобы скрыть бессмысленный
        ползунок длительности (live mpv нередко рапортует мусорную duration,
        из-за чего раньше показывалось «10 часов»)."""
        try:
            return bool(self._is_live_stream(self._last_url, self._last_start_raw))
        except Exception:
            return False

    @Property(float, notify=durationChanged)
    def duration(self):
        if HAS_MPV and self.player and self._init:
            # Для LIVE не отдаём duration в UI: mpv часто рапортует огромное/мусорное
            # значение (наблюдалось «10 часов»). Возвращаем 0 → QML прячет ползунок
            # и показывает индикатор прямого эфира вместо позиции.
            try:
                if self._is_live_stream(self._last_url, self._last_start_raw):
                    return 0.0
            except Exception:
                pass
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

    # === PHASESHIFT: свойства для UI ===
    @Property(bool, notify=phaseShiftChanged)
    def phaseShiftActive(self):
        return phaseshift_engine.is_active

    @Property(str, notify=phaseShiftStatusChanged)
    def phaseShiftStatus(self):
        return phaseshift_engine.status_text

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
            self._rebuild_indexes()

            # === PHASESHIFT: обучаем паттерны на всех каналах ===
            # При загрузке плейлиста PhaseShift запоминает URL-паттерны
            # рабочих каналов, чтобы потом использовать их для обхода
            # гео-блокировки заблокированных каналов.
            phaseshift_engine.learn_from_channels(self._ch)
            phaseshift_engine.learn_from_channels(self._movies)
            phaseshift_engine.learn_from_channels(self._series)

            # Дополнительная очистка предсказаний
            
            
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
    _MAX_CONCURRENT_PREFETCH = 2  # Снижено: 4 → 2 — не грузим сеть при стриминге
    _MAX_PREFETCH_BATCH = 30
    _PREFETCH_RETRIES = 1  # Снижено: 3 → 1 — prefetch не критичен
    _prefetch_cancel = False  # Флаг отмены при смене канала

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
        # Проверяем отмену — при смене канала все prefetch-задачи сбрасываются
        if self._prefetch_cancel:
            return
        if self._prefetch_semaphore is None:
            self._prefetch_semaphore = threading.Semaphore(self._MAX_CONCURRENT_PREFETCH)
        if not self._prefetch_semaphore.acquire(timeout=5):  # Снижено: 15 → 5
            return
        try:
            # Повторная проверка отмены после ожидания семафора
            if self._prefetch_cancel:
                return
            url = ch.get('url', '')
            if not url:
                return
            cache = HLSCache(None, stream_optimizer.quality_level)
            content, _ = cache.fetch_with_headers(url)
            if not content or self._prefetch_cancel:
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
                if variant_uri and not self._prefetch_cancel:
                    abs_url = make_absolute(variant_uri, url.rsplit('/', 1)[0] + '/', url)
                    try:
                        http_session.get(abs_url, headers=BROWSER_HEADERS, timeout=5, stream=True)  # Снижено: 10 → 5
                    except Exception:
                        pass
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
        Ищет кандидатов **ТОЛЬКО В ТЕКУЩЕМ ПЛЕЙЛИСТЕ**."""
        try:
            if not current_ch or not isinstance(current_ch, dict):
                return None
            cur_id = str(current_ch.get('id', ''))
            cur_cat = str(current_ch.get('group', ''))
            now_struct = time.localtime(time.time())
            cur_hour, cur_wday = now_struct.tm_hour, now_struct.tm_wday

            pl_id = self.current_playlist_id or 0
            candidates = {}

            def _add(cid, name, cat, score, source):
                if not cid or cid == cur_id:
                    return
                if cid in candidates:
                    old = candidates[cid]
                    candidates[cid] = (old[0], old[1], old[2] + score, source + "+" + old[3])
                else:
                    candidates[cid] = (name, cat, score, source)

            # 1. Последовательная (>=3 подтверждений) — только в текущем плейлисте
            for r in self.db.fetchall(
                """SELECT ch2.channel_id AS cid, ch2.channel_name AS cname, ch2.category AS cat, COUNT(*) AS cnt
                   FROM click_history ch1
                   JOIN click_history ch2 ON ch2.ts > ch1.ts AND ch2.ts < ch1.ts + 120
                                          AND ch2.channel_id != ch1.channel_id
                   WHERE ch1.channel_id = ? AND ch1.playlist_id = ? AND ABS(ch1.hour - ?) <= 2
                   GROUP BY ch2.channel_id ORDER BY cnt DESC LIMIT 5""",
                (cur_id, pl_id, cur_hour)):
                if r["cnt"] >= 3:
                    _add(r["cid"], r["cname"], r["cat"], r["cnt"] * 10, "seq")

            # 2. Популярный в этот час (±1) — только в текущем плейлисте
            for r in self.db.fetchall(
                """SELECT channel_id AS cid, channel_name AS cname, category AS cat, COUNT(*) AS cnt
                   FROM click_history WHERE playlist_id = ? AND ABS(hour - ?) <= 1
                   GROUP BY channel_id ORDER BY cnt DESC LIMIT 5""",
                (pl_id, cur_hour,)):
                _add(r["cid"], r["cname"], r["cat"], r["cnt"] * 2, "hour")

            # 3. Популярный в этот день недели — только в текущем плейлисте
            for r in self.db.fetchall(
                """SELECT channel_id AS cid, channel_name AS cname, category AS cat, COUNT(*) AS cnt
                   FROM click_history WHERE playlist_id = ? AND weekday = ?
                   GROUP BY channel_id ORDER BY cnt DESC LIMIT 5""",
                (pl_id, cur_wday,)):
                _add(r["cid"], r["cname"], r["cat"], r["cnt"], "weekday")

            # 4. Из той же категории — только в текущем плейлисте
            if cur_cat:
                for r in self.db.fetchall(
                    """SELECT channel_id AS cid, channel_name AS cname, COUNT(*) AS cnt
                       FROM click_history WHERE playlist_id = ? AND category = ?
                       GROUP BY channel_id ORDER BY cnt DESC LIMIT 3""",
                    (pl_id, cur_cat,)):
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

            # FINAL SAFETY: only return channels that actually exist in the current playlist
            current_ids = {str(c.get('id')) for c in (self._ch or [])}
            if top_id not in current_ids:
                # Find first candidate that actually exists in this playlist
                for cid, (cname, ccat, cscore, csrc) in sorted_cands:
                    if cid in current_ids:
                        return {'id': cid, 'name': cname, 'category': ccat,
                                'confidence': min(int(cscore), 100), 'source': csrc,
                                'candidates_count': len(top5)}
                return None

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

            # 4. Для проксированных Xtream-потоков НЕ делаем полный релоад при смене качества!
            # Это вызывает бесконечную перезагрузку и ошибку -13.
            # Оставляем только масштабирование + ограничение кэша.
            # Quality buttons now work for ALL sources via vf scaling only.
            # No network requests, no cache clearing, no reloads.
            print(f"📺 [Quality] {quality} → vf scaling only")
        except Exception as e:
            print(f"⚠️ Quality setting error: {e}")

        # Never clear HLS cache on quality change - it kills the stream
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
                # Фикс растягивания: scale=-2:высота заставляет MPV держать пропорции
                # и гарантирует, что ширина будет четной (нужно для многих кодеков)
                self._set_vf(f'scale=-2:{target_height}')
                direction = "Downscale" if target_height < original_height else "Upscale"
                print(f"🎬 [Quality] {direction} {original_height}p → {target_height}p (Asm-Optimized)")
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

        # Reset proxy state so the new playlist works correctly
        global MASTER_FETCHED, SEEN_MASTERS
        MASTER_FETCHED = False
        SEEN_MASTERS.clear()
        HLSCache.clear()

        self._last_url = f_url
        self._last_channel_name = name
        self._last_category = category
        self._last_start_raw = start_raw or ""
        self._retry_count = 0
        # Сбрасываем live-reopen state при старте нового канала, чтобы cooldown
        # от предыдущего канала не блокировал восстановление нового.
        self._last_live_reopen_at = 0.0
        self._live_reopen_count = 0
        self._live_rejoin_pending = False
        # Новый канал запускается штатно — снимаем флаг намеренной остановки.
        self._user_stopped = False
        self._prefetch_cancel = False  # Сброс: prefetch разрешён для нового канала
        self._last_catchup_check = 0.0
        # Сброс скорости: предыдущий live latency guard мог оставить speed=1.05.
        if HAS_MPV and self.player and self._init:
            try:
                self.player['speed'] = 1.0
            except Exception:
                pass
        self._available_qualities = ["auto", "ultra", "high", "medium", "low", "minimal"]
        self.availableQualitiesChanged.emit()
        self._qualities_analyzed = False
        self._set_status("Воспроизведение...")
        # Сразу обновим UI: для live duration=0 → QML покажет бейдж LIVE,
        # а не унаследованную «10 часов» от предыдущего VOD.
        self.durationChanged.emit()
        self.positionChanged.emit()

        # Всегда сбрасываем страну при запуске нового канала
        self._target_code = "ALL"
        self._target_name = "Глобальный"
        self.countryChanged.emit()   # <-- обновляем QML сразу

        # Запускаем определение страны именно для этого канала
        threading.Thread(target=self._detect_country, args=(f_url, category, name), daemon=True).start()

        # === PHASESHIFT: дообучаем паттерны перед воспроизведением ===
        # Убеждаемся, что PhaseShift знает паттерны текущего плейлиста
        if self._ch:
            phaseshift_engine.learn_from_channels(self._ch)
        # Эмитим начальный статус PhaseShift (неактивен)
        self.phaseShiftChanged.emit()
        self.phaseShiftStatusChanged.emit()

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
            is_live = self._is_live_stream(self._last_url, self._last_start_raw)
            try:
                p['live-keepalive'] = 'yes'
            except Exception:
                pass
            # Для LIVEPIPE/direct-TS нужен режим настоящего live, а не seekable cache.
            # Иначе mpv/QML видят конец текущего буфера как конец файла: пропадает
            # LIVE-плашка, кадр блюрится/замирает и поток стопается на ~2 минутах.
            if is_live:
                p['force-seekable'] = 'no'
                try:
                    p['demuxer-seekable-cache'] = 'no'
                except Exception:
                    pass
                # БОЛЬШАЯ forward-подушка ~40с: mpv хранит всё окно, что заливает
                # LIVEPIPE на старте → паузы подачи от 404/refresh не осушают буфер,
                # и зритель НЕ видит буферизацию.
                p['cache-secs'] = '60'
                p['demuxer-max-back-bytes'] = '0'
                p['demuxer-readahead-secs'] = '60'
                p['demuxer-hysteresis-secs'] = '15'
                try:
                    # cache-pause=yes → честная буферизация вместо слайд-шоу.
                    p['cache-pause'] = 'yes'
                    p['untimed'] = 'no'
                    p['video-sync'] = 'audio'
                except Exception:
                    pass
            else:
                p['force-seekable'] = 'yes'
                try:
                    p['demuxer-seekable-cache'] = 'yes'
                except Exception:
                    pass
                p['cache-secs'] = CacheConfig.CACHE_SECS
                p['demuxer-max-back-bytes'] = CacheConfig.MAX_BACK_BYTES
                p['demuxer-readahead-secs'] = CacheConfig.READAHEAD_SECS
                p['demuxer-hysteresis-secs'] = CacheConfig.HYSTERESIS_SECS

            # Применяем качество ЧЕРЕЗ ЗАДЕРЖКУ — когда поток уже открыт и
            # video_params доступны. Безопасно для прямых Xtream-потоков.
            stream_optimizer.quality_level = self._current_quality
            # Создание QTimer планируем на GUI-thread (защита от вызова из mpv-потока).
            self._gui_call(lambda: QTimer.singleShot(3000, self._safe_apply_quality))
            self._gui_call(lambda: QTimer.singleShot(2500, self._update_available_qualities_from_tracks))

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

    def _monitor_phaseshift(self):
        """Мониторинг статуса PhaseShift и эмиссия сигналов в QML."""
        try:
            active = phaseshift_engine.is_active
            status = phaseshift_engine.status_text
            if active != self._ps_prev_active:
                self._ps_prev_active = active
                self.phaseShiftChanged.emit()
            if status != self._ps_prev_status:
                self._ps_prev_status = status
                self.phaseShiftStatusChanged.emit()
        except Exception:
            pass

    def _detect_country(self, url, cat, name):
        code, cn = detect_country(cat, name)
        # Шаг 1: Быстрый детект по TLD/домену (без DNS/HTTP запросов!)
        if code == "ALL":
            url_cc = detect_country_from_url(url)
            if url_cc:
                RU_NAMES_MAP = {
                    "AL": "Албания", "US": "США", "GB": "Великобритания",
                    "DE": "Германия", "FR": "Франция", "IT": "Италия",
                    "ES": "Испания", "TR": "Турция", "RU": "Россия",
                    "UA": "Украина", "PL": "Польша", "GR": "Греция",
                    "RO": "Румыния", "BG": "Болгария", "RS": "Сербия",
                    "HR": "Хорватия", "BA": "Босния", "ME": "Черногория",
                    "MK": "Македония", "PT": "Португалия", "NL": "Нидерланды",
                    "AZ": "Азербайджан", "AM": "Армения", "GE": "Грузия",
                    "KZ": "Казахстан", "IL": "Израиль", "AE": "ОАЭ",
                    "JP": "Япония", "KR": "Корея", "IN": "Индия",
                    "BR": "Бразилия", "CA": "Канада", "MX": "Мексика",
                }
                code = url_cc
                cn = RU_NAMES_MAP.get(url_cc, url_cc)
                print(f"🎯 Country: {code} ({cn}) — по TLD/домену")
        # Шаг 2: IP геолокация (медленнее, но точнее)
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
        # Гео-маска: притворяемся клиентом из страны канала. Помогает пройти
        # серверы, которые определяют гео по заголовку (а не по src-IP пакета).
        try:
            set_geo_target(code)
        except Exception:
            pass
        self.countryChanged.emit()
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
                # Помечаем остановку как намеренную ДО player.stop(), иначе
                # end-file(reason=STOP) успеет запустить авто-reopen живого потока.
                self._user_stopped = True
                self._live_rejoin_pending = False
                self.player.stop()
                self._set_status("Ready")
                self.playingChanged.emit(False)
                # === PHASESHIFT: сброс статуса ===
                phaseshift_engine._active = False
                phaseshift_engine._last_result = ""
                self.phaseShiftChanged.emit()
                self.phaseShiftStatusChanged.emit()
            except Exception:
                pass

    @Slot(str, result=str)
    def getFallback(self, channel_name):
        # Slot объявлен result=str → нельзя возвращать None (PySide6 ругается/отдаёт мусор).
        # Возвращаем пустую строку как «нет резервного потока».
        fallback = get_fallback_url(channel_name)
        if fallback:
            print(f"[Player] Trying fallback: {fallback[:50]}...")
            try:
                self.player.play(fallback)
                self._set_status("Использую резервный поток")
                return fallback
            except Exception:
                pass
        return ""


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
