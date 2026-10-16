"""Fetch, cache and parse lyrics.

NetEase first (LRC line timing + YRC word timing). When NetEase has nothing,
optionally fall back to other versions of the same song on NetEase, LRCLIB and
QQ Music, matched by title, artist and duration. Lines without real word timing
get estimated word timing so they can be revealed word by word.
"""
import bisect
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import requests

from config import DATA_DIR

CACHE_DIR = os.path.join(DATA_DIR, "cache")
PLAYING_LIST = os.path.expandvars(r"%LOCALAPPDATA%\NetEase\CloudMusic\webdata\file\playingList")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://music.163.com/"}
QQ_HEADERS = {"User-Agent": HEADERS["User-Agent"], "Referer": "https://y.qq.com/"}
LRCLIB_HEADERS = {"User-Agent": "ncm-desktop-lyrics/1.0"}
LYRIC_API = "https://music.163.com/api/song/lyric/v1?id={}&lv=-1&tv=-1&rv=-1&yv=-1&ytv=-1"
DETAIL_API = "https://music.163.com/api/song/detail/?ids=[{}]"
SEARCH_API = "https://music.163.com/api/search/get"
LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
QQ_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
QQ_LYRIC = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
AI_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcribe.py")
AI_TIMEOUT = 600             # seconds; the first run also downloads the models
MISS_TTL = 3 * 24 * 3600     # re-check songs without lyrics after a few days
DURATION_SLACK = 3.0         # seconds a fallback version may differ from the playing one

TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
OFFSET_TAG = re.compile(r"\[offset:\s*([+-]?\d+)\]", re.I)
YRC_LINE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
YRC_WORD = re.compile(r"\((\d+),(\d+),-?\d+\)")
CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
IS_CJK = re.compile(f"[{CJK}]")
ATOM = re.compile(rf"[{CJK}]|(?:(?![{CJK}])[^\W_])+(?:['’](?:(?![{CJK}])[^\W_])+)*")
OPENERS = "([{“‘「『《〈（【"
VOWELS = re.compile(r"[aeiouyàáâãäåèéêëìíîïòóôõöùúûüý]+", re.I)


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Line:
    start: float
    end: float
    text: str
    words: list = None
    trans: str = ""
    estimated: bool = False      # word timing guessed from line timing, not from the source


@dataclass
class Lyrics:
    lines: list = field(default_factory=list)
    pure_music: bool = False
    word_level: bool = False
    source: str = "网易云"

    def __post_init__(self):
        self._starts = [ln.start for ln in self.lines]

    def index_at(self, t):
        return bisect.bisect_right(self._starts, t) - 1


# -- parsing ---------------------------------------------------------------------

def parse_lrc(text):
    """[(seconds, text)] sorted; JSON credit lines are skipped."""
    out, offset = [], 0.0
    for raw in (text or "").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("{"):
            continue
        m = OFFSET_TAG.match(raw)
        if m:
            offset = int(m.group(1)) / 1000.0
            continue
        stamps, pos = [], 0
        while (m := TIME_TAG.match(raw, pos)):
            stamps.append(int(m.group(1)) * 60 + float(m.group(2).replace(":", ".")))
            pos = m.end()
        body = raw[pos:].strip()
        out.extend((max(0.0, s - offset), body) for s in stamps)
    out.sort(key=lambda x: x[0])
    return out


def parse_yrc(text):
    lines = []
    for raw in (text or "").splitlines():
        m = YRC_LINE.match(raw.strip())
        if not m:
            continue
        start, dur = int(m.group(1)) / 1000.0, int(m.group(2)) / 1000.0
        parts = YRC_WORD.split(m.group(3))
        words = []
        for i in range(1, len(parts) - 2, 3):
            ws, wd = int(parts[i]) / 1000.0, int(parts[i + 1]) / 1000.0
            if parts[i + 2]:
                words.append(Word(ws, ws + wd, parts[i + 2]))
        if words:
            lines.append(Line(start, start + dur, "".join(w.text for w in words), words))
    lines.sort(key=lambda ln: ln.start)
    return lines


