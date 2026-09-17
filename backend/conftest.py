import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Голосовой контур грузит модели ~5 секунд на каждый старт приложения.
# Тесты каналов проверяют протокол, а не голос; голос — в test_voice_pipeline.py.
import os

os.environ.setdefault("VOICE_ENABLED", "false")
