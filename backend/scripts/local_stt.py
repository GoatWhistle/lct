"""Локальный whisper.cpp server для Whisper small (macOS / Windows / Linux).

Сначала скачайте GGML-вес через download_local_models.py. Исполняемый файл
задаётся WHISPER_SERVER_BIN или ищется в PATH / локальной сборке. Сеть не нужна.
"""

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "whisper-small" / "ggml-small-q5_1.bin"
PORT = 18082


def binary_path() -> str:
    built = ROOT / "models" / "bin" / "whisper.cpp-1.9.4" / "build" / "bin" / "whisper-server"
    binary = (os.environ.get("WHISPER_SERVER_BIN") or shutil.which("whisper-server")
              or shutil.which("whisper-server.exe")
              # The in-tree build is macOS arm64, not a portable executable.
              or (str(built) if sys.platform == "darwin" and built.is_file() else None))
    if not binary or not Path(binary).is_file():
        raise RuntimeError("нет whisper-server; установите бинарник whisper.cpp и задайте WHISPER_SERVER_BIN")
    return binary


def command(binary: str, threads: int = 4) -> list[str]:
    if not MODEL.is_file():
        raise RuntimeError(f"нет модели: {MODEL} — запустите make local-models")
    return [binary, "-m", str(MODEL), "--host", "127.0.0.1", "--port", str(PORT),
            "--language", "ru", "--threads", str(threads), "--no-fallback"]


def ready() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.HTTPError):
        return False


def main() -> int:
    try:
        if ready():
            raise RuntimeError(f"порт {PORT} уже занят сервером")
        argv = command(binary_path())
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(argv, cwd=ROOT, creationflags=flags, start_new_session=os.name != "nt")
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("whisper-server завершился до готовности")
            if ready():
                print(f"Whisper small готов на 127.0.0.1:{PORT}; Ctrl+C остановит сервер", flush=True)
                while process.poll() is None:
                    time.sleep(0.5)
                raise RuntimeError("whisper-server неожиданно завершился")
            time.sleep(0.5)
        raise RuntimeError("Whisper small не стал готов за 120 секунд")
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
