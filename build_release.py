"""Build the shareable zip: packaged exe (no Python needed), no AI parts, no personal data.

    py build_release.py            -> <Desktop>\\网易云桌面歌词.zip

The AI parts (torch / demucs / faster-whisper, several GB, NVIDIA only) are left out on
purpose; the app notices and skips calibration and transcription. data/ (settings, cached
lyrics, logs, models) is never packed: the exe makes a fresh one next to itself.
"""
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = "网易云桌面歌词"
EXE = "NeteaseLyrics"
EXCLUDE = ["torch", "torchaudio", "torchvision", "demucs", "faster_whisper", "ctranslate2", "julius",
           "transformers", "onnxruntime", "tokenizers", "huggingface_hub", "scipy", "sympy", "pandas",
           "matplotlib", "IPython", "tkinter", "lameenc", "einops", "dora", "openunmix", "numba", "cv2",
           "worker", "align"]
FORBIDDEN = ("/data/", "config.json", "cache/", ".log", "/models/", ".git/", ".align.json", ".ai.json")


def desktop():
    import win32com.client
    return win32com.client.Dispatch("WScript.Shell").SpecialFolders("Desktop")


def main():
    work = os.path.join(ROOT, "build")
    dist = os.path.join(ROOT, "dist")
    icon = os.path.join(ROOT, "data", "icon.ico")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
           "--name", EXE, "--distpath", dist, "--workpath", work, "--specpath", work,
           "--collect-data", "soundcard"]
    if os.path.exists(icon):
        cmd += ["--icon", icon]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]
    subprocess.run(cmd + [os.path.join(ROOT, "main.pyw")], check=True)

    stage = os.path.join(ROOT, "build", "stage", NAME)
    shutil.copytree(os.path.join(dist, EXE), stage)
    for f in os.listdir(os.path.join(ROOT, "release")):
        shutil.copy(os.path.join(ROOT, "release", f), stage)

    target = os.path.join(desktop(), NAME + ".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for folder, _, files in os.walk(stage):
            for f in files:
                full = os.path.join(folder, f)
                z.write(full, os.path.relpath(full, os.path.dirname(stage)))

    with zipfile.ZipFile(target) as z:                 # nothing personal may ride along
        names = z.namelist()
    bad = [n for n in names if any(x in "/" + n.replace("\\", "/") for x in FORBIDDEN)]
    if bad:
        os.remove(target)
        raise SystemExit(f"refused: personal data in the zip: {bad[:5]}")
    size = os.path.getsize(target) / 1e6
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(dist, ignore_errors=True)
    print(f"{target}  {size:.0f} MB, {len(names)} files, privacy check passed")


if __name__ == "__main__":
    main()
