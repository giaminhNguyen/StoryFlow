"""VieNeu-TTS synthesis worker. Runs under the VieNeu venv interpreter, NOT the StoryFlow one.

    python vieneu_worker.py check   < {}            -> {"ok":true,"vieneu_importable":true,"version":..}
    python vieneu_worker.py synth   < request JSON  -> JSON lines (see below)

Standalone on purpose: stdlib only at import time (numpy is used opportunistically when present),
so it never pulls torch/onnx into any other environment and can be tested with a stub ``vieneu``.

Protocol (STDOUT, one JSON object per line, flushed; anything else printed goes to stderr):
    {"event":"ready","sample_rate":N}
    {"event":"chunk","index":i,"out":"0001.wav","duration_ms":N}      chunk synthesized
    {"event":"skip","index":i,"out":"0002.wav"}                        already valid, untouched
    {"event":"done","made":n,"skipped":m}
    {"event":"error","index":i|null,"kind":K,"message":"short, no paths"}   then exit code 2
Error kinds: voice_not_found, oom, model_load, import_error, empty_text, synth_failed, io_error, bad_request.

Request (synth): {voice, precision, threads, temperature, gap_seconds,
                  items:[{"text_file": abs, "out_file": abs}]}  (absolute paths; used for this call only)
Every wav is written to ``<out_file>.part`` (PCM16 mono) and promoted with os.replace; an existing valid
out_file is never touched. Chunk text is never printed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import wave

# This directory contains ``vieneu.py`` (the StoryFlow adapter): it must never shadow the real ``vieneu``
# package that the venv provides, so drop the script directory from sys.path before anything imports it.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]

# Keep the real stdout for the protocol; library prints (progress bars, logs) go to stderr.
_OUT = sys.stdout
sys.stdout = sys.stderr

MAX_PIECE_CHARS = 700
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
_SYNTH_ERRORS = (ValueError, RuntimeError, MemoryError, OSError, wave.Error, ArithmeticError, LookupError,
                 TypeError, AttributeError)


def emit(obj: dict) -> None:
    _OUT.write(json.dumps(obj) + "\n")  # ASCII-only: independent of the console code page
    _OUT.flush()


_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/][^\s'\"]*|/(?:[\w.\-]+/)+[\w.\-]*)")


def short(exc: BaseException, limit: int = 160) -> str:
    text = _PATH_RE.sub("<path>", f"{type(exc).__name__}: {exc}")
    return " ".join(text.split())[:limit]


def split_pieces(text: str, max_chars: int = MAX_PIECE_CHARS) -> list[str]:
    """<= max_chars pieces, preferring paragraph then sentence boundaries (like queue_runner.py)."""
    pieces, cur = [], ""
    for para in (p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()):
        parts = [para] if len(para) <= max_chars else [s for s in _SENTENCE_RE.split(para) if s]
        for part in parts:
            while len(part) > max_chars:  # a single over-long sentence: hard split on a space
                cut = part.rfind(" ", 0, max_chars + 1)
                cut = cut if cut >= max_chars // 2 else max_chars
                if cur:
                    pieces.append(cur)
                    cur = ""
                pieces.append(part[:cut].strip())
                part = part[cut:].strip()
            if not part:
                continue
            if cur and len(cur) + len(part) + 2 > max_chars:
                pieces.append(cur)
                cur = part
            else:
                cur = f"{cur}\n\n{part}".strip() if cur else part
    if cur:
        pieces.append(cur)
    return pieces


def to_pcm16(audio) -> bytes:
    """Mono float samples in [-1, 1] -> little-endian PCM16 bytes (numpy if available, else stdlib)."""
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        arr = np.asarray(audio, dtype=np.float32).reshape(-1)
        return (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    from array import array
    samples = audio.tolist() if hasattr(audio, "tolist") else list(audio)
    out = array("h", (max(-32768, min(32767, int(round(x * 32767.0)))) for x in samples))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def valid_wav(path: str) -> int | None:
    """duration_ms of a well-formed, non-empty PCM wav (mono, 8/16-bit), else None."""
    try:
        with wave.open(path, "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
            if frames <= 0 or rate <= 0 or w.getnchannels() != 1 or w.getsampwidth() not in (1, 2):
                return None
            if w.getcomptype() != "NONE":
                return None
            return frames * 1000 // rate
    except (OSError, EOFError, wave.Error):
        return None


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _read_request() -> dict:
    raw = sys.stdin.buffer.read()
    return json.loads(raw.decode("utf-8") or "{}")


def op_check() -> int:
    try:
        import vieneu
    except ImportError as exc:
        emit({"ok": False, "vieneu_importable": False, "message": short(exc)})
        return 2
    emit({"ok": True, "vieneu_importable": True, "version": str(getattr(vieneu, "__version__", "unknown"))})
    return 0


def _voice_message(tts, voice: str) -> str:
    ids = []
    lister = getattr(tts, "list_preset_voices", None)
    if callable(lister):
        try:
            ids = [str(v[1] if isinstance(v, (tuple, list)) and len(v) > 1 else v) for v in lister()]
        except _SYNTH_ERRORS:
            ids = []
    msg = f"voice '{voice[:60]}' not found"
    return msg + (f"; available: {', '.join(ids[:12])}" if ids else "")


def _is_voice_error(exc: BaseException) -> bool:
    return isinstance(exc, ValueError) and "not found" in str(exc).lower() and "voice" in str(exc).lower()


def _synth_item(tts, item: dict, req: dict, rate: int, index: int) -> int:
    text = ""
    with open(item["text_file"], "rb") as fh:
        text = fh.read().decode("utf-8").strip()
    pieces = split_pieces(text)
    if not pieces:
        raise ValueError("empty_text")
    out = item["out_file"]
    part = out + ".part"
    gap = float(req.get("gap_seconds", 0.35))
    silence = b"\x00\x00" * int(rate * gap) if gap > 0 else b""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    _remove(part)
    frames = 0
    try:
        with wave.open(part, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            for n, piece in enumerate(pieces):
                audio = tts.infer(piece, voice=req["voice"], temperature=float(req.get("temperature", 0.8)))
                data = to_pcm16(audio)
                w.writeframes(data)
                frames += len(data) // 2
                if n < len(pieces) - 1 and silence:
                    w.writeframes(silence)
                    frames += len(silence) // 2
        if frames <= 0:
            raise ValueError("empty_audio")
        os.replace(part, out)
    finally:
        _remove(part)  # no-op after a successful promote
    return frames * 1000 // rate


def op_synth() -> int:
    try:
        req = _read_request()
        items = req["items"]
        voice = req["voice"]
        if not isinstance(items, list) or not isinstance(voice, str) or not voice:
            raise ValueError("bad request")
    except (ValueError, KeyError, UnicodeDecodeError):
        emit({"event": "error", "index": None, "kind": "bad_request", "message": "invalid synth request"})
        return 2

    # Skip the model load entirely when there is nothing to do.
    todo = [i for i, it in enumerate(items, 1) if valid_wav(it.get("out_file", "")) is None]
    made = skipped = 0
    tts = None
    rate = 0
    if todo:
        try:
            from vieneu import Vieneu
        except ImportError as exc:
            emit({"event": "error", "index": None, "kind": "import_error", "message": short(exc)})
            return 2
        try:
            tts = Vieneu(mode="v3turbo", precision=str(req.get("precision", "fp32")),
                         threads=int(req.get("threads", 6)))
            rate = int(tts.sample_rate)
        except MemoryError as exc:
            emit({"event": "error", "index": None, "kind": "oom", "message": short(exc)})
            return 2
        except (RuntimeError, OSError, ValueError, TypeError, AttributeError, ArithmeticError) as exc:
            emit({"event": "error", "index": None, "kind": "model_load", "message": short(exc)})
            return 2
        getter = getattr(tts, "get_preset_voice", None)
        if callable(getter):
            try:
                getter(voice)
            except ValueError:
                emit({"event": "error", "index": None, "kind": "voice_not_found",
                      "message": _voice_message(tts, voice)})
                return 2
            except _SYNTH_ERRORS:
                pass  # not a definitive answer; infer() below decides
        emit({"event": "ready", "sample_rate": rate})

    for index, item in enumerate(items, 1):
        try:
            out = str(item["out_file"])
            item["text_file"]
        except (KeyError, TypeError):
            emit({"event": "error", "index": index, "kind": "bad_request", "message": "invalid item"})
            return 2
        name = os.path.basename(out)
        if valid_wav(out) is not None:
            skipped += 1
            emit({"event": "skip", "index": index, "out": name})
            continue
        try:
            duration_ms = _synth_item(tts, item, req, rate, index)
        except _SYNTH_ERRORS as exc:
            _remove(out + ".part")
            if _is_voice_error(exc):
                kind, msg = "voice_not_found", _voice_message(tts, voice)
            elif isinstance(exc, MemoryError):
                kind, msg = "oom", short(exc)
            elif isinstance(exc, ValueError) and str(exc) in ("empty_text", "empty_audio"):
                kind, msg = "empty_text", f"chunk {index} has no speakable text"
            elif isinstance(exc, (OSError, wave.Error)):
                kind, msg = "io_error", short(exc)
            else:
                kind, msg = "synth_failed", short(exc)
            emit({"event": "error", "index": index, "kind": kind, "message": msg})
            return 2
        made += 1
        emit({"event": "chunk", "index": index, "out": name, "duration_ms": duration_ms})
    emit({"event": "done", "made": made, "skipped": skipped})
    return 0


def main(argv: list[str]) -> int:
    op = argv[1] if len(argv) > 1 else ""
    if op == "check":
        return op_check()
    if op == "synth":
        return op_synth()
    emit({"event": "error", "index": None, "kind": "bad_request", "message": "unknown op"})
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
