"""Fetch, cache and parse lyrics.

NetEase first (LRC line timing + YRC word timing). Only when the listener presses
找词 are other versions of the song on NetEase, LRCLIB and QQ Music searched,
matched by title, kind of take, artist and length, and checked against what the
song actually sings before they are filled in. Lines without real word timing
get estimated word timing so they can be revealed word by word.
"""
import bisect
import html
import json
import os
import re
import threading
import time
from collections import Counter
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
AI_TIMEOUT = 600             # seconds to wait for a transcription (first run loads the models)
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
KANA = re.compile("[\u3040-\u30ff]")
KANJI = re.compile("[\u4e00-\u9fff]")
SMALL_KANA = set("ゃゅょぁぃぅぇぉゎャュョァィゥェォヮ")


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


def _syllables(unit, japanese=False):
    if japanese and IS_CJK.search(unit):
        # Japanese is sung by mora: a kanji is usually read as about two kana,
        # small kana ride on the one before, っ/ー get a short beat.
        w = 0.0
        for ch in unit:
            if KANJI.match(ch):
                w += 1.8
            elif ch in SMALL_KANA:
                w += 0.3
            elif ch in "ーっッ":
                w += 0.6
            elif KANA.match(ch) or IS_CJK.match(ch):
                w += 1.0
        return w
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
    japanese = bool(KANA.search(line.text))
    weights = [_syllables(u, japanese) for u in units]
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


# -- finding lyrics elsewhere (only when the listener presses 找词) ----------------------

FIND_DEADLINE = 20.0          # seconds all library lookups together may take
PER_SOURCE = 3                # candidates kept from each library
STRICT_SLACK = 1.5            # without a listen, only near-identical lengths are trusted
# Words in a title's brackets / " - " suffix that mark a different take of a song.
QUALIFIERS = [
    ("inst", r"伴奏|纯音乐|(?<![a-z])inst(rumental)?(?![a-z])|off ?vocal|karaoke|卡拉ok"),
    ("live", r"(?<![a-z])live(?![a-z])|现场|演唱会"),
    ("remix", r"remix|(?<![a-z])(mix|dj|bootleg)(?![a-z])"),
    ("acoustic", r"acoustic|unplugged|不插电"),
    ("cover", r"cover|翻唱|翻自"),
    ("demo", r"(?<![a-z])demo(?![a-z])"),
    ("short", r"(?<![a-z])(tv|size|short|cut|edit)(?![a-z])"),
    ("long", r"extended|加长"),
    ("piano", r"piano|钢琴"),
    ("orch", r"orchestra|交响|管弦"),
    ("chorus", r"合唱|choir"),
    ("ver", r"(?<![a-z])ver(sion)?(?![a-z])|版"),
]
NOT_A_VERSION = re.compile(r"album|original|single|remaster|原版|正式")
FEAT = re.compile(r"\s(feat|ft)\.?\s.*$", re.I)
CREDIT = re.compile(r"^.{0,12}[:：]")       # "作词 : ..." style lines are never sung
PLACEHOLDER = re.compile(r"纯音乐|请欣赏|请您欣赏|instrumental|no lyrics", re.I)


def _split_title(title):
    """(normalised main title, lower-cased bracket / suffix text)."""
    title = title or ""
    extras = " ".join(re.findall(r"[(（\[【](.*?)[)）\]】]", title))
    main = re.sub(r"[(（\[【].*?[)）\]】]", " ", title)
    parts = re.split(r"\s+[-–—~～]\s+", main, maxsplit=1)
    if len(parts) == 2:
        main, extras = parts[0], extras + " " + parts[1]
    main = re.sub(r"[\W_]+", "", FEAT.sub("", main)).lower()
    return main or re.sub(r"[\W_]+", "", title).lower(), extras.lower()


def _qualifiers(extras):
    found = {name for name, pattern in QUALIFIERS if re.search(pattern, extras)}
    if "ver" in found and NOT_A_VERSION.search(extras):
        found.discard("ver")
    return found


def _artists(names):
    return {re.sub(r"[\W_]+", "", a).lower() for a in names} - {""}


