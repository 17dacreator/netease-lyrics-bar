"""Last-resort lyrics: transcribe a song from NetEase's local audio cache.

Runs as its own process (spawned by the overlay) so torch and the models never
live inside the lyric window:

    python transcribe.py <song_id> <duration_seconds> <out.json> [language_hint]

Pipeline: cached .uc file (audio XOR 0xA3) -> decode -> Demucs vocal stem ->
faster-whisper with word timestamps -> lines. Progress goes to stdout as
"STAGE <name>" lines; the result (or {"error": ...}) is written to out.json.
"""
import glob
import io
import json
import os
import re
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(APP_DIR, "data", "models")
WHISPER_MODEL = "large-v3"
DEFAULT_CACHE = os.path.expandvars(r"%LOCALAPPDATA%\NetEase\CloudMusic\Cache\Cache")
XOR_TABLE = bytes(b ^ 0xA3 for b in range(256))
_whisper = None

# Whisper's well-known filler for silence/music, mostly from subtitled videos.
HALLUCINATIONS = re.compile(
    r"字幕|谢谢观看|謝謝觀看|请不吝|點贊|点赞|订阅|訂閱|打赏|明镜|優優|YoYo|Amara|"
    r"thank(s| you) for watching|subscribe|subtitles? by", re.I)
CJK = re.compile("[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")


def stage(name):
    print("STAGE", name, flush=True)


def cache_dir():
    try:
        raw = open(os.path.expandvars(r"%LOCALAPPDATA%\NetEase\CloudMusic\cache_path"), "rb").read()
        path = raw.decode("utf-16-le" if b"\x00" in raw else "utf-8").strip().strip("\x00")
        if os.path.isdir(path):
            return path
    except OSError:
        pass
    return DEFAULT_CACHE


def wait_for_audio(song_id, duration, timeout=90.0):
    """Decoded (samples[channels, n], rate) of the cached song once the file is complete."""
    import av
    import numpy as np
    deadline = time.time() + timeout
    last_size = -1
    while time.time() < deadline:
        files = sorted(glob.glob(os.path.join(cache_dir(), f"{song_id}-*.uc")), key=os.path.getsize, reverse=True)
        if files:
            size = os.path.getsize(files[0])
            if size == last_size and size > 0:
                with open(files[0], "rb") as f:
                    data = f.read().translate(XOR_TABLE)
                try:
                    with av.open(io.BytesIO(data)) as container:
                        stream = container.streams.audio[0]
                        rate = stream.rate
                        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=rate)
                        chunks = []
                        for frame in container.decode(stream):
                            for out in resampler.resample(frame):
                                chunks.append(out.to_ndarray())
                    audio = np.concatenate(chunks, axis=1)
                    if not duration or audio.shape[1] / rate >= duration - 2.0:
                        return audio, rate
                except Exception:
                    pass              # still downloading: header or tail not there yet
            last_size = size
        time.sleep(1.5)
    return None, None


def separate_vocals(audio, rate):
    import torch
    os.environ.setdefault("TORCH_HOME", os.path.join(MODELS_DIR, "torch"))
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    import julius
    model = get_model("htdemucs")
    model.eval()
    wav = torch.from_numpy(audio).float()
    if rate != model.samplerate:
        wav = julius.resample_frac(wav, rate, model.samplerate)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        sources = apply_model(model, ((wav - mean) / std)[None], device="cuda", split=True,
                              overlap=0.25, progress=False)[0]
    vocals = sources[model.sources.index("vocals")] * std + mean
    mono16k = julius.resample_frac(vocals.mean(0), model.samplerate, 16000)
    del model, sources
    torch.cuda.empty_cache()
    return mono16k.cpu().numpy().astype("float32")


def language_hint(text):
    """Script of the title/artist: kana -> ja, hangul -> ko, han only -> zh, else ''."""
    if re.search("[\u3040-\u30ff]", text):
        return "ja"
    if re.search("[\uac00-\ud7af]", text):
        return "ko"
    if re.search("[\u4e00-\u9fff]", text):
        return "zh"
    return ""


def pick_language(model, vocals, hint):
    """Detect over several stretches of the vocals; trust the title's script unless the audio clearly disagrees."""
    lang, prob, ranked = model.detect_language(vocals, vad_filter=True, language_detection_segments=6)
    if not hint or lang == hint:
        return lang
    probs = dict(ranked)
    if hint == "zh" and lang in ("yue", "ja") and prob > 0.5:
        return lang                # Cantonese, or a Japanese song with a kanji-only title
    if prob > 0.85 and probs.get(hint, 0) < 0.05:
        return lang                # e.g. an English song under a Chinese title
    return hint


