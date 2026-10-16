"""Keep the lyric clock on the music you actually hear.

NetEase's own progress value is right when a song plays from the start, but after
a seek it can stay off by a varying amount until the next seek or song: measured
on this machine it ran 0.6 s and 1.0 s ahead of the sound after two seeks. So
right after a seek (and now and then while playing) we record ~1.6 s of what the
speakers play (WASAPI loopback; compared in memory, never stored), find that
snippet in the song's cached audio and correct the clock by the difference.

Loopback is taken before the output device, so headphone / Bluetooth latency is
still the user's global offset to set.
"""
import glob
import io
import os
import threading
import time

import numpy as np

import transcribe
from config import DATA_DIR

FS = 8000              # matching sample rate
REC_RATE = 48000
SNIP = 1.6             # seconds recorded per check
SEARCH = 4.0           # look this far either side of where the clock says we are
GOOD = 0.7             # normalised correlation needed to trust a match
PERIOD = 30.0          # routine re-check while playing
SETTLE = 1.0           # after a seek NetEase restarts the sound late: let it settle first
RETRY = 4.0            # after a failed check
XOR_TABLE = bytes(b ^ 0xA3 for b in range(256))
LOG = os.path.join(DATA_DIR, "sync.log")


def _log(text):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 200_000:
            os.replace(LOG, LOG + ".old")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def _lowpass_taps(cutoff, rate, n=129):
    t = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff / rate * t) * np.hamming(n)
    return (h / h.sum()).astype(np.float32)


_TAPS = _lowpass_taps(3600, REC_RATE)