def _same_song(meta, title, artists, duration):
    """Same title (and the same kind of take: live, remix, instrumental...), a shared artist,
    and a length within DURATION_SLACK."""
    want, want_extra = _split_title(meta["title"])
    got, got_extra = _split_title(title)
    if not want or want != got or _qualifiers(want_extra) != _qualifiers(got_extra):
        return False
    mine, theirs = _artists(meta["artists"]), _artists(artists)
    if mine and theirs and not (mine & theirs or any(a in b or b in a for a in mine for b in theirs)):
        return False
    if not duration or (meta["duration"] and abs(meta["duration"] - duration) > DURATION_SLACK):
        return False
    return True


def _left(end, cap=8.0):
    """Request timeout that respects the overall deadline."""
    return max(1.0, min(cap, end - time.time()))


def _netease_versions(meta, song_id, end):
    params = {"s": f"{meta['title']} {' '.join(meta['artists'][:2])}", "type": 1, "limit": 20}
    songs = requests.get(SEARCH_API, params=params, headers=HEADERS, timeout=_left(end)).json() \
        .get("result", {}).get("songs") or []
    out = []
    for s in songs:
        if len(out) >= PER_SOURCE or time.time() > end:
            break
        duration = (s.get("duration") or 0) / 1000.0
        if str(s["id"]) == song_id or not _same_song(
                meta, s.get("name"), [a.get("name", "") for a in s.get("artists") or []], duration):
            continue
        api = netease_api(str(s["id"]))
        if not api.get("pureMusic"):
            out.append((api, duration))
    return out


def _lrclib(meta, song_id, end):
    found = []
    params = {"track_name": meta["title"], "artist_name": ", ".join(meta["artists"])}
    if meta["album"]:
        params["album_name"] = meta["album"]
    if meta["duration"]:
        params["duration"] = round(meta["duration"])
    r = requests.get(LRCLIB_GET, params=params, headers=LRCLIB_HEADERS, timeout=_left(end))
    if r.status_code == 200:
        found.append(r.json())
    if time.time() < end:
        r = requests.get(LRCLIB_SEARCH, params={"track_name": meta["title"],
                                                "artist_name": " ".join(meta["artists"][:2])},
                         headers=LRCLIB_HEADERS, timeout=_left(end))
        if r.status_code == 200:
            found.extend(r.json() or [])
    out, seen = [], set()
    for c in found:
        if len(out) >= PER_SOURCE:
            break
        if c.get("id") in seen or c.get("instrumental") or not c.get("syncedLyrics"):
            continue
        seen.add(c.get("id"))
        if _same_song(meta, c.get("trackName"), [c.get("artistName", "")], c.get("duration") or 0):
            out.append(({"lrc": {"lyric": c["syncedLyrics"]}}, c.get("duration") or 0))
    return out


def _qq(meta, song_id, end):
    params = {"w": f"{meta['title']} {' '.join(meta['artists'][:2])}", "format": "json", "n": 10, "p": 1, "cr": 1}
    songs = requests.get(QQ_SEARCH, params=params, headers=QQ_HEADERS, timeout=_left(end)).json() \
        .get("data", {}).get("song", {}).get("list") or []
    out = []
    for s in songs:
        if len(out) >= PER_SOURCE or time.time() > end:
            break
        duration = s.get("interval") or 0
        if not _same_song(meta, s.get("songname"), [a.get("name", "") for a in s.get("singer") or []], duration):
            continue
        j = requests.get(QQ_LYRIC, params={"songmid": s["songmid"], "format": "json", "nobase64": 1, "g_tk": 5381},
                         headers=QQ_HEADERS, timeout=_left(end)).json()
        lrc = html.unescape(j.get("lyric") or "")
        if parse_lrc(lrc):
            out.append(({"lrc": {"lyric": lrc}, "tlyric": {"lyric": html.unescape(j.get("trans") or "")}}, duration))
    return out


SOURCES = [("网易云其他版本", _netease_versions), ("LRCLIB", _lrclib), ("QQ音乐", _qq)]


