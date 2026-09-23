"""Запуск двух локальных GGUF-серверов через llama.cpp на Windows/macOS/Linux.

Никаких загрузок при старте: GGUF предварительно кладутся в models/ через
download_local_models.py. Путь к llama-server задаётся LLAMA_SERVER_BIN либо
берётся из PATH. Ctrl+C останавливает дочерние процессы.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "dialogue": ("qwen3-1.7b/Qwen3-1.7B-Q8_0.gguf", "Qwen3-1.7B", 18080),
    "russian_control": ("vikhr-1b/Vikhr-Llama-3.2-1B-Q4_K_M.gguf", "Vikhr-1B", 18081),
}


def binary_path() -> str:
    bundled_mac = ROOT / "models" / "bin" / "llama-b10934" / "llama-server"
    binary = (os.environ.get("LLAMA_SERVER_BIN") or shutil.which("llama-server")
              or shutil.which("llama-server.exe")
              # The checked-out helper is Mach-O arm64. Do not select it on a
              # Windows machine merely because the whole models/ folder was
              # copied there for its platform-neutral GGUF weights.
              or (str(bundled_mac) if sys.platform == "darwin" and bundled_mac.is_file() else None))
    if not binary:
        raise RuntimeError("нет llama-server; установите бинарник llama.cpp и задайте LLAMA_SERVER_BIN")
    if not Path(binary).is_file():
        raise RuntimeError(f"llama-server не найден: {binary}")
    return binary


def command(binary: str, profile: str, threads: int) -> list[str]:
    relative, alias, port = PROFILES[profile]
    model = ROOT / "models" / relative
    if not model.is_file():
        raise RuntimeError(f"нет модели: {model} — запустите make local-models")
    result = [binary, "-m", str(model), "--alias", alias,
              "--host", "127.0.0.1", "--port", str(port),
              "--ctx-size", "2048", "--threads", str(threads), "--parallel", "1",
              "--cors-origins", "localhost"]
    if profile == "dialogue":
        result += ["--reasoning-budget", "0"]
    return result


def ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.HTTPError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["dialogue", "russian_control", "both"], default="dialogue")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads должен быть положительным")
    selected = list(PROFILES) if args.mode == "both" else [args.mode]
    try:
        binary = binary_path()
        commands = [command(binary, profile, args.threads) for profile in selected]
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    processes: list[subprocess.Popen] = []
    try:
        for profile, argv in zip(selected, commands):
            port = PROFILES[profile][2]
            if ready(port):
                raise RuntimeError(f"порт {port} уже занят сервером — не запускаю дубликат")
            print(f"запускаю {profile} на 127.0.0.1:{port}", flush=True)
            # Ctrl+C должен достаться управляющему процессу один раз: он сам
            # остановит дочерний сервер. Иначе llama.cpp получает двойной
            # SIGINT и на Metal иногда падает во время освобождения памяти.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            processes.append(subprocess.Popen(
                argv, cwd=ROOT, creationflags=flags, start_new_session=os.name != "nt"
            ))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("один из серверов модели завершился до готовности")
            if all(ready(PROFILES[profile][2]) for profile in selected):
                print("локальные модели готовы; Ctrl+C остановит их", flush=True)
                while all(process.poll() is None for process in processes):
                    time.sleep(0.5)
                raise RuntimeError("один из серверов модели неожиданно завершился")
            time.sleep(0.5)
        raise RuntimeError("модели не стали готовы за 120 секунд")
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
