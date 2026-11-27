#!/usr/bin/env python3
"""Turn a YouTube video or a local video/audio file into a text transcript.

Order of work:
  1. YouTube URL -> use the video's own captions if available (fast).
  2. Otherwise   -> extract audio and run Whisper speech recognition locally.

The transcript always stays in the spoken language. Nothing is translated.
"""
import argparse
import html
import importlib.util
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

YTDLP = [sys.executable, "-m", "yt_dlp"]
CUE = re.compile(
    r"^((?:\d+:)?\d\d:\d\d[.,]\d{3})\s*-->\s*((?:\d+:)?\d\d:\d\d[.,]\d{3})"
)


# ---------------------------------------------------------------- helpers
def log(msg):
    print(msg, file=sys.stderr, flush=True)


def have(module):
    return importlib.util.find_spec(module) is not None


def run(cmd):
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()[-5:]
        raise RuntimeError("Command failed: %s\n%s" % (" ".join(cmd[:4]), "\n".join(tail)))


def is_url(s):
    return s.startswith(("http://", "https://"))


def safe_name(s):
    return re.sub(r"[^\w\-. ]+", "_", s, flags=re.U).strip(" ._")[:80] or "transcript"


def to_sec(ts):
    parts = ts.replace(",", ".").split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def clock(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


def srt_time(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def check_env():
    def online():
        try:
            socket.create_connection(("www.youtube.com", 443), timeout=4).close()
            return True
        except OSError:
            return False

    rows = [
        ("ffmpeg", bool(shutil.which("ffmpeg")), "needed to extract audio"),
        ("yt-dlp", have("yt_dlp"), "pip install yt-dlp  (YouTube links)"),
        ("faster-whisper", have("faster_whisper"), "pip install faster-whisper  (speech recognition)"),
        ("openai-whisper", have("whisper"), "optional alternative to faster-whisper"),
        ("network", online(), "needed for YouTube and for first model download"),
    ]
    for name, ok, note in rows:
        print("%-15s %-8s %s" % (name, "ok" if ok else "MISSING", "" if ok else note))
    whisper_ok = have("faster_whisper") or have("whisper")
    return 0 if whisper_ok or have("yt_dlp") else 1


# ---------------------------------------------------------------- captions
def parse_vtt(text):
    """Return [(start, end, line)] with markup removed and rolling duplicates dropped."""
    cues, cur, last = [], None, None
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        m = CUE.match(line)
        if m:
            cur = (to_sec(m.group(1)), to_sec(m.group(2)))
            continue
        if not line:
            cur = None
            continue
        if cur is None:  # header, NOTE blocks, cue identifiers
            continue
        clean = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if clean and clean != last:
            cues.append((cur[0], cur[1], clean))
            last = clean
    return cues


def merge_cues(cues, max_chars=220):
    """Join short caption fragments into sentence-like lines.

    A new line starts at sentence-ending punctuation, at a speaker-change
    marker (">>" or a leading dash), or when the line gets long.
    """
    out, buf, start = [], [], None

    def flush():
        nonlocal buf, start
        if buf:
            out.append((start, start, " ".join(buf).strip()))
        buf, start = [], None

    for s, _e, t in cues:
        if t.startswith((">>", "- ")):
            flush()
        if start is None:
            start = s
        buf.append(t)
        if re.search(r"[.!?\u3002\uff01\uff1f\u2026]['\")\]]?$", t) or len(" ".join(buf)) >= max_chars:
            flush()
    flush()
    return out


def yt_info(url, cookies):
    cmd = YTDLP + ["-J", "--no-playlist", "--skip-download"]
    if cookies:
        cmd += ["--cookies", cookies]
    return json.loads(run(cmd + [url]).stdout)


def pick_key(tracks, want):
    keys = [k for k in tracks if k != "live_chat"]
    if want:
        for k in (want + "-orig", want):
            if k in keys:
                return k
        for k in keys:
            if k.startswith(want + "-") or k.split("-")[0] == want.split("-")[0]:
                return k
        return None
    return None


def youtube_captions(url, info, lang, workdir, cookies):
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    want = lang or info.get("language")
    if not want:  # infer the original language from YouTube's "-orig" auto track
        orig = [k for k in auto if k.endswith("-orig")]
        want = orig[0][:-5] if orig else None

    key, use_auto = pick_key(manual, want), False
    if key is None:
        key, use_auto = pick_key(auto, want), True
    if key is None and not want:
        keys = [k for k in manual if k != "live_chat"]
        key, use_auto = (keys[0], False) if keys else (None, False)
    if key is None:
        return None

    cmd = YTDLP + [
        "--no-playlist", "--skip-download",
        "--write-auto-subs" if use_auto else "--write-subs",
        "--sub-langs", key, "--sub-format", "vtt",
        "-o", str(Path(workdir) / "%(id)s.%(ext)s"),
    ]
    if cookies:
        cmd += ["--cookies", cookies]
    run(cmd + [url])
    files = sorted(Path(workdir).glob("*.vtt"))
    if not files:
        return None
    cues = parse_vtt(files[0].read_text(encoding="utf-8", errors="replace"))
    if not cues:
        return None
    label = "%s %s captions" % ("automatic" if use_auto else "uploaded", key)
    return cues, label


# ---------------------------------------------------------------- whisper
def get_audio(source, workdir, cookies):
    if is_url(source):
        cmd = YTDLP + [
            "--no-playlist", "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
            "-o", str(Path(workdir) / "audio.%(ext)s"),
        ]
        if cookies:
            cmd += ["--cookies", cookies]
        run(cmd + [source])
        return Path(workdir) / "audio.mp3"
    src = Path(source).expanduser()
    if not src.exists():
        raise SystemExit("File not found: %s" % src)
    if shutil.which("ffmpeg"):
        wav = Path(workdir) / "audio.wav"
        run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
        return wav
    log("ffmpeg not found; passing the file to the speech model directly.")
    return src


def transcribe_whisper(audio, model_size, lang, device):
    if have("faster_whisper"):
        from faster_whisper import WhisperModel

        log("Loading faster-whisper model '%s' (first run downloads it)..." % model_size)
        model = WhisperModel(model_size, device=device, compute_type="auto")
        # task="transcribe" keeps the original language; "translate" would convert to English.
        segs, info = model.transcribe(
            str(audio), language=lang, task="transcribe", vad_filter=True, beam_size=5
        )
        log("Detected language: %s (p=%.2f)" % (info.language, info.language_probability))
        out = []
        for s in segs:
            text = s.text.strip()
            if text:
                out.append((s.start, s.end, text))
            log("  %s" % clock(s.end))
        return out, info.language
    if have("whisper"):
        import whisper

        log("Loading openai-whisper model '%s'..." % model_size)
        model = whisper.load_model(model_size, device=None if device == "auto" else device)
        r = model.transcribe(str(audio), language=lang, task="transcribe")
        out = [(s["start"], s["end"], s["text"].strip()) for s in r["segments"] if s["text"].strip()]
        return out, r.get("language", lang)
    raise SystemExit(
        "No speech-recognition package found. Install one:\n  pip install faster-whisper"
    )


# ---------------------------------------------------------------- output
def write_txt(segments, out_dir, base, timestamps):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (base + ".txt")
    lines = [("[%s] %s" % (clock(s), t)) if timestamps else t for s, _e, t in segments]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_srt(segments, out_dir, base):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (base + ".srt")
    blocks = []
    for i, (s, e, t) in enumerate(segments, 1):
        e = max(e, s + 1.0)
        blocks.append("%d\n%s --> %s\n%s\n" % (i, srt_time(s), srt_time(e), t))
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="YouTube URL or path to a video/audio file")
    ap.add_argument("--out-dir", default=".", help="where to write the transcript (default: current dir)")
    ap.add_argument("--lang", help="spoken language code, e.g. en, de, bn, tr (default: auto-detect)")
    ap.add_argument("--engine", choices=["auto", "captions", "whisper"], default="auto")
    ap.add_argument("--model", default="small", help="Whisper size: tiny, base, small, medium, large-v3")
    ap.add_argument("--device", default="auto", help="auto, cpu or cuda")
    ap.add_argument("--timestamps", action="store_true", help="prefix each line with [mm:ss]")
    ap.add_argument("--srt", action="store_true", help="also write an .srt subtitle file")
    ap.add_argument("--cookies", help="cookies.txt for yt-dlp (age-restricted or bot-checked videos)")
    ap.add_argument("--check", action="store_true", help="report which tools are available and exit")
    a = ap.parse_args()

    if a.check:
        sys.exit(check_env())
    if not a.source:
        ap.error("source is required (YouTube URL or file path)")

    url = is_url(a.source)
    if url and not have("yt_dlp"):
        raise SystemExit("yt-dlp is required for URLs:  pip install yt-dlp")
    if a.engine == "captions" and not url:
        raise SystemExit("--engine captions only works with YouTube URLs")

    out_dir = Path(a.out_dir).expanduser()
    segments, txt_segments, how, base = None, None, "", None

    with tempfile.TemporaryDirectory() as tmp:
        if url:
            info = yt_info(a.source, a.cookies)
            base = safe_name("%s-%s" % (info.get("title", "video"), info.get("id", "")))
            if a.engine in ("auto", "captions"):
                try:
                    res = youtube_captions(a.source, info, a.lang, tmp, a.cookies)
                except RuntimeError as e:
                    log("Caption download failed: %s" % e)
                    res = None
                if res:
                    cues, how = res
                    segments = cues  # raw cues keep exact timing for .srt
                    txt_segments = merge_cues(cues)  # sentence-like lines for .txt
                elif a.engine == "captions":
                    raise SystemExit("This video has no usable captions. Try --engine whisper.")
                else:
                    log("No usable captions; falling back to speech recognition.")
        else:
            base = safe_name(Path(a.source).stem)

        if segments is None:
            audio = get_audio(a.source, tmp, a.cookies)
            segments, lang = transcribe_whisper(audio, a.model, a.lang, a.device)
            how = "Whisper (%s), language: %s" % (a.model, lang)
            txt_segments = segments

    written = [write_txt(txt_segments, out_dir, base, a.timestamps)]
    if a.srt:
        written.append(write_srt(segments, out_dir, base))
    log("Source of text: %s" % how)
    for p in written:
        print(p)


if __name__ == "__main__":
    main()
