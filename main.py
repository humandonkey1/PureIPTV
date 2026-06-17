import sys
import os
import requests
import re
import json
import gzip
import sqlite3
import locale
from datetime import datetime
from datetime import timezone
from xml.etree import ElementTree

from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtCore import QObject
from PySide6.QtCore import Slot
from PySide6.QtCore import Signal
from PySide6.QtCore import Property
from PySide6.QtCore import QAbstractListModel
from PySide6.QtCore import Qt
from PySide6.QtCore import QThread
from PySide6.QtCore import QVariant

# Настройка MPV
try:
    import mpv
    HAS_MPV = True
except ImportError:
    HAS_MPV = False

# ВОТ ОНО! locale вернулся для корректной работы чисел
try:
    locale.setlocale(locale.LC_NUMERIC, 'C')
except Exception:
    pass

class IPTVWorker(QThread):
    """Асинхронный комбайн: M3U, Xtream, Stalker V3 + EPG XMLTV."""
    finished = Signal(list, dict, str)
    error = Signal(str)

    def __init__(self, proto, host, epg_url="", user="", pwd="", mac=""):
        super().__init__()
        self.proto = proto
        self.host = host.strip().rstrip('/')
        self.epg_url = epg_url.strip()
        self.user = user
        self.pwd = pwd
        self.mac = mac
        self.ua = "Mozilla/5.0 (MAG200; Qt4-static) AppleWebKit/533.3"

    def run(self):
        try:
            channels = []
            epg_db = {}
            headers = {"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"}
            
            if self.epg_url:
                epg_db = self._parse_epg_full(self.epg_url, headers)

            if self.proto == "M3U":
                channels = self._parse_m3u_full(headers)
            elif self.proto == "XTREAM":
                channels = self._parse_xtream_full(headers)
            elif self.proto == "STALKER":
                channels = self._parse_stalker_full(headers)
            
            self.finished.emit(channels, epg_db, f"OK: {len(channels)}")
        except Exception as e:
            self.error.emit(str(e))

    def _parse_epg_full(self, url, h):
        r = requests.get(url, headers=h, timeout=30)
        data = r.content
        if url.endswith(".gz") or data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        tree = ElementTree.fromstring(data)
        epg = {}
        for p in tree.findall('programme'):
            cid = p.get('channel')
            if not cid: continue
            if cid not in epg: epg[cid] = []
            start = p.get('start').split(' ')[0]
            stop = p.get('stop').split(' ')[0]
            try:
                dt_s = datetime.strptime(start, "%Y%m%d%H%M%S")
                dt_e = datetime.strptime(stop, "%Y%m%d%H%M%S")
                display_time = f"{dt_s.strftime('%H:%M')} - {dt_e.strftime('%H:%M')}"
            except:
                display_time = "00:00"
            epg[cid].append({
                "title": p.findtext('title'),
                "desc": p.findtext('desc') or "Нет описания",
                "start": start,
                "time": display_time
            })
        return epg

    def _parse_m3u_full(self, h):
        r = requests.get(self.host, headers=h, timeout=20)
        p = re.compile(r'#EXTINF:.*?(?:tvg-id="(.*?)")?.*?(?:tvg-logo="(.*?)")?.*?(?:catchup="(.*?)")?.*?,(.*?)\n(http.*?)(?:\n|$)', re.M)
        return [{"id": m.group(1) or m.group(4).strip(), "logo": m.group(2) or "", "catchup": m.group(3) or "default", "name": m.group(4).strip(), "url": m.group(5).strip()} for m in p.finditer(r.text)]

    def _parse_xtream_full(self, h):
        api = f"{self.host}/player_api.php?username={self.user}&password={self.pwd}&action=get_live_streams"
        res = requests.get(api, headers=h, timeout=20).json()
        return [{"id": str(i.get('epg_channel_id', i.get('name'))), "name": i.get('name'), "logo": i.get('stream_icon', ''), "url": f"{self.host}/live/{self.user}/{self.pwd}/{i.get('stream_id')}.ts", "catchup": "default"} for i in res]

    def _parse_stalker_full(self, h):
        h["X-User-MAC"] = self.mac
        h["Cookie"] = f"mac={self.mac}"
        hs = requests.get(f"{self.host}/server/load.php?type=stb&action=handshake", headers=h, timeout=10).json()
        token = hs['js']['token']
        api = f"{self.host}/server/load.php?type=itv&action=get_all_channels&token={token}"
        data = requests.get(api, headers=h, timeout=15).json()
        return [{"id": str(c.get('tvg_id', c.get('name'))), "name": c['name'], "logo": "", "url": c['url'].split(' ')[-1], "catchup": "none"} for c in data.get('js', [])]

class EPGModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self._items = []
    def rowCount(self, p=None): return len(self._items)
    def data(self, index, role):
        if not index.isValid(): return None
        it = self._items[index.row()]
        if role == 201: return it.get("title")
        if role == 202: return it.get("time")
        if role == 203: return it.get("desc")
        if role == 204: return it.get("start")
        return None
    def roleNames(self): return {201: b"displayTitle", 202: b"displayTime", 203: b"desc", 204: b"startRaw"}
    def set_data(self, d):
        self.beginResetModel()
        self._items = d
        self.endResetModel()

class IPTVCore(QObject):
    statusChanged = Signal()
    loadFinished = Signal()

    def __init__(self):
        super().__init__()
        self._status, self._channels, self._epg_data = "Ready", [], {}
        self._epg_model = EPGModel()
        self.player = None
        if HAS_MPV:
            try:
                self.player = mpv.MPV(vo='libmpv', hwdec='auto', ytdl=True)
                self.player['cache'] = 'yes'
                self.player['demuxer-max-bytes'] = '128MiB'
                self.player['demuxer-readahead-secs'] = '20'
            except Exception: pass
        self._init_db_pro()

    def _init_db_pro(self):
        self.db = sqlite3.connect("premium.db", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS store (key TEXT PRIMARY KEY, val TEXT)")
        res = self.db.execute("SELECT val FROM store WHERE key='cache'").fetchone()
        if res:
            try:
                d = json.loads(res[0])
                self._channels, self._epg_data = d.get('ch', []), d.get('epg', {})
                self._status = f"Vault: {len(self._channels)}"
            except Exception: pass

    @Property(str, notify=statusChanged)
    def status(self): return self._status
    @Property(QVariant, notify=statusChanged)
    def channels(self): return self._channels
    @Property(QObject, constant=True)
    def epgModel(self): return self._epg_model

    @Slot(str, str, str, str, str, str)
    def connect(self, proto, host, epg, user, pwd, mac):
        self._status = "Connecting..."; self.statusChanged.emit()
        self.w = IPTVWorker(proto, host, epg, user, pwd, mac)
        self.w.finished.connect(self._on_load)
        self.w.error.connect(self._on_error)
        self.w.start()

    def _on_load(self, ch, epg, msg):
        self._channels, self._epg_data, self._status = ch, epg, msg
        self.statusChanged.emit(); self.loadFinished.emit()
        try:
            val = json.dumps({'ch': ch, 'epg': epg})
            self.db.execute("INSERT OR REPLACE INTO store VALUES ('cache', ?)", (val,))
            self.db.commit()
        except Exception: pass

    def _on_error(self, e):
        self._status = f"Error: {e}"; self.statusChanged.emit()

    @Slot(str)
    def updateEPG(self, cid):
        self._epg_model.set_data(self._epg_data.get(cid, []))

    @Slot(str, QVariant, str)
    def play(self, url, wid, start_raw=""):
        if not self.player: return
        f_url = url
        if start_raw:
            try:
                dt = datetime.strptime(start_raw, "%Y%m%d%H%M%S")
                utc = int(dt.replace(tzinfo=timezone.utc).timestamp())
                f_url = f"{url}?utc={utc}" if "?" not in url else f"{url}&utc={utc}"
            except Exception: pass
        try:
            self.player.wid = int(wid)
            self.player.play(f_url)
        except Exception: pass

    @Slot()
    def stop(self):
        if self.player: self.player.stop()

if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    engine = QQmlApplicationEngine()
    core = IPTVCore()
    engine.rootContext().setContextProperty("backend", core)
    engine.load(os.path.join(os.path.dirname(__file__), "main.qml"))
    sys.exit(app.exec())