def transcribe(vocals, hint="", **overrides):
    import torch
    torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
    if os.path.isdir(torch_lib):           # CTranslate2 loads cuBLAS/cuDNN from torch's copy
        os.add_dll_directory(torch_lib)
        os.environ["PATH"] = torch_lib + os.pathsep + os.environ.get("PATH", "")
    from faster_whisper import WhisperModel
    global _whisper
    if _whisper is None:
        _whisper = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16",
                                download_root=os.path.join(MODELS_DIR, "whisper"))
    model = _whisper
    options = dict(beam_size=5, word_timestamps=True, vad_filter=True,
                   # Silero VAD is tuned for speech and drops sung phrases at its default
                   # threshold; looser keeps the words while still anchoring timestamps.
                   vad_parameters=dict(threshold=0.2, min_silence_duration_ms=800),
                   condition_on_previous_text=False, hallucination_silence_threshold=2.0,
                   temperature=(0.0, 0.2, 0.4))       # fewer runaway retries on unclear stretches
    language = pick_language(model, vocals, hint)
    prompt = "以下是普通话歌词，使用简体中文。" if language == "zh" else None   # simplified, not traditional
    options.update(overrides)
    segments, info = model.transcribe(vocals, language=language, initial_prompt=prompt, **options)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append((w.start, w.end, w.word, w.probability, seg.id))
    return words, info.language


def to_lines(words):
    """Group words into display lines at pauses / length limits; drop filler and noise."""
    lines, cur = [], []

    def too_long(ws, hard=False):
        text = "".join(w[2] for w in ws)
        units = len(CJK.findall(text)) + len(re.findall(r"[A-Za-z0-9']+", text))
        return units >= (22 if hard else 13)

    def flush():
        if not cur:
            return
        texts = [w[2] for w in cur]
        texts[0] = texts[0].lstrip()
        for i in range(1, len(texts)):       # leading spaces belong to the previous word
            if texts[i][:1].isspace():
                texts[i - 1] += " "
                texts[i] = texts[i].lstrip()
        text = "".join(texts).strip()
        prob = sum(w[3] for w in cur) / len(cur)
        if text and prob >= 0.35 and not HALLUCINATIONS.search(text):
            lines.append({"start": round(cur[0][0], 3),
                          "end": round(cur[-1][1] + 0.3, 3),
                          "words": [[round(w[0], 3), round(min(w[1], w[0] + 2.0), 3), t]
                                    for w, t in zip(cur, texts) if t]})
        cur.clear()

    for w in words:
        if cur:
            gap = w[0] - cur[-1][1]
            if gap > 0.6 or (w[4] != cur[-1][4] and gap > 0.25) or (too_long(cur) and gap > 0.12) \
                    or too_long(cur, hard=True):
                flush()
        cur.append(w)
    flush()

    # Whisper sometimes loops on one phrase; real choruses don't repeat 6+ times back to back.
    cleaned, run = [], 0
    for ln in lines:
        same = cleaned and "".join(w[2] for w in ln["words"]) == "".join(w[2] for w in cleaned[-1]["words"])
        run = run + 1 if same else 0
        if run < 5:
            cleaned.append(ln)
    for a, b in zip(cleaned, cleaned[1:]):
        a["end"] = min(a["end"], b["start"])
    return cleaned


def main():
    song_id, duration, out_path = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    hint = sys.argv[4] if len(sys.argv) > 4 else ""
    t0 = time.time()
    result = {"source": "AI听写", "model": WHISPER_MODEL}
    try:
        stage("audio")
        audio, rate = wait_for_audio(song_id, duration)
        if audio is None:
            result["error"] = "no_cache"
        else:
            stage("vocals")
            vocals = separate_vocals(audio, rate)
            stage("words")
            words, language = transcribe(vocals, hint)
            result.update(language=language, lines=to_lines(words))
    except Exception as e:                     # report, never crash silently
        result["error"] = f"{type(e).__name__}: {e}"
    result["elapsed"] = round(time.time() - t0, 1)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, out_path)
    stage("done")


if __name__ == "__main__":
    main()
