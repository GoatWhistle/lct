#!/usr/bin/env python3
"""Проверка последовательной записи PostgreSQL без изменения данных продукта."""

import argparse
import asyncio
import json
import math
import os
import statistics
import time
import urllib.parse

import asyncpg


def local_database_url(value: str) -> str:
    normalized = value.replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("поддерживается только PostgreSQL")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("предохранитель: benchmark разрешён только для локальной БД")
    return normalized


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


async def run(args: argparse.Namespace) -> int:
    if not 100 <= args.operations <= 10_000:
        raise ValueError("operations должно быть от 100 до 10000")
    database_url = local_database_url(args.database_url)
    connection = await asyncpg.connect(database_url, timeout=10)
    latencies: list[float] = []
    try:
        async with connection.transaction():
            await connection.execute("""
                CREATE TEMPORARY TABLE lct_write_benchmark (
                    sequence_no integer PRIMARY KEY,
                    written_at timestamptz NOT NULL DEFAULT now(),
                    payload text NOT NULL
                ) ON COMMIT DROP
            """)
            started = time.perf_counter()
            for sequence_no in range(args.operations):
                operation_started = time.perf_counter()
                await connection.execute(
                    "INSERT INTO lct_write_benchmark(sequence_no, payload) VALUES($1, $2)",
                    sequence_no, f"local-load-{sequence_no:05d}",
                )
                latencies.append((time.perf_counter() - operation_started) * 1000)
            elapsed = time.perf_counter() - started
            count = await connection.fetchval("SELECT count(*) FROM lct_write_benchmark")
    finally:
        await connection.close()

    throughput = args.operations / elapsed
    result = {
        "database": "local PostgreSQL",
        "operations_requested": args.operations,
        "operations_written": count,
        "elapsed_seconds": elapsed,
        "operations_per_second": throughput,
        "operation_latency_ms": {
            "min": min(latencies),
            "median": statistics.median(latencies),
            "p95": percentile(latencies, 95),
            "max": max(latencies),
        },
        "pass_100_writes_per_second": count == args.operations and throughput >= 100,
        "scope": (
            "sequential INSERT statements over one local asyncpg connection into a temporary "
            "table; table is dropped on commit; not target 6-core/32-GB hardware"
        ),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    return 0 if result["pass_100_writes_per_second"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", "postgresql://lct:lct@127.0.0.1:5432/lct"),
    )
    parser.add_argument("--operations", type=int, default=1000)
    parser.add_argument("--output", help="сохранить JSON-отчёт")
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except Exception as exc:
        print(f"database load failed: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
