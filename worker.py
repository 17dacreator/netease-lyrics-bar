"""Long-lived GPU helper for the lyric bar.

Keeps Demucs and Whisper loaded, so calibration and AI transcription start within
a second or two instead of reloading ~4 GB of models for every song. Calibration
starts where the listener is: the song is separated 30 s at a time from just
before the current position, and each stretch's lines are reported as soon as
they are aligned.

One JSON object per line on stdin / stdout:
  in : {"id": n, "job": "warm"}
       {"id": n, "job": "align", "song": sid, "duration": d, "lines": [...], "focus": seconds}
       {"id": n, "job": "voice", "song": sid, "duration": d}      (done: {"voice": {...}})
       {"id": n, "job": "transcribe", "song": sid, "duration": d, "hint": "ja", "check_vocals": bool}
           (done: {"lines": [...], "singing": s, "voiced": [[start, end], ...]} or {"instrumental": true})
       {"cancel": n}
  out: {"ready": true}
       {"id": n, "stage": "..."}
       {"id": n, "partial": {"<line index>": result, ...}}     (align, as stretches finish)
       {"id": n, "done": {...}}  or  {"id": n, "error": "..."}
It exits after IDLE seconds without work so games get the VRAM back.
"""
import json
import os
import queue
import sys
import threading

import numpy as np

import align
import transcribe as tr

IDLE = 300.0
CHUNK = 30.0          # seconds separated per step during calibration
EDGE = 2.0            # extra context either side of a stretch for Demucs
MIN_SINGING = 4.0     # below this much clear voice a song marked instrumental is left alone
MIN_LINES, MIN_WORDS = 3, 8   # ...above it, Whisper has to hear at least this much to count as lyrics
# Where someone sings, for the 前奏/间奏/尾奏 labels: per 0.1 s the vocals carry at least VOICE_SHARE of
# the song's energy. On the word-timed songs in the cache this caught 74% of sung frames; runs under
# VOICE_MIN s are dropped (instrument bleed) and gaps up to VOICE_BRIDGE s closed.
VOICE_SHARE, VOICE_MIN, VOICE_BRIDGE = 0.12, 0.8, 1.0
VOICE_VERSION = 1

_out_lock = threading.Lock()
_demucs = None
_audio = {"song": None}