def _real_lyrics(api):
    """Synced lines that are actual lyrics, not a one-line "instrumental, please enjoy" placeholder
    (QQ Music answers instrumentals that way)."""
    texts = [ln.text for ln in build(api).lines if ln.text.strip()]
    if len(texts) < 3:
        return False
    return not (len(texts) <= 5 and any(PLACEHOLDER.search(t) for t in texts))


def plausible_timing(ly):
    """False when lines come faster than anyone sings: sloppy uploads cram several lines into a
    second or two (10+ sung units a second on a fifth of the lines; no normal lyric in the cache
    comes close)."""
    sung = [ln for ln in ly.lines if ln.text.strip() and not CREDIT.match(ln.text)]
    rushed = total = 0
    for a, b in zip(sung, sung[1:]):
        units = len(_atoms(a.text))
        if b.start - a.start < 0.05 or units < 3:      # stacked lines (a translation) / short calls
            continue
        total += 1
        rushed += b.start - a.start < units * 0.1
    return rushed < 0.2 * max(total, 1)


def _text_key(api):
    return re.sub(r"[\W_]+", "", "".join(ln.text for ln in build(api).lines)).lower()[:300]


def find_candidates(song_id, meta, deadline=FIND_DEADLINE):
    """Lyrics for this song from other libraries, best source first, all within `deadline` seconds:
    [{"source", "api", "strict"}], where strict means the length matches within STRICT_SLACK."""
    if not meta["title"]:
        return []
    end = time.time() + deadline
    results, lock = {}, threading.Lock()

    def run(name, fn):
        try:
            got = fn(meta, song_id, end)
        except Exception:
            got = []
        with lock:
            results[name] = got

    threads = [threading.Thread(target=run, args=src, daemon=True) for src in SOURCES]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, end - time.time()))
    out, seen = [], set()
    with lock:
        for name, _ in SOURCES:
            for api, duration in results.get(name) or []:
                if not _real_lyrics(api) or not plausible_timing(build(api)):
                    continue
                key = _text_key(api)
                if key in seen:
                    continue
                seen.add(key)
                strict = bool(meta["duration"] and abs(meta["duration"] - duration) <= STRICT_SLACK)
                out.append({"source": name, "api": api, "strict": strict})
    return out


# -- checking found lyrics against what the song actually sings ------------------------

LISTEN_TOL = 1.0      # seconds a heard word may sit outside its line's span
MAX_SPAN = 8.0        # longest stretch one line is taken to be sung over
MAX_SHIFT = 12.0      # another cut of the song may start this much earlier or later
LINE_HIT = 0.5        # share of a line's words that must be heard for the line to count
ACCEPT = 0.5          # share of lines that must be heard in place
MAX_ONSET_ERR = 1.0   # seconds a heard line may start, on average, away from its time stamp
MIN_CHECKED = 4       # lines needed before a score means anything


def _atoms(text):
    return ATOM.findall((text or "").lower())


def _heard(words):
    """Sorted (times, tokens) of transcribed words, one entry per atom."""
    pairs = []
    for s, e, text in words:
        atoms = _atoms(text)
        for k, a in enumerate(atoms):
            pairs.append((s + (e - s) * k / len(atoms), a))
    pairs.sort()
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _voice_grid(voiced):
    """Quarter-second flags from [[start, end], ...] singing spans."""
    if not voiced:
        return None
    grid = bytearray(int(voiced[-1][1] * 4) + 2)
    for s, e in voiced:
        grid[int(s * 4): int(e * 4) + 1] = b"" * (int(e * 4) + 1 - int(s * 4))
    return grid


