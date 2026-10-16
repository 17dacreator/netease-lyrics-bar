"""Settings stored as JSON next to the app (data/config.json)."""
import json
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    "global_offset": 0.0,      # seconds; positive = lyrics appear earlier
    "song_offsets": {},        # song id -> extra seconds for that song only
    "scope": "all",            # which offset the buttons/hotkeys adjust: all | song
    "font_size": 34,
    "theme": 0,
    "show_trans": True,
    "show_next": True,         # preview the upcoming line under the current one
    "reveal": True,            # words appear one by one
    "autofill": True,          # look in other lyric libraries when NetEase has none
    "ai_transcribe": True,     # last resort: transcribe the cached audio with Whisper
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

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
