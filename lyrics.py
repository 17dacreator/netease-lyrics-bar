"""Fetch, cache and parse NetEase lyrics (LRC line timing + YRC word timing)."""
import bisect
import json
import os
import re
import time
from dataclasses import dataclass, field

import requests

from config import DATA_DIR

CACHE_DIR = os.path.join(DATA_DIR, "cache")
PLAYING_LIST = os.path.expandvars(r"%LOCALAPPDATA%\NetEase\CloudMusic\webdata\file\playingList")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://music.163.com/"}
LYRIC_API = "https://music.163.com/api/song/lyric/v1?id={}&lv=-1&tv=-1&rv=-1&yv=-1&ytv=-1"
DETAIL_API = "https://music.163.com/api/song/detail/?ids=[{}]"
MISS_TTL = 3 * 24 * 3600     # re-check songs without lyrics after a few days

TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
OFFSET_TAG = re.compile(r"\[offset:\s*([+-]?\d+)\]", re.I)
YRC_LINE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
YRC_WORD = re.compile(r"\((\d+),(\d+),-?\d+\)")


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


@dataclass
class Lyrics:
    lines: list = field(default_factory=list)
    pure_music: bool = False
    word_level: bool = False

    def __post_init__(self):
        self._starts = [ln.start for ln in self.lines]

    def index_at(self, t):
        return bisect.bisect_right(self._starts, t) - 1


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


def build(api, duration=0.0):
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
    return Lyrics(lines, pure_music=bool(api.get("pureMusic")), word_level=bool(yrc))


def _cache_path(song_id):
    return os.path.join(CACHE_DIR, f"{song_id}.json")


def fetch_lyrics(song_id, duration=0.0):
    """Lyrics for a numeric song id. Raises on network failure with no cache."""
    path = _cache_path(song_id)
    api = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            has_lines = any((cached.get(k) or {}).get("lyric") for k in ("lrc", "yrc"))
            if has_lines or time.time() - os.path.getmtime(path) < MISS_TTL:
                api = cached
        except (OSError, ValueError):
            api = None
    if api is None:
        resp = requests.get(LYRIC_API.format(song_id), headers=HEADERS, timeout=8)
        resp.raise_for_status()
        api = resp.json()
        if api.get("code") != 200:
            raise RuntimeError(f"lyric api code {api.get('code')}")
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(api, f, ensure_ascii=False)
        os.replace(tmp, path)
    return build(api, duration)


def song_title(song_id):
    """'name - artists' from the client's local queue, falling back to the web API."""
    try:
        with open(PLAYING_LIST, encoding="utf-8") as f:
            items = json.load(f).get("list", [])
        for item in items:
            track = item.get("track") or {}
            if str(track.get("id")) == song_id or str(item.get("id")) == song_id:
                artists = " / ".join(a.get("name", "") for a in track.get("artists") or [])
                return f"{track.get('name', '')} - {artists}".strip(" -")
    except (OSError, ValueError):
        pass
    try:
        song = requests.get(DETAIL_API.format(song_id), headers=HEADERS, timeout=6).json()["songs"][0]
        artists = " / ".join(a.get("name", "") for a in song.get("artists") or [])
        return f"{song.get('name', '')} - {artists}".strip(" -")
    except Exception:
        return ""
