"""What's playing when nobody sings: labels for the lyric bar's 间奏 display.

The separated stems settle the clear cases (vocal samples, bass solo, drum solo);
CLAP (laion/clap-htsat-unfused, zero-shot against a fixed instrument list) names
what's in the "other" stem. A label is only kept when CLAP is confident and the
same label lasts at least MIN_SEGMENT seconds; everything else stays unlabelled
and the bar just says 间奏.

laion/larger_clap_music was tried first: with transformers 5.x its audio
embeddings come out nearly identical for any input, so it can't tell sounds apart.
"""
import os
from collections import Counter

import numpy as np

CLAP_MODEL = "laion/clap-htsat-unfused"
WIN, HOP = 2.0, 1.0           # analysis window and step, seconds
MIN_SEGMENT = 2.0             # shorter labels are dropped instead of flickering
CONFIDENT = 0.40              # CLAP probability needed to name an instrument
MARGIN = 0.12                 # ...and its lead over the runner-up

# (label shown, CLAP prompt, extra confidence required). On electronic tracks CLAP
# hears synth leads as erhu/dizi with up to 0.86 confidence, so no threshold alone
# separates them: Chinese instruments are only offered for songs whose title, artist
# or lyrics are Chinese, and even then need more confidence. Organ is over-picked for
# synth pads, so it asks for a bit more too.
INSTRUMENTS = [
    ("钢琴", "piano", 0.0),
    ("吉他", "acoustic guitar", 0.0),
    ("电吉他", "electric guitar", 0.0),
    ("合成器", "synthesizer", 0.0),
    ("弦乐", "a string section", 0.0),
    ("小提琴", "violin", 0.0),
    ("萨克斯", "saxophone", 0.0),
    ("铜管", "a brass section with trumpets", 0.0),
    ("长笛", "flute", 0.0),
    ("口琴", "harmonica", 0.0),
    ("手风琴", "accordion", 0.0),
    ("风琴", "organ", 0.1),
    ("八音盒", "a music box", 0.0),
    ("唢呐声", "suona, a loud Chinese double-reed horn", 0.2),
    ("二胡", "erhu, a Chinese two-string fiddle", 0.2),
    ("古筝", "guzheng, a Chinese plucked zither", 0.2),
    ("琵琶", "pipa, a Chinese plucked lute", 0.2),
    ("笛子", "dizi, a Chinese bamboo flute", 0.2),
]
CHINESE = {"唢呐声", "二胡", "古筝", "琵琶", "笛子"}


def stem_labels(stems, sr):
    """Per window: a label decided from stem loudness, 'clap' if the other stem needs naming, or None."""
    n = int((len(stems["other"]) / sr - WIN) // HOP) + 1
    energy = {}
    for name, x in stems.items():
        e = np.empty(max(n, 0))
        for k in range(n):
            seg = x[int(k * HOP * sr): int((k * HOP + WIN) * sr)]
            e[k] = float((seg.astype(np.float64) ** 2).mean()) if len(seg) else 0.0
        energy[name] = e
    total = sum(energy.values())
    quiet = np.percentile(total, 95) * 0.003 if n else 0
    out = []
    for k in range(n):
        if total[k] <= quiet:
            out.append(None)
            continue
        inst = energy["drums"][k] + energy["bass"][k] + energy["other"][k] + 1e-12
        if energy["vocals"][k] / total[k] >= 0.35:
            out.append("人声采样")            # voices with no lyric line on them
        elif energy["bass"][k] / inst >= 0.6 and energy["other"][k] / inst < 0.2:
            out.append("贝斯独奏")
        elif energy["drums"][k] / inst >= 0.65 and energy["other"][k] / inst < 0.15:
            out.append("鼓独奏")
        elif energy["other"][k] / inst >= 0.25:
            out.append("clap")
        else:
            out.append(None)
    return out


def name_instruments(other, sr, windows, models_dir, allow_chinese, batch=32):
    """CLAP label (or None) for each requested window of the 'other' stem."""
    candidates = [c for c in INSTRUMENTS if allow_chinese or c[0] not in CHINESE]
    import julius
    import torch
    from transformers import ClapModel, ClapProcessor
    cache = os.path.join(models_dir, "clap")
    model = ClapModel.from_pretrained(CLAP_MODEL, cache_dir=cache).to("cuda").eval()
    proc = ClapProcessor.from_pretrained(CLAP_MODEL, cache_dir=cache)
    x48 = julius.resample_frac(torch.from_numpy(other), sr, 48000).numpy()
    with torch.no_grad():
        ti = proc(text=[f"the sound of {p}" for _, p, _ in candidates], return_tensors="pt", padding=True).to("cuda")
        temb = torch.nn.functional.normalize(model.get_text_features(**ti).pooler_output, dim=-1)
        scale = model.logit_scale_a.exp()
        labels = []
        for i in range(0, len(windows), batch):
            clips = [x48[int(k * HOP * 48000): int((k * HOP + WIN) * 48000)] for k in windows[i:i + batch]]
            ai = proc(audio=clips, sampling_rate=48000, return_tensors="pt").to("cuda")
            aemb = torch.nn.functional.normalize(model.get_audio_features(**ai).pooler_output, dim=-1)
            probs = (scale * aemb @ temb.T).softmax(-1).cpu().numpy()
            for p in probs:
                order = np.argsort(p)[::-1]
                best, second = order[0], order[1]
                name, _, extra = candidates[best]
                ok = p[best] >= CONFIDENT + extra and p[best] - p[second] >= MARGIN
                labels.append(name if ok else None)
    del model
    torch.cuda.empty_cache()
    return labels


def to_segments(labels):
    """Smooth per-window labels and merge them into [[start, end, label]] segments."""
    smooth = list(labels)
    for k in range(1, len(labels) - 1):          # one-window blips take their neighbours' label
        trio = Counter(labels[k - 1:k + 2]).most_common(1)[0]
        if trio[1] >= 2:
            smooth[k] = trio[0]
    segments = []
    for k, lab in enumerate(smooth):
        start, end = k * HOP + (WIN - HOP) / 2, k * HOP + (WIN + HOP) / 2   # each window owns its middle second
        if segments and segments[-1][2] == lab and abs(segments[-1][1] - start) < 1e-6:
            segments[-1][1] = end
        else:
            segments.append([start, end, lab])
    return [[round(s, 2), round(e, 2), lab] for s, e, lab in segments if lab and e - s >= MIN_SEGMENT]


def label_song(stems, sr, models_dir, allow_chinese=False):
    labels = stem_labels(stems, sr)
    ask = [k for k, lab in enumerate(labels) if lab == "clap"]
    if ask:
        for k, name in zip(ask, name_instruments(stems["other"], sr, ask, models_dir, allow_chinese)):
            labels[k] = name
    return to_segments(labels)
