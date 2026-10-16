"""Auto-calibration: line the lyrics we already have up against the actual singing.

Uses Whisper's cross-attention alignment (the DTW it runs for its own word
timestamps), but fed the known lyric text instead of its own guess. Each line is
aligned in its own short window, cut at the real vocal onset near its timestamp;
afterwards every word is snapped onto stretches where the vocals are actually
sounding.

    python align.py <song_id> <duration_seconds> <lines.json | -> <out.json> [--scenes [--chinese]]

With --scenes the same separation also labels what's playing (scenes.py) for the
间奏 display; "-" skips the alignment.

lines.json: [{"start": s, "text": "...", "units": ["...", ...], "first": s?}, ...]: units
are the display words of the line (they join back into its text); "first" is the
source's own first-word time when it has real word timing.
out.json: {"lines": [{"start", "end", "prob", "words": [[s, e, unit], ...]} or null, ...]}
or {"error": "..."}. Progress goes to stdout as "STAGE <name>".
"""
import difflib
import json
import os
import re
import sys
import time

import numpy as np

import transcribe as tr

SR = 16000
HOP = 0.01                    # vocal-activity resolution, seconds
NO_SPACE_LANGS = {"zh", "ja", "yue", "th", "lo", "my"}
CREDIT = re.compile(r"^.{0,12}[:：]")      # "作词 : ..." style lines are never sung


def text_language(text):
    if re.search("[぀-ヿ]", text):
        return "ja"
    if re.search("[가-힯]", text):
        return "ko"
    if re.search("[一-鿿]", text):
        return "zh"
    return "en"


# -- where is the singer actually singing? --------------------------------------------

def _runs(mask):
    if len(mask) == 0:
        return []
    edges = np.flatnonzero(np.diff(mask.astype(np.int8))) + 1
    starts, ends = np.r_[0, edges], np.r_[edges, len(mask)]
    return [(int(s), int(e), bool(mask[s])) for s, e in zip(starts, ends)]


def _flip_short(mask, value, max_len):
    out = mask.copy()
    runs = _runs(mask)
    for i, (s, e, v) in enumerate(runs):
        if v == value and e - s < max_len and 0 < i < len(runs) - 1:
            out[s:e] = not value
    return out


