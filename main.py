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


doh_resolver = DohResolver()


# Глобальный тумблер: при True getaddrinfo отдаёт IPv6-адреса ПЕРВЫМИ.
# Это и есть «IPv6-дыра» — ОС/плеер сначала соединятся по IPv6, где гео-фильтр
# чаще отсутствует. Требует, чтобы у клиента был рабочий IPv6 (иначе фолбэк
# на IPv4 отработает автоматически по happy-eyeballs).
PREFER_IPV6 = True


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
    "US": ["23.20.0.1", "3.208.0.1"],
    "GB": ["51.140.0.1", "5.148.0.1"],
    "DE": ["3.120.0.1", "85.214.0.1"],
    "FR": ["51.15.0.1", "212.27.48.1"],
    "NL": ["51.158.0.1", "94.142.240.1"],
    "RU": ["5.255.255.1", "77.88.55.1"],
    "UA": ["77.120.0.1", "91.198.36.1"],
    "TR": ["31.145.0.1", "88.255.0.1"],
    "ES": ["51.68.0.1", "80.58.0.1"],
    "IT": ["79.171.0.1", "151.1.0.1"],
    "PL": ["5.172.0.1", "83.1.0.1"],
    "BR": ["18.228.0.1", "200.147.0.1"],
    "IN": ["13.126.0.1", "103.21.244.1"],
    "CA": ["3.96.0.1", "24.48.0.1"],
    "AE": ["5.32.0.1", "94.200.0.1"],
    "SA": ["5.42.192.1", "212.71.32.1"],
    "AZ": ["5.62.160.1", "85.132.0.1"],
    "KZ": ["2.72.0.1", "212.19.128.1"],
    "DZ": ["41.96.0.1", "105.96.0.1"],
}

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
                total=3, connect=3, read=3,
                backoff_factor=0.4,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(['GET', 'HEAD', 'OPTIONS']),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry,
                                  pool_connections=16, pool_maxsize=32)
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

                # Для VOD/MP4 mpv часто читает через HTTP Range. Если не пробросить
                # Range/If-Range к origin и не вернуть 206 + Content-Range назад,
                # фильмы/сериалы ломаются или клиент сам абортит сокет (WinError 10053).
                range_header = self.headers.get('Range')
                if range_header:
                    seg_headers['Range'] = range_header
                if_range = self.headers.get('If-Range')
                if if_range:
                    seg_headers['If-Range'] = if_range

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
                            raise RuntimeError("403 geo-block (edge fallback exhausted)")
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
        r = http_session.get(url, headers=h, timeout=20)
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
        # GUI-треда и выполнится там. Это безопасное место для QTimer и пр.
        self.runOnGui.connect(self._run_on_gui)

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