def _is_placeholder(s):
    return not s or set(s) <= set("/ 　")


# -- word timing estimate for line-only lyrics -------------------------------------

def tokenize(text):
    """Split a line into reveal units: one CJK character or one Latin word each.

    Spaces and punctuation stick to the unit before them (opening brackets to the
    one after), so the units always join back into exactly `text`.
    """
    units, pending, pos = [], "", 0
    for m in ATOM.finditer(text):
        glue = text[pos:m.start()]
        cut = next((i for i, c in enumerate(glue) if c in OPENERS), len(glue))
        if units:
            units[-1] += glue[:cut]
        else:
            pending += glue[:cut]
        units.append(pending + glue[cut:] + m.group())
        pending, pos = "", m.end()
    tail = pending + text[pos:]
    if units:
        units[-1] += tail
    elif tail:
        units.append(tail)
    return units


def _syllables(unit):
    if IS_CJK.search(unit):
        return len(IS_CJK.findall(unit))
    word = re.sub(r"[^a-zA-Zà-ÿ0-9]", "", unit)
    if not word:
        return 0
    if word.isdigit():
        return max(1, len(word) // 2)
    count = len(VOWELS.findall(word))
    if count > 1 and word.lower().endswith("e") and not word.lower().endswith(("le", "ee")):
        count -= 1
    return max(1, count)


def estimate_words(line):
    units = tokenize(line.text)
    weights = [_syllables(u) for u in units]
    total = sum(weights)
    if not units or total == 0:
        return None
    span = max(line.end - line.start, 0.05)
    # Sing across most of the gap to the next line, but don't stretch a short
    # line over a long instrumental break.
    sing = min(span * 0.92, 0.42 * total + 0.2)
    words, acc = [], 0
    for unit, w in zip(units, weights):
        ws = line.start + sing * acc / total
        acc += w
        words.append(Word(ws, line.start + sing * acc / total, unit))
    return words


def build(api, duration=0.0, source="网易云"):
    yrc = parse_yrc((api.get("yrc") or {}).get("lyric"))
    lrc = parse_lrc((api.get("lrc") or {}).get("lyric"))
    trans = [(t, s) for t, s in parse_lrc((api.get("ytlrc") or api.get("tlyric") or {}).get("lyric"))
             if not _is_placeholder(s)]
    if yrc:
        lines, tolerance = yrc, 1.0
    else:
        lines, tolerance = [], 0.35
        for i, (t, s) in enumerate(lrc):
            end = lrc[i + 1][0] if i + 1 < len(lrc) else max(t + 8.0, duration)
            lines.append(Line(t, end, s))
        for ln in lines:
            if ln.text:
                ln.words = estimate_words(ln)
                ln.estimated = ln.words is not None
    if trans:
        times = [t for t, _ in trans]
        for ln in lines:
            if not ln.text:
                continue
            j = bisect.bisect_left(times, ln.start)
            best = min((k for k in (j - 1, j) if 0 <= k < len(trans)),
                       key=lambda k: abs(times[k] - ln.start), default=None)
            if best is not None and abs(times[best] - ln.start) <= tolerance:
                ln.trans = trans[best][1]
    return Lyrics(lines, pure_music=bool(api.get("pureMusic")), word_level=bool(yrc), source=source)


# -- cache helpers -----------------------------------------------------------------

def _read_cache(path, has_content):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if has_content(data) or time.time() - os.path.getmtime(path) < MISS_TTL:
        return data
    return None


def _write_cache(path, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _has_lines(api):
    return bool(api) and any(parse_lrc((api.get("lrc") or {}).get("lyric")) or
                             (api.get("yrc") or {}).get("lyric") for _ in [0])


def netease_api(song_id):
    path = os.path.join(CACHE_DIR, f"{song_id}.json")
    api = _read_cache(path, _has_lines)
    if api is None:
        resp = requests.get(LYRIC_API.format(song_id), headers=HEADERS, timeout=8)
        resp.raise_for_status()
        api = resp.json()
        if api.get("code") != 200:
            raise RuntimeError(f"lyric api code {api.get('code')}")
        _write_cache(path, api)
    return api


# -- song info ---------------------------------------------------------------------

def song_meta(song_id):
    """{'title', 'artists', 'album', 'duration'} from the client's queue, else the web API."""
    try:
        with open(PLAYING_LIST, encoding="utf-8") as f:
            items = json.load(f).get("list", [])
        for item in items:
            track = item.get("track") or {}
            if str(track.get("id")) == song_id or str(item.get("id")) == song_id:
                return {"title": track.get("name", ""),
                        "artists": [a.get("name", "") for a in track.get("artists") or []],
                        "album": (track.get("album") or {}).get("name", ""),
                        "duration": (track.get("duration") or 0) / 1000.0}
    except (OSError, ValueError):
        pass
    try:
        song = requests.get(DETAIL_API.format(song_id), headers=HEADERS, timeout=6).json()["songs"][0]
        return {"title": song.get("name", ""),
                "artists": [a.get("name", "") for a in song.get("artists") or []],
                "album": (song.get("album") or {}).get("name", ""),
                "duration": (song.get("duration") or 0) / 1000.0}
    except Exception:
        return {"title": "", "artists": [], "album": "", "duration": 0.0}


def display_title(meta):
    return f"{meta['title']} - {' / '.join(meta['artists'])}".strip(" -")


# -- fallback sources ---------------------------------------------------------------

def _norm(s):
    s = re.sub(r"[(（\[【].*?[)）\]】]", "", s or "")
    return re.sub(r"[\W_]+", "", s).lower()


def _same_song(meta, title, artists, duration):
    want, got = _norm(meta["title"]), _norm(title)
    if not want or not got or (want != got and not (want in got or got in want)):
        return False
    mine = {_norm(a) for a in meta["artists"] if _norm(a)}
    theirs = {_norm(a) for a in artists if _norm(a)}
    if mine and theirs and not (mine & theirs or any(a in b or b in a for a in mine for b in theirs)):
        return False
    if meta["duration"] and duration and abs(meta["duration"] - duration) > DURATION_SLACK:
        return False
    return True


def _from_netease_versions(meta, song_id):
    params = {"s": f"{meta['title']} {' '.join(meta['artists'][:2])}", "type": 1, "limit": 20}
    songs = requests.get(SEARCH_API, params=params, headers=HEADERS, timeout=8).json() \
        .get("result", {}).get("songs") or []
    for s in songs:
        if str(s["id"]) == song_id:
            continue
        if not _same_song(meta, s.get("name"), [a.get("name", "") for a in s.get("artists") or []],
                          (s.get("duration") or 0) / 1000.0):
            continue
        api = netease_api(str(s["id"]))
        if _has_lines(api) and not api.get("pureMusic"):
            return api
    return None


def _from_lrclib(meta):
    params = {"track_name": meta["title"], "artist_name": ", ".join(meta["artists"])}
    if meta["album"]:
        params["album_name"] = meta["album"]
    if meta["duration"]:
        params["duration"] = round(meta["duration"])
    candidates = []
    r = requests.get(LRCLIB_GET, params=params, headers=LRCLIB_HEADERS, timeout=8)
    if r.status_code == 200:
        candidates.append(r.json())
    else:
        r = requests.get(LRCLIB_SEARCH, params={"track_name": meta["title"],
                                                "artist_name": " ".join(meta["artists"][:2])},
                         headers=LRCLIB_HEADERS, timeout=8)
        if r.status_code == 200:
            candidates.extend(r.json() or [])
    for c in candidates:
        if c.get("instrumental"):
            return {"pureMusic": True}
        if c.get("syncedLyrics") and _same_song(meta, c.get("trackName"), [c.get("artistName", "")],
                                                c.get("duration") or 0):
            return {"lrc": {"lyric": c["syncedLyrics"]}}
    return None


def _from_qq(meta):
    params = {"w": f"{meta['title']} {' '.join(meta['artists'][:2])}", "format": "json", "n": 10, "p": 1, "cr": 1}
    songs = requests.get(QQ_SEARCH, params=params, headers=QQ_HEADERS, timeout=8).json() \
        .get("data", {}).get("song", {}).get("list") or []
    for s in songs:
        if not _same_song(meta, s.get("songname"), [a.get("name", "") for a in s.get("singer") or []],
                          s.get("interval") or 0):
            continue
        j = requests.get(QQ_LYRIC, params={"songmid": s["songmid"], "format": "json", "nobase64": 1, "g_tk": 5381},
                         headers=QQ_HEADERS, timeout=8).json()
        lrc = html.unescape(j.get("lyric") or "")
        if parse_lrc(lrc):
            return {"lrc": {"lyric": lrc}, "tlyric": {"lyric": html.unescape(j.get("trans") or "")}}
    return None


FALLBACKS = [("网易云其他版本", _from_netease_versions), ("LRCLIB", _from_lrclib), ("QQ音乐", _from_qq)]


def find_elsewhere(song_id, meta):
    """(api_like_dict, source_name) from the first fallback that has synced lyrics, else (None, None)."""
    path = os.path.join(CACHE_DIR, f"{song_id}.alt.json")
    cached = _read_cache(path, lambda d: bool(d.get("source")))
    if cached is not None:
        return cached.get("api"), cached.get("source")
    if not meta["title"]:
        return None, None
    errors = 0
    for name, fn in FALLBACKS:
        try:
            api = fn(meta, song_id) if fn is _from_netease_versions else fn(meta)
        except Exception:
            errors += 1
            continue
        if api:
            if api.get("pureMusic"):
                break
            _write_cache(path, {"source": name, "api": api})
            return api, name
    if errors < len(FALLBACKS):          # only remember a miss if the lookups actually ran
        _write_cache(path, {"source": None})
    return None, None


# -- AI transcription (last resort) --------------------------------------------------

def _ai_path(song_id):
    return os.path.join(CACHE_DIR, f"{song_id}.ai.json")


def load_ai(song_id):
    """Lyrics from a finished transcription, or None if there is none (or it failed)."""
    try:
        with open(_ai_path(song_id), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("error"):
        return None
    lines = [Line(d["start"], d["end"], "".join(w[2] for w in d["words"]),
                  [Word(s, e, t) for s, e, t in d["words"]]) for d in data.get("lines") or []]
    return Lyrics(lines, word_level=True, source="AI听写")


def run_transcription(song_id, meta, on_stage, holder):
    """Run transcribe.py for one song (blocking) and return its Lyrics, or None.

    `holder` receives the Popen so the caller can kill it when the song changes.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = _ai_path(song_id)
    exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
    script_text = meta["title"] + " " + " ".join(meta["artists"])
    hint = "ja" if re.search("[\u3040-\u30ff]", script_text) else \
        "ko" if re.search("[\uac00-\ud7af]", script_text) else \
        "zh" if re.search("[\u4e00-\u9fff]", script_text) else ""
    proc = subprocess.Popen([exe if os.path.exists(exe) else sys.executable, AI_SCRIPT,
                             song_id, str(meta["duration"] or 0), out, hint],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    holder.append(proc)
    timer = threading.Timer(AI_TIMEOUT, proc.kill)
    timer.start()
    try:
        for line in proc.stdout:
            if line.startswith("STAGE "):
                on_stage(line[6:].strip())
        proc.wait()
    finally:
        timer.cancel()
        if proc in holder:
            holder.remove(proc)
    try:
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("error"):
        with open(os.path.join(DATA_DIR, "ai_errors.log"), "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {song_id} {data['error']}\n")
        os.remove(out)           # let the next play try again
        return None
    return load_ai(song_id)


def fetch_lyrics(song_id, meta, autofill=True):
    """Lyrics for a numeric song id. Raises on network failure with no cache."""
    api = netease_api(song_id)
    ly = build(api, meta["duration"])
    if ly.lines or ly.pure_music or not autofill:
        return ly
    alt, source = find_elsewhere(song_id, meta)
    return build(alt, meta["duration"], source) if alt else ly
