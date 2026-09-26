"""Interactive provider configuration for StoryFlow (called at the end of scripts\\setup.bat).

Asks one question at a time, auto-detects everything it can, installs the optional subtitle
dependencies and writes ``backend/.env`` (UTF-8, git-ignored). No credentials are read or stored.

    python scripts/configure_providers.py            interactive
    python scripts/configure_providers.py --yes      accept every detected default, ask nothing
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "backend" / ".env"
VENV_PY = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
SUBTITLE_REQ = ROOT / "backend" / "requirements-subtitle.txt"
CHANNEL_REQ = ROOT / "backend" / "requirements-channel.txt"
YES = "--yes" in sys.argv

for stream in (sys.stdout, sys.stdin):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def ask(question: str, default: str = "") -> str:
    if YES:
        return default
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  ? {question}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def ask_yes(question: str, default: bool = True) -> bool:
    if YES:
        return default
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{question} ({hint})", "").lower()
    if not answer:
        return default
    return answer.startswith(("y", "c"))  # y / yes / c(ó)


def heading(text: str) -> None:
    print(f"\n=== {text} ===")


# ---------------------------------------------------------------------------- subtitles


def configure_subtitles(env: dict[str, str]) -> None:
    heading("1/4  Phụ đề YouTube")
    print("  Lấy phụ đề thật từ YouTube (thư viện youtube-transcript-api).")
    if not ask_yes("Bật lấy phụ đề thật?"):
        env["STORYFLOW_SUBTITLE_PROVIDER"] = "none"
        return
    print("  Đang cài thư viện phụ đề...")
    rc = subprocess.call([str(VENV_PY), "-m", "pip", "install", "-q", "-r", str(SUBTITLE_REQ)])
    if rc != 0:
        print("  ! Cài thư viện thất bại (kiểm tra mạng). Tạm tắt phụ đề thật; chạy lại setup để thử lại.")
        env["STORYFLOW_SUBTITLE_PROVIDER"] = "none"
        return
    env["STORYFLOW_SUBTITLE_PROVIDER"] = "external"
    print("  OK")


# ---------------------------------------------------------------------------- channels


def configure_channels(env: dict[str, str]) -> None:
    heading("4/4  Link channel / playlist YouTube")
    print("  Cho phép dán link CHANNEL hoặc PLAYLIST để tự lấy danh sách video (dùng yt-dlp, không cần API key).")
    print("  Link 1 video lẻ thì luôn dùng được, không cần bước này.")
    if not ask_yes("Bật link channel / playlist?"):
        return
    print("  Đang cài yt-dlp...")
    rc = subprocess.call([str(VENV_PY), "-m", "pip", "install", "-q", "-r", str(CHANNEL_REQ)])
    if rc != 0:
        print("  ! Cài yt-dlp thất bại (kiểm tra mạng). Link channel tạm không dùng được; chạy lại setup để thử lại.")
        return
    print("  OK")


# ---------------------------------------------------------------------------- story


def configure_story(env: dict[str, str]) -> None:
    heading("2/4  Viết truyện bằng Claude")
    print("  Dùng công cụ `claude` (Claude Code) trên máy bạn; tốn usage của tài khoản Claude của bạn.")
    found = shutil.which("claude")
    if found:
        print(f"  Tìm thấy: {found}")
    else:
        print("  Không tìm thấy `claude` trong PATH.")
    if not ask_yes("Bật viết truyện bằng Claude?", default=bool(found)):
        env["STORYFLOW_STORY_RUNNER"] = "none"
        return
    if not found:
        path = ask("Đường dẫn tới claude.exe (bỏ trống = bỏ qua)")
        if not path or not Path(path).is_file():
            print("  ! Không hợp lệ, tắt phần truyện. Cài Claude Code rồi chạy lại setup.")
            env["STORYFLOW_STORY_RUNNER"] = "none"
            return
        env["STORYFLOW_CLAUDE_CLI"] = path
    env["STORYFLOW_STORY_RUNNER"] = "claude-cli"
    env["STORYFLOW_STORY_TIMEOUT"] = "1800"  # long stories (>= reference length) need more than the 900 s default
    print("  Lưu ý: nếu chưa đăng nhập, mở cmd gõ `claude` một lần để đăng nhập.")


# ---------------------------------------------------------------------------- tts


def _vieneu_candidates() -> list[Path]:
    home = Path.home()
    cands = [Path(p) for p in (os.environ.get("STORYFLOW_VIENEU_ROOT"),) if p]
    cands += [home / "Apps" / "VieNeu-TTS", home / "VieNeu-TTS", ROOT.parent / "VieNeu-TTS"]
    state = home / ".claude" / "skills" / "gen-audio-queue" / "state.json"
    try:
        cands.append(Path(json.loads(state.read_text(encoding="utf-8"))["vieneu_root"]))
    except Exception:
        pass
    return cands


def _vieneu_python(root: Path) -> Path:
    return root / ".venv" / "Scripts" / "python.exe"


def _list_voices(py: Path) -> list[str]:
    code = ("import json;from vieneu import Vieneu;"
            "print('VOICES:'+json.dumps([v[1] for v in Vieneu().list_preset_voices()],ensure_ascii=False))")
    try:
        out = subprocess.run([str(py), "-c", code], capture_output=True, timeout=300,
                             env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    except Exception:
        return []
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("VOICES:"):
            try:
                return list(json.loads(line[7:]))
            except ValueError:
                return []
    return []


def configure_tts(env: dict[str, str]) -> None:
    heading("3/4  Đọc truyện thành audio (VieNeu-TTS)")
    print("  Cần bản VieNeu-TTS cài sẵn trên máy (thư mục có .venv riêng).")
    root = next((c for c in _vieneu_candidates() if _vieneu_python(c).is_file()), None)
    if root:
        print(f"  Tìm thấy: {root}")
    else:
        print("  Không tự tìm thấy VieNeu-TTS.")
    if not ask_yes("Bật đọc audio bằng VieNeu-TTS?", default=bool(root)):
        env["STORYFLOW_TTS_ENGINE"] = "none"
        return
    while True:
        if root is None or (not YES and not ask_yes(f"Dùng thư mục {root}?")):
            typed = ask("Đường dẫn thư mục VieNeu-TTS (bỏ trống = bỏ qua)")
            if not typed:
                env["STORYFLOW_TTS_ENGINE"] = "none"
                return
            root = Path(typed.strip('"'))
        if _vieneu_python(root).is_file():
            break
        print(f"  ! Không thấy {_vieneu_python(root)}. Thử lại.")
        root = None
        if YES:
            env["STORYFLOW_TTS_ENGINE"] = "none"
            return
    env["STORYFLOW_TTS_ENGINE"] = "vieneu"
    env["STORYFLOW_VIENEU_ROOT"] = str(root)

    print("  Đang lấy danh sách giọng (vài giây)...")
    voices = _list_voices(_vieneu_python(root))
    default_voice = "Ngọc Huyền"
    if voices:
        for i, v in enumerate(voices, 1):
            print(f"    {i:>2}. {v}")
        if default_voice not in voices:
            default_voice = voices[0]
        pick = ask("Chọn giọng (số hoặc tên)", default_voice)
        if pick.isdigit() and 1 <= int(pick) <= len(voices):
            pick = voices[int(pick) - 1]
        if pick not in voices:
            print(f"  ! '{pick}' không có trong danh sách, dùng '{default_voice}'.")
            pick = default_voice
        env["STORYFLOW_VIENEU_VOICE"] = pick
    else:
        print("  Không lấy được danh sách giọng.")
        env["STORYFLOW_VIENEU_VOICE"] = ask("Nhập tên giọng", default_voice)

    print("  fp32 = chất lượng cao hơn, chậm hơn; int8 = nhanh hơn, nhẹ hơn.")
    prec = ask("Độ chính xác (fp32/int8)", "fp32").lower()
    env["STORYFLOW_VIENEU_PRECISION"] = prec if prec in ("fp32", "int8") else "fp32"
    cpu = os.cpu_count() or 4
    threads = ask("Số luồng CPU", str(min(6, cpu)))
    env["STORYFLOW_VIENEU_THREADS"] = threads if threads.isdigit() and int(threads) > 0 else str(min(6, cpu))


# ---------------------------------------------------------------------------- main


def write_env(env: dict[str, str]) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if ENV_FILE.exists():
        backup = ENV_FILE.parent / ".env.bak"
        shutil.copyfile(ENV_FILE, backup)
        print(f"  (đã sao lưu file cũ -> {backup.name})")
    lines = ["# Generated by scripts/configure_providers.py - safe to edit. Re-run setup to regenerate."]
    lines += [f"{k}={v}" for k, v in env.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    if not VENV_PY.is_file():
        print("Chưa có backend\\.venv - hãy chạy scripts\\setup.bat đầy đủ trước.")
        return 1
    print("\n>>> Cấu hình các dịch vụ thật cho StoryFlow (Enter = chấp nhận giá trị trong [ ])")
    if ENV_FILE.exists() and not YES:
        print(f"\nĐã có cấu hình: {ENV_FILE}")
        if not ask_yes("Cấu hình lại từ đầu? (file cũ sẽ được sao lưu)", default=False):
            print("Giữ nguyên cấu hình hiện tại.")
            return 0
    env: dict[str, str] = {}
    configure_subtitles(env)
    configure_story(env)
    configure_tts(env)
    configure_channels(env)
    write_env(env)
    print(f"\nĐã ghi {ENV_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
