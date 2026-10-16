"""Settings stored as JSON next to the app (data/config.json)."""
import json
import os
import sys

# Packaged (PyInstaller) builds keep their data next to the exe, not inside _internal.
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    "global_offset": 0.0,      # seconds; positive = lyrics appear earlier
    "song_offsets": {},        # song id -> extra seconds for that song only
    "scope": "all",            # which offset the buttons/hotkeys adjust: all | song
    "font_size": 34,
    "theme": 0,                # index into overlay.THEMES; len(THEMES) = custom_color
    "custom_color": None,      # "#rrggbb" picked by the user
    "show_trans": True,
    "show_next": True,         # preview the upcoming line under the current one
    "reveal": True,            # words appear one by one
    "sweep": True,             # sung part fills from white to the theme colour, left to right
    "ai_transcribe": True,     # 找词 found nothing that fits: use the Whisper transcription instead
    "calibrate": True,         # line word timing up with the audio (once per song, cached)
    "audio_sync": True,        # after a seek, re-time the clock from what the speakers play
    "seek_lead": 0.0,          # learnt: how far NetEase's progress usually runs ahead after a seek
    "gap_label": "simple",     # label stretches nobody sings with 前奏/间奏/尾奏: off | simple
    "locked": False,
    "visible": True,
    "x": None,
    "y": None,
    "width": None,
    "first_run": True,
    "hide_tip_shown": False,
}


class Config(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            self.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        self["song_offsets"] = dict(self.get("song_offsets") or {})
        if self["gap_label"] not in ("off", "simple"):      # "detail" (instrument names) was removed
            self["gap_label"] = "simple"

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