def listen_score(ly, words, voiced=None):
    """How well `ly` fits what was heard (`words` = [[start, end, text], ...] from transcription,
    `voiced` = [[start, end], ...] where someone sings).

    A line counts when half its words are heard within its own span (plus LISTEN_TOL). A line
    with nothing heard is skipped if someone sings there (transcription misses whole stretches)
    and fails if nobody does. The whole lyric may sit up to MAX_SHIFT off. Returns (share of
    checked lines heard in place, best shift, mean distance between a heard line's first word
    and its time stamp, lines checked).
    """
    times, tokens = _heard(words)
    grid = _voice_grid(voiced)
    sung = [ln for ln in ly.lines if ln.text.strip() and not CREDIT.match(ln.text)]
    spans = []
    for k, ln in enumerate(sung):
        atoms = Counter(_atoms(ln.text))
        if sum(atoms.values()) < 2:
            continue
        nxt = sung[k + 1].start if k + 1 < len(sung) else ln.start + 6.0
        spans.append((ln.start, ln.start + min(max(nxt - ln.start, 0.3), MAX_SPAN), atoms))
    if len(spans) < MIN_CHECKED or not times:
        return 0.0, 0.0, 99.0, 0

    def sung_there(lo, hi):
        if grid is None:
            return False
        part = grid[max(0, int(lo * 4)): max(0, int(hi * 4))]
        return bool(part) and sum(part) >= 0.5 * len(part)

    def score(shift):
        hits, checked, err = 0, 0, 0.0
        for lo, hi, atoms in spans:
            a = bisect.bisect_left(times, lo + shift - LISTEN_TOL)
            b = bisect.bisect_right(times, hi + shift + LISTEN_TOL)
            if a == b and sung_there(lo + shift, hi + shift):
                continue                                  # sung, but the transcription missed it
            checked += 1
            got = Counter(tokens[a:b])
            if sum(min(n, got[t]) for t, n in atoms.items()) >= LINE_HIT * sum(atoms.values()):
                hits += 1
                first = next(times[i] for i in range(a, b) if tokens[i] in atoms)
                err += abs(first - (lo + shift))
        enough = checked >= max(MIN_CHECKED, 0.4 * len(spans))
        return (hits / checked if enough else 0.0), shift, (err / hits if hits else 99.0), checked

    steps = int(MAX_SHIFT * 4)
    tried = [score(k / 4) for k in range(-steps, steps + 1)]
    base, top = tried[steps], max(r[0] for r in tried)
    # Repeated choruses make several shifts fit about as well; the right one is the tightest.
    best = min((r for r in tried if r[0] >= 0.85 * top), key=lambda r: r[2])
    if best[0] - base[0] < 0.1 and not (base[2] - best[2] >= 0.4 and best[0] >= base[0] - 0.05):
        best = base                                       # only move lyrics that clearly sit off
    return best


def listen_ok(score, err, checked):
    return checked >= MIN_CHECKED and score >= ACCEPT and err <= MAX_ONSET_ERR


def shifted(ly, shift):
    """`ly` moved `shift` seconds later (earlier if negative)."""
    if not shift:
        return ly
    lines = [Line(max(0.0, ln.start + shift), ln.end + shift, ln.text,
                  [Word(w.start + shift, w.end + shift, w.text) for w in ln.words] if ln.words else None,
                  ln.trans, ln.estimated) for ln in ly.lines]
    return Lyrics(lines, pure_music=ly.pure_music, word_level=ly.word_level, source=ly.source)


# -- lyrics filled in by 找词 ------------------------------------------------------------

def _fill_path(song_id):
    return os.path.join(CACHE_DIR, f"{song_id}.fill.json")


def save_fill(song_id, data):
    """{"source", "api", "shift", ...} from a library, or {"source": "AI听写", "lines": [...]}."""
    _write_cache(_fill_path(song_id), data)


def has_fill(song_id):
    return os.path.exists(_fill_path(song_id))


def forget_fill(song_id):
    if has_fill(song_id):
        os.remove(_fill_path(song_id))