def decimate(x48k):
    """48 kHz mono -> 8 kHz mono."""
    return np.convolve(x48k, _TAPS, mode="same")[:: REC_RATE // FS].astype(np.float32)


def load_reference(song_id, duration, stop, timeout=90.0):
    """The cached song as 8 kHz mono float32 once NetEase has the whole file, else None."""
    import av
    deadline, last_size = time.time() + timeout, -1
    while time.time() < deadline and not stop():
        files = sorted(glob.glob(os.path.join(transcribe.cache_dir(), f"{song_id}-*.uc")),
                       key=os.path.getsize, reverse=True)
        if files:
            size = os.path.getsize(files[0])
            if size == last_size and size > 0:
                try:
                    with open(files[0], "rb") as f:
                        data = f.read().translate(XOR_TABLE)
                    with av.open(io.BytesIO(data)) as container:
                        stream = container.streams.audio[0]
                        resampler = av.AudioResampler(format="flt", layout="mono", rate=FS)
                        parts = [out.to_ndarray().reshape(-1) for frame in container.decode(stream)
                                 for out in resampler.resample(frame)]
                    ref = np.concatenate(parts).astype(np.float32)
                    if not duration or len(ref) / FS >= duration - 2.0:
                        return ref - ref.mean()
                except Exception:
                    pass            # still downloading
            last_size = size
        time.sleep(1.0)
    return None


class AudioSync:
    def __init__(self, reader, cfg):
        self.reader, self.cfg = reader, cfg
        self.correction = 0.0          # seconds to subtract from the reader's position
        self.last = ""                 # last check, for the log / diagnostics
        self._song = None
        self._ref = None
        self._c2 = None
        self._device = None
        self._stop = threading.Event()
        threading.Thread(target=self._run, name="audio-sync", daemon=True).start()

    def stop(self):
        self._stop.set()

    # -- loop ------------------------------------------------------------------------

    def _run(self):
        next_check, seen_epoch, after_seek = 0.0, None, False
        while not self._stop.wait(0.2):
            if not self.cfg.get("audio_sync", True):
                self.correction = 0.0
                continue
            state, song, duration, _ = self.reader.snapshot()
            if song != self._song:
                self._song, self._ref, self.correction = song, None, 0.0
                seen_epoch, after_seek = self.reader.epoch, False
                next_check = time.monotonic() + 3.0
                if song.isdigit():
                    threading.Thread(target=self._load, args=(song, duration), daemon=True).start()
                continue
            if state != "playing" or self._ref is None:
                continue
            now = time.monotonic()
            if self.reader.epoch != seen_epoch:                  # seek or resume
                seen_epoch = self.reader.epoch
                if self.reader.last_jump == "seek":
                    self.correction = self.cfg.get("seek_lead", 0.0)   # typical lead until measured
                    after_seek = True
                next_check = now + SETTLE
            if now >= next_check:
                ok = self._check(after_seek)
                after_seek = after_seek and not ok
                next_check = now + (PERIOD if ok else RETRY)

    def _load(self, song, duration):
        ref = load_reference(song, duration, lambda: self._stop.is_set() or self._song != song)
        if ref is not None and self._song == song:
            self._c2 = np.cumsum(np.r_[0.0, ref.astype(np.float64) ** 2])
            self._ref = ref

    # -- one measurement -------------------------------------------------------------------

    def _record(self):
        import soundcard as sc
        devices = [self._device] if self._device else []
        devices += [m for m in sc.all_microphones(include_loopback=True) if m.isloopback and m not in devices]
        default = str(sc.default_speaker().name)
        devices.sort(key=lambda m: m is not self._device and m.name != default)
        for mic in devices[:3]:
            try:
                with mic.recorder(samplerate=REC_RATE, channels=2, blocksize=960) as rec:
                    rec.record(numframes=REC_RATE // 10)             # drop what was buffered
                    data = rec.record(numframes=int(SNIP * REC_RATE))
                    t_end = time.perf_counter()
            except Exception:
                continue
            mono = data.mean(1).astype(np.float32)
            if np.sqrt((mono ** 2).mean()) > 1e-4:                  # this device carries sound
                self._device = mic
                return t_end, mono
        return None

    def _locate(self, q, centre):
        ref, c2 = self._ref, self._c2
        a = max(0, int((centre - SEARCH) * FS))
        b = min(len(ref), int((centre + SEARCH) * FS) + len(q))
        seg = ref[a:b]
        if len(seg) <= len(q):
            return None, 0.0
        n = 1 << int(np.ceil(np.log2(len(seg) + len(q))))
        corr = np.fft.irfft(np.fft.rfft(seg, n) * np.conj(np.fft.rfft(q, n)), n)[: len(seg) - len(q) + 1]
        energy = np.sqrt(np.maximum(c2[a + len(q): a + len(seg) + 1] - c2[a: a + len(seg) - len(q) + 1], 0))
        ncc = corr / (energy * np.sqrt(float((q.astype(np.float64) ** 2).sum())) + 1e-9)
        k = int(np.argmax(ncc))
        return (a + k) / FS, float(ncc[k])

    def _check(self, after_seek):
        song = self._song
        rec = self._record()
        if rec is None:
            self.last = "no sound on any loopback device"
            return False
        t_end, x = rec
        state, now_song, _, pos_now = self.reader.snapshot()
        if now_song != song or state != "playing":
            return False
        clock_end = pos_now - (time.perf_counter() - t_end)      # reader position when recording ended
        q = decimate(x)
        q -= q.mean()
        start, score = self._locate(q, clock_end - SNIP)
        if start is None or score < GOOD:
            self.last = f"no confident match ({score:.2f})"
            _log(f"{song} {'seek ' if after_seek else ''}no match {score:.2f}")
            return False
        error = clock_end - (start + len(q) / FS)
        if abs(error - self.correction) > 0.03:
            self.correction = error
        if after_seek:              # learn how far NetEase usually runs ahead right after a seek
            self.cfg["seek_lead"] = round(0.7 * self.cfg.get("seek_lead", 0.0) + 0.3 * error, 3)
        self.last = f"clock off by {error * 1000:+.0f} ms (match {score:.2f})"
        _log(f"{song} {'seek ' if after_seek else ''}off {error * 1000:+.0f} ms match {score:.2f}")
        return True