def vocal_activity(vocals):
    """Boolean per 10 ms frame: is the separated vocal track audibly singing?"""
    hop = int(SR * HOP)
    n = len(vocals) // hop
    rms = np.sqrt((vocals[: n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(1))
    if not (rms > 0).any():
        return np.zeros(n, bool)
    active = rms > np.percentile(rms[rms > 0], 95) * 0.1
    active = _flip_short(active, False, 8)     # bridge dips under 80 ms (consonants, vibrato)
    return _flip_short(active, True, 5)        # drop blips under 50 ms (bleed, breaths)


def _first_active(active, lo, hi):
    a, b = int(lo / HOP), int(hi / HOP)
    idx = np.flatnonzero(active[a:b])
    return (a + idx[0]) * HOP if len(idx) else None


def _pause_near(active, lo, hi):
    """Middle of the longest pause inside [lo, hi], or None."""
    a, b = max(0, int(lo / HOP)), min(len(active), int(hi / HOP))
    best = None
    for s, e, v in _runs(active[a:b]):
        if not v and (best is None or e - s > best[1] - best[0]):
            best = (s, e)
    return None if best is None else (a + (best[0] + best[1]) / 2) * HOP


def snap(start, end, active):
    """Pull a word's edges onto audible vocals inside its own span."""
    a, b = int(start / HOP), max(int(end / HOP), int(start / HOP) + 1)
    idx = np.flatnonzero(active[a:b])
    if len(idx) == 0:
        return start, end
    return (a + idx[0]) * HOP, (a + idx[-1] + 1) * HOP


# -- alignment -------------------------------------------------------------------------

def _onset_near(active, t, before=0.4, after=0.6):
    """Start of the vocal run beginning closest to t (searching t-before .. t+after), or None."""
    a, b = max(1, int((t - before) / HOP)), min(len(active), int((t + after) / HOP))
    onsets = [f for f in range(a, b) if active[f] and not active[f - 1]]
    return min(onsets, key=lambda f: abs(f * HOP - t)) * HOP if onsets else None


def plan_windows(lines, active, total):
    """[(line index, lo, hi)]: one short window per sung line.

    Aligning several lines at once fails on songs: Whisper's alignment hands the
    pause between lines to the next line's first word. One line per window, cut
    at the real vocal onset near its timestamp and ending where the next line
    starts, leaves the pause nowhere to go.
    """
    todo = [i for i, ln in enumerate(lines) if ln["text"].strip() and not CREDIT.match(ln["text"])]
    windows = []
    for n, i in enumerate(todo):
        start = lines[i]["start"]
        onset = _onset_near(active, start)
        lines[i]["_onset"] = onset is not None
        lo = onset - 0.03 if onset is not None else start
        if n + 1 < len(todo):
            nxt = lines[todo[n + 1]]["start"]
            nxt_onset = _onset_near(active, nxt)
            hi = nxt_onset - 0.02 if nxt_onset is not None and nxt_onset > lo + 0.3 else nxt
        else:
            hi = start + 12.0
        hi = min(hi, lo + 29.0, total)
        a, b = int(lo / HOP), int(hi / HOP)
        sounding = np.flatnonzero(active[a:b])
        if len(sounding):
            hi = min(hi, (a + sounding[-1] + 1) * HOP + 0.1)      # drop trailing silence
        windows.append((i, max(0.0, lo), hi))
    return windows


def _char_times(full, words):
    """Per character of `full`: (start, end, prob) from Whisper's word list, or None."""
    joined = "".join(w["word"] for w in words)
    per_char = []
    for w in words:
        per_char.extend([(w["start"], w["end"], w["probability"])] * len(w["word"]))
    if joined == full:
        return per_char
    out = [None] * len(full)
    for a, b, size in difflib.SequenceMatcher(None, joined, full, autojunk=False).get_matching_blocks():
        for k in range(size):
            out[b + k] = per_char[a + k]
    return out


def align_chunk(model, tokenizer, vocals, lo, hi, texts, language):
    from faster_whisper.audio import pad_or_trim
    feats = model.feature_extractor(vocals[int(lo * SR): int(hi * SR)])
    num_frames = min(feats.shape[-1], 3000)
    encoded = model.encode(pad_or_trim(feats))
    spaced = language not in NO_SPACE_LANGS
    full, spans = "", []
    for t in texts:
        body = t.strip()
        # Whisper writes spaced languages with a leading space per word; elsewhere a
        # space between lines is enough to keep them apart.
        prefix = " " if (spaced or full) else ""
        spans.append(len(full) + len(prefix))
        full += prefix + body
    tokens = tokenizer.encode(full)
    words = model.find_alignment(tokenizer, [tokens], encoded, num_frames)[0]
    for w in words:
        w["start"] += lo
        w["end"] += lo
    return full, spans, _char_times(full, words)


def align_song(vocals, lines):
    from faster_whisper.tokenizer import Tokenizer
    model = tr.get_whisper()
    language = text_language("".join(ln["text"] for ln in lines))
    tokenizer = Tokenizer(model.hf_tokenizer, True, task="transcribe", language=language)
    active = vocal_activity(vocals)
    total = len(vocals) / SR
    out = [None] * len(lines)
    for line_index, lo, hi in plan_windows(lines, active, total):
        if hi - lo < 0.3:
            continue
        full, spans, chars = align_chunk(model, tokenizer, vocals, lo, hi, [lines[line_index]["text"]], language)
        for i, body_at in zip([line_index], spans):
            text = lines[i]["text"]
            lead = len(text) - len(text.lstrip())       # unit offsets count from the raw text
            words, pos, probs = [], 0, []
            for unit in lines[i]["units"]:
                seg = []
                for k, ch in enumerate(unit):
                    j = body_at + pos + k - lead
                    if not ch.isspace() and 0 <= j < len(chars) and chars[j]:
                        seg.append(chars[j])
                pos += len(unit)
                if seg:
                    s, e = snap(min(c[0] for c in seg), max(c[1] for c in seg), active)
                    probs.extend(c[2] for c in seg)
                else:
                    s = e = None
                words.append([s, e, unit])
            # fill units the text match missed by spacing them between known neighbours
            known = [k for k, w in enumerate(words) if w[0] is not None]
            if not known:
                continue
            for k, w in enumerate(words):
                if w[0] is None:
                    before = max((j for j in known if j < k), default=None)
                    after = min((j for j in known if j > k), default=None)
                    s0 = words[before][1] if before is not None else words[after][0]
                    s1 = words[after][0] if after is not None else words[before][1] + 0.2
                    gap = (k - (before if before is not None else k - 1)) / \
                          ((after if after is not None else k + 1) - (before if before is not None else k - 1))
                    w[0] = s0 + (s1 - s0) * gap
                    w[1] = w[0] + 0.1
            for k in range(1, len(words)):            # keep word starts in order
                words[k][0] = max(words[k][0], words[k - 1][0] + 0.02)
                words[k][1] = max(words[k][1], words[k][0] + 0.02)
            src_first = lines[i].get("first")         # the source's own word timing, if it has any
            src_silent = src_first is not None and not active[
                max(0, int((src_first - 0.05) / HOP)): int((src_first + 0.15) / HOP)].any()
            out[i] = {"start": round(words[0][0], 3), "end": round(words[-1][1], 3),
                      "prob": round(float(np.mean(probs)) if probs else 0.0, 3),
                      "onset": bool(lines[i].get("_onset")), "src_silent": bool(src_silent),
                      "words": [[round(s, 3), round(e, 3), u] for s, e, u in words]}
    return out


def main():
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    song_id, duration, lines_path, out_path = args[0], float(args[1]), args[2], args[3]
    lines = None
    if lines_path != "-":
        with open(lines_path, encoding="utf-8") as f:
            lines = json.load(f)
    t0 = time.time()
    result = {}
    try:
        tr.stage("audio")
        audio, rate = tr.wait_for_audio(song_id, duration)
        if audio is None:
            result["error"] = "no_cache"
        else:
            tr.stage("vocals")
            stems, sr = tr.separate_stems(audio, rate)
            if lines:
                tr.stage("align")
                result["lines"] = align_song(tr.to_16k(stems["vocals"], sr), lines)
            if "--scenes" in flags:
                tr.stage("scenes")
                import scenes
                result["scenes"] = scenes.label_song(stems, sr, tr.MODELS_DIR, "--chinese" in flags)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    result["elapsed"] = round(time.time() - t0, 1)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, out_path)
    tr.stage("done")


if __name__ == "__main__":
    main()