def send(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def demucs():
    global _demucs
    if _demucs is None:
        os.environ.setdefault("TORCH_HOME", os.path.join(tr.MODELS_DIR, "torch"))
        from demucs.pretrained import get_model
        _demucs = get_model("htdemucs").eval()
    return _demucs


def song_audio(song, duration):
    if _audio["song"] != song:
        audio, rate = tr.wait_for_audio(song, duration)
        if audio is None:
            return None, None
        _audio.update(song=song, audio=audio, rate=rate)
    return _audio["audio"], _audio["rate"]


def separate(audio, rate, lo, hi):
    """Mono stems for [lo, hi) seconds; Demucs sees EDGE s of context either side."""
    import julius
    import torch
    from demucs.apply import apply_model
    model = demucs()
    total = audio.shape[1] / rate
    a0, a1 = max(0.0, lo - EDGE), min(total, hi + EDGE)
    wav = torch.from_numpy(audio[:, int(a0 * rate): int(a1 * rate)]).float()
    if rate != model.samplerate:
        wav = julius.resample_frac(wav, rate, model.samplerate)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        src = apply_model(model, ((wav - mean) / std)[None], device="cuda", split=True,
                          overlap=0.25, progress=False)[0] * std + mean
    sr = model.samplerate
    k0 = int(round((lo - a0) * sr))
    k1 = k0 + int(round((hi - lo) * sr))
    return {name: src[k].mean(0)[k0:k1].cpu().numpy().astype("float32")
            for k, name in enumerate(model.sources)}, sr


def chunk_order(total, focus):
    """Stretches to separate: from just before the listener forward, then what came before."""
    start = max(0.0, min(focus - 3.0, total - CHUNK))
    forward, lo = [], start
    while lo < total - 0.05:
        forward.append((lo, min(total, lo + CHUNK)))
        lo += CHUNK
    backward, hi = [], start
    while hi > 0.05:
        backward.append((max(0.0, hi - CHUNK), hi))
        hi -= CHUNK
    return forward + backward


def job_warm(job, cancelled):
    demucs()
    tr.get_whisper()
    send({"id": job["id"], "done": {}})


class VoiceMeter:
    """Collects per-0.1 s energies of the vocals and the whole mix from separated stretches."""

    def __init__(self, total):
        self.n = int(np.ceil(total * 10)) + 1
        self.sums = {"vocals": np.zeros(self.n), "all": np.zeros(self.n)}
        self.count = np.zeros(self.n)

    def add(self, stems, sr, lo):
        """Stems of the stretch starting at `lo` seconds."""
        k = ((lo + np.arange(len(stems["vocals"])) / sr) * 10).astype(np.int64)
        k = np.minimum(k, self.n - 1)
        power = {name: x.astype(np.float64) ** 2 for name, x in stems.items()}
        self.sums["vocals"] += np.bincount(k, weights=power["vocals"], minlength=self.n)
        self.sums["all"] += np.bincount(k, weights=sum(power.values()), minlength=self.n)
        self.count += np.bincount(k, minlength=self.n)

    def spans(self):
        """{"v": version, "spans": [[start, end], ...]} seconds where someone sings."""
        c = np.maximum(self.count, 1)
        ev, et = self.sums["vocals"] / c, self.sums["all"] / c
        audible = et > np.percentile(et, 95) * 0.01
        on = (ev / (et + 1e-12) >= VOICE_SHARE) & audible
        runs, i = [], 0
        while i < len(on):
            if on[i]:
                j = i
                while j < len(on) and on[j]:
                    j += 1
                if runs and i - runs[-1][1] <= VOICE_BRIDGE * 10:
                    runs[-1][1] = j
                else:
                    runs.append([i, j])
                i = j
            else:
                i += 1
        return {"v": VOICE_VERSION,
                "spans": [[a / 10, b / 10] for a, b in runs if b - a >= VOICE_MIN * 10]}


def job_voice(job, cancelled):
    jid = job["id"]
    audio, rate = song_audio(job["song"], job.get("duration") or 0)
    if audio is None:
        send({"id": jid, "error": "no_cache"})
        return
    total = audio.shape[1] / rate
    meter = VoiceMeter(total)
    stems, sr = separate(audio, rate, 0.0, total)
    meter.add(stems, sr, 0.0)
    send({"id": jid, "done": {"voice": meter.spans()}})


def job_align(job, cancelled):
    jid = job["id"]
    send({"id": jid, "stage": "audio"})
    audio, rate = song_audio(job["song"], job.get("duration") or 0)
    if audio is None:
        send({"id": jid, "error": "no_cache"})
        return
    lines = job["lines"]
    total = audio.shape[1] / rate
    vocals = np.zeros(int(total * align.SR) + 1, np.float32)
    covered = np.zeros(int(total / align.HOP) + 2, bool)
    aligner = align.make_aligner(lines)
    sung = [i for i, ln in enumerate(lines) if ln["text"].strip() and not align.CREDIT.match(ln["text"])]
    need = {}                  # line -> the stretch of audio its window may use
    for n, i in enumerate(sung):
        nxt = lines[sung[n + 1]]["start"] if n + 1 < len(sung) else lines[i]["start"] + 12.0
        need[i] = (max(0.0, lines[i]["start"] - 1.5), min(total, nxt + 0.7))
    results = [None] * len(lines)
    done = set()
    meter = VoiceMeter(total)              # where someone sings, for the gap labels, on the way
    send({"id": jid, "stage": "align"})
    for lo, hi in chunk_order(total, job.get("focus") or 0.0):
        if cancelled(jid):
            return
        stems, sr = separate(audio, rate, lo, hi)
        meter.add(stems, sr, lo)
        v16 = tr.to_16k(stems["vocals"], sr)
        a = int(round(lo * align.SR))
        vocals[a: a + len(v16)] = v16[: max(0, len(vocals) - a)]
        covered[int(lo / align.HOP): int(np.ceil(hi / align.HOP))] = True
        ready = {i for i in sung if i not in done and
                 covered[int(need[i][0] / align.HOP): int(need[i][1] / align.HOP)].all()}
        if not ready:
            continue
        active = align.vocal_activity(vocals, covered)
        part = {}
        for i, wlo, whi in align.plan_windows(lines, active, total, only=ready):
            if cancelled(jid):
                return
            results[i] = align.align_line(aligner, vocals, active, lines, i, wlo, whi)
            if results[i]:
                part[str(i)] = results[i]
        done |= ready
        if part:
            send({"id": jid, "partial": part})
    send({"id": jid, "done": {"lines": results, "voice": meter.spans()}})


def _voice_frames(stems, sr):
    """Per 0.1 s: is a clearly audible voice the main thing in the mix?"""
    hop = int(sr * 0.1)
    n = len(stems["vocals"]) // hop
    energy = {k: (x[: n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(1) for k, x in stems.items()}
    total = sum(energy.values()) + 1e-12
    audible = total > np.percentile(total, 95) * 0.01
    return (energy["vocals"] / total >= 0.3) & audible


def singing_seconds(stems, sr):
    """How long a clearly audible voice is the main thing in the mix."""
    return float(_voice_frames(stems, sr).sum() * 0.1)


def voice_spans(stems, sr, bridge=0.5):
    """[[start, end], ...] seconds where someone sings, gaps under `bridge` s closed."""
    spans = []
    for k in np.flatnonzero(_voice_frames(stems, sr)):
        t = k * 0.1
        if spans and t - spans[-1][1] <= bridge:
            spans[-1][1] = round(t + 0.1, 1)
        else:
            spans.append([round(t, 1), round(t + 0.1, 1)])
    return spans


def job_transcribe(job, cancelled):
    jid = job["id"]
    send({"id": jid, "stage": "audio"})
    audio, rate = song_audio(job["song"], job.get("duration") or 0)
    if audio is None:
        send({"id": jid, "error": "no_cache"})
        return
    send({"id": jid, "stage": "vocals"})
    stems, sr = separate(audio, rate, 0.0, audio.shape[1] / rate)
    if cancelled(jid):
        return
    sing, voiced = singing_seconds(stems, sr), voice_spans(stems, sr)
    if job.get("check_vocals") and sing < MIN_SINGING:
        send({"id": jid, "done": {"instrumental": True, "singing": round(sing, 1)}})
        return
    send({"id": jid, "stage": "words"})
    words, language = tr.transcribe(tr.to_16k(stems["vocals"], sr), job.get("hint", ""))
    lines = tr.to_lines(words)
    if job.get("check_vocals") and (len(lines) < MIN_LINES or sum(len(ln["words"]) for ln in lines) < MIN_WORDS):
        # Vocal samples in an instrumental, not lyrics (energy alone can't tell them apart).
        send({"id": jid, "done": {"instrumental": True, "singing": round(sing, 1), "lines_heard": len(lines)}})
        return
    send({"id": jid, "done": {"source": "AI听写", "model": tr.WHISPER_MODEL, "language": language,
                              "lines": lines, "singing": round(sing, 1), "voiced": voiced}})


JOBS = {"warm": job_warm, "align": job_align, "voice": job_voice, "transcribe": job_transcribe}


def main():
    jobs, cancelled = queue.Queue(), set()

    def read_stdin():
        for raw in sys.stdin:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "cancel" in msg:
                cancelled.add(msg["cancel"])
            else:
                jobs.put(msg)
        jobs.put(None)                     # the lyric bar went away

    threading.Thread(target=read_stdin, daemon=True).start()
    send({"ready": True})
    while True:
        try:
            job = jobs.get(timeout=IDLE)
        except queue.Empty:
            break
        if job is None:
            break
        if job.get("id") in cancelled:
            continue
        try:
            JOBS[job["job"]](job, lambda j: j in cancelled)
        except Exception as e:             # report and keep serving
            send({"id": job.get("id"), "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
