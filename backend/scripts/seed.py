"""make seed: сценарии из /scenarios в БД."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import get_sessionmaker  # noqa: E402
from app.scenarios import store  # noqa: E402
from app.scenarios.loader import ScenarioError  # noqa: E402

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


async def main() -> None:
    try:
        scenarios = store.load_from_disk(LIBRARY)
    except ScenarioError as exc:
        print(f"библиотека не прошла проверку: {exc}")
        raise SystemExit(1) from exc

    async with get_sessionmaker()() as db:
        count = await store.seed(db, scenarios)
    print(f"залито сценариев: {count}")


if __name__ == "__main__":
    asyncio.run(main())