def load_fill(song_id, duration=0.0):
    """Lyrics filled in earlier with 找词, or None."""
    try:
        with open(_fill_path(song_id), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("lines"):
        lines = [Line(d["start"], d["end"], "".join(w[2] for w in d["words"]),
                      [Word(s, e, t) for s, e, t in d["words"]]) for d in data["lines"]]
        return Lyrics(lines, word_level=True, source="AI听写")
    if data.get("api"):
        ly = build(data["api"], duration, data.get("source") or "别的歌词库")
        ly.pure_music = False
        return shifted(ly, data.get("shift") or 0.0)
    return None


def log_find(song_id, text):
    try:
        with open(os.path.join(DATA_DIR, "find.log"), "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {song_id} {text}\n")
    except OSError:
        pass


# -- AI helpers ------------------------------------------------------------------------

def log_error(song_id, text):
    try:
        with open(os.path.join(DATA_DIR, "ai_errors.log"), "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {song_id} {text}\n")
    except OSError:
        pass


def language_hint(meta):
    """Script of title/artists: kana -> ja, hangul -> ko, han -> zh, else ''."""
    text = meta["title"] + " " + " ".join(meta["artists"])
    if re.search("[\u3040-\u30ff]", text):
        return "ja"
    if re.search("[\uac00-\ud7af]", text):
        return "ko"
    if re.search("[\u4e00-\u9fff]", text):
        return "zh"
    return ""


def _align_path(song_id):
    return os.path.join(CACHE_DIR, f"{song_id}.align.json")


def _signature(ly):
    import hashlib
    raw = "\n".join(f"{ln.start:.2f} {ln.text}" for ln in ly.lines)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def can_calibrate(ly):
    return bool(ly and ly.lines and not ly.pure_music and ly.source != "AI听写"
                and any(ln.words for ln in ly.lines))


def load_alignment(song_id, ly):
    """Cached alignment for exactly these lyrics, or None."""
    try:
        with open(_align_path(song_id), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("sig") != _signature(ly) or not data.get("lines"):
        return None
    return data


def alignment_request(ly):
    """The lines as the worker's align job wants them."""
    lines = []
    for ln in ly.lines:
        units = [w.text for w in ln.words] if ln.words else tokenize(ln.text)
        item = {"start": ln.start, "text": ln.text, "units": units}
        if ln.words and not ln.estimated:
            item["first"] = ln.words[0].start
        lines.append(item)
    return lines


def save_alignment(song_id, ly, data):
    _write_cache(_align_path(song_id), {"lines": data["lines"], "sig": _signature(ly)})


def apply_alignment(ly, data):
    """New Lyrics with audio-based timing.

    Line-only lyrics take the aligned word times outright. Lyrics that already have
    real word timing keep their own spacing; a line is only shifted as a whole when
    the source puts its first word in silence and the audio shows a clear onset.
    """
    lines, fixed = [], 0
    for ln, r in zip(ly.lines, data["lines"]):
        ln = Line(ln.start, ln.end, ln.text, list(ln.words) if ln.words else None, ln.trans, ln.estimated)
        if r and ln.words:
            if ln.estimated and len(r["words"]) == len(ln.words):
                ln.words = [Word(s, e, u) for s, e, u in r["words"]]
                ln.estimated = False
                ln.start = min(ln.start, r["start"] - 0.05)
                fixed += 1
            elif not ln.estimated and r.get("onset") and r.get("src_silent"):
                delta = r["start"] - ln.words[0].start
                if abs(delta) > 0.3:
                    ln.words = [Word(w.start + delta, w.end + delta, w.text) for w in ln.words]
                    ln.start, ln.end = ln.start + delta, ln.end + delta
                    fixed += 1
        lines.append(ln)
    for a, b in zip(lines, lines[1:]):               # keep lines in order after moving starts
        if b.start < a.start + 0.05:
            b.start = a.start + 0.05
        if not ly.word_level and a.end > b.start:
            a.end = b.start
    out = Lyrics(lines, pure_music=ly.pure_music, word_level=ly.word_level, source=ly.source)
    out.calibrated, out.raw = fixed, ly        # keep the source timing for a manual re-run
    return out


def netease_lyrics(song_id, meta):
    """NetEase's own lyrics. Raises on network failure with no cache."""
    return build(netease_api(song_id), meta["duration"])


def needs_more(ly):
    """No usable lyrics yet (songs NetEase marks as instrumental included: obscure songs
    get that mark just because nobody uploaded lyrics, so 找词 may still find some)."""
    return not ly or not ly.lines or ly.pure_music
