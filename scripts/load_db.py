#!/usr/bin/env python3
"""Проверка локальной пропускной способности записи PostgreSQL."""

import argparse
import asyncio
import json
import math
import os
import statistics
import time
import urllib.parse
from uuid import uuid4

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
    if not 1 <= args.concurrency <= 100:
        raise ValueError("concurrency должно быть от 1 до 100")
    if args.workload == "temporary" and args.concurrency != 1:
        raise ValueError("temporary workload поддерживает только concurrency=1")
    database_url = local_database_url(args.database_url)
    worker_count = min(args.concurrency, args.operations)
    pool = (
        await asyncpg.create_pool(
            database_url, min_size=worker_count, max_size=worker_count,
            command_timeout=30,
        )
        if args.workload == "audit" and worker_count > 1 else None
    )
    connection = await asyncpg.connect(database_url, timeout=10) if pool is None else None
    latencies: list[float] = []
    run_marker = None
    cleanup_rows = 0
    try:
        if args.workload == "temporary":
            assert connection is not None
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
        else:
            if not args.allow_audit_writes:
                raise ValueError("для workload=audit требуется явный --allow-audit-writes")
            assert pool is not None or connection is not None
            fetchval = pool.fetchval if pool is not None else connection.fetchval
            if await fetchval("SELECT to_regclass('public.audit_log')") is None:
                raise ValueError("таблица public.audit_log отсутствует; сначала примените миграции")
            # This opt-in mode measures durable product-table writes. Every
            # INSERT autocommits and exercises the real audit index; exact
            # run-scoped rows are removed in finally, even after a partial run.
            run_marker = f"load-benchmark:{uuid4().hex}"
            started = time.perf_counter()

            async def write_partition(worker_index: int) -> None:
                if pool is None:
                    assert connection is not None
                    worker_connection = connection
                    for sequence_no in range(worker_index, args.operations, worker_count):
                        operation_started = time.perf_counter()
                        await worker_connection.execute(
                            """INSERT INTO public.audit_log(id, actor, role, action, object_id, detail)
                               VALUES($1, $2, 'system', 'benchmark.write', $3, $4)""",
                            uuid4(), "load-benchmark", run_marker, f"sequence={sequence_no}",
                        )
                        latencies.append((time.perf_counter() - operation_started) * 1000)
                else:
                    async with pool.acquire() as worker_connection:
                        for sequence_no in range(worker_index, args.operations, worker_count):
                            operation_started = time.perf_counter()
                            await worker_connection.execute(
                                """INSERT INTO public.audit_log(id, actor, role, action, object_id, detail)
                                   VALUES($1, $2, 'system', 'benchmark.write', $3, $4)""",
                                uuid4(), "load-benchmark", run_marker, f"sequence={sequence_no}",
                            )
                            latencies.append((time.perf_counter() - operation_started) * 1000)

            await asyncio.gather(*(write_partition(worker) for worker in range(worker_count)))
            elapsed = time.perf_counter() - started
            count = await fetchval(
                "SELECT count(*) FROM public.audit_log WHERE object_id = $1", run_marker
            )
    finally:
        try:
            if run_marker is not None:
                if pool is not None:
                    cleanup_rows = await pool.fetchval(
                        "WITH removed AS (DELETE FROM public.audit_log WHERE object_id = $1 RETURNING 1) "
                        "SELECT count(*) FROM removed", run_marker,
                    )
                elif connection is not None:
                    cleanup_rows = await connection.fetchval(
                        "WITH removed AS (DELETE FROM public.audit_log WHERE object_id = $1 RETURNING 1) "
                        "SELECT count(*) FROM removed", run_marker,
                    )
        finally:
            if pool is not None:
                await pool.close()
            elif connection is not None:
                await connection.close()

    throughput = args.operations / elapsed
    result = {
        "database": "local PostgreSQL",
        "workload": args.workload,
        "operations_requested": args.operations,
        "operations_written": count,
        "cleanup_rows_deleted": cleanup_rows,
        "concurrency": worker_count,
        "elapsed_seconds": elapsed,
        "operations_per_second": throughput,
        "operation_latency_ms": {
            "min": min(latencies),
            "median": statistics.median(latencies),
            "p95": percentile(latencies, 95),
            "max": max(latencies),
        },
        "pass_100_writes_per_second": (
            count == args.operations
            and throughput >= 100
            and (args.workload != "audit" or cleanup_rows == args.operations)
        ),
        "scope": (
            f"autocommitted INSERTs over {worker_count} local asyncpg connection(s) into the "
            "indexed product audit_log table; this run's rows are deleted in finally; "
            "not target 6-core/32-GB hardware"
            if args.workload == "audit" else
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
    parser.add_argument("--concurrency", type=int, default=1,
                        help="number of concurrent PostgreSQL clients (audit workload only)")
    parser.add_argument(
        "--workload", choices=("temporary", "audit"), default="temporary",
        help="temporary benchmark table (default) or real indexed audit_log writes",
    )
    parser.add_argument(
        "--allow-audit-writes", action="store_true",
        help="required opt-in: workload=audit inserts then removes rows in audit_log",
    )
    parser.add_argument("--output", help="сохранить JSON-отчёт")
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except Exception as exc:
        print(f"database load failed: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
