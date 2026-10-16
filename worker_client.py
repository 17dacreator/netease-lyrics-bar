"""The lyric bar's side of worker.py: start it on demand, send jobs, route its replies."""
import importlib.util
import json
import os
import subprocess
import sys
import threading

from config import DATA_DIR

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(APP_DIR, "worker.py")
LOG = os.path.join(DATA_DIR, "worker.log")


def ai_available():
    """Calibration and transcription need a Python with torch, demucs and faster-whisper.
    The packaged build for friends ships without them (several GB, NVIDIA only)."""
    if getattr(sys, "frozen", False) or not os.path.exists(WORKER):
        return False
    return all(importlib.util.find_spec(m) for m in ("torch", "demucs", "faster_whisper"))


class WorkerClient:
    def __init__(self):
        self.available = ai_available()
        self._proc = None
        self._lock = threading.Lock()
        self._handlers = {}
        self._next_id = 1

    def _ensure(self):
        if self._proc and self._proc.poll() is None:
            return
        exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        log = open(LOG, "a", encoding="utf-8")
        self._proc = subprocess.Popen([exe if os.path.exists(exe) else sys.executable, WORKER],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                      text=True, encoding="utf-8", bufsize=1, cwd=APP_DIR,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()

    def submit(self, job, on_event=None):
        """Queue a job; on_event(msg) runs on a background thread for each reply. Returns its id."""
        with self._lock:
            self._ensure()
            jid = self._next_id
            self._next_id += 1
            if on_event:
                self._handlers[jid] = on_event
            try:
                self._proc.stdin.write(json.dumps({**job, "id": jid}, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except OSError:
                self._handlers.pop(jid, None)
                if on_event:
                    on_event({"id": jid, "error": "worker unavailable"})
        return jid

    def cancel(self, jid):
        with self._lock:
            handler = self._handlers.pop(jid, None)
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.stdin.write(json.dumps({"cancel": jid}) + "\n")
                    self._proc.stdin.flush()
                except OSError:
                    pass
        if handler:                      # wake anyone waiting on it
            handler({"id": jid, "error": "cancelled"})

    def stop(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()

    def _read(self, proc):
        for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            with self._lock:
                handler = self._handlers.get(msg.get("id"))
                if handler and ("done" in msg or "error" in msg):
                    self._handlers.pop(msg["id"], None)
            if handler:
                handler(msg)
        with self._lock:                 # the worker ended (idle timeout or crash)
            if self._proc is proc:
                left, self._handlers = self._handlers, {}
            else:
                left = {}
        for jid, handler in left.items():
            handler({"id": jid, "error": "worker exited"})
