#!/usr/bin/env python3
"""Export isolated Clarp telemetry to Parquet for DuckDB analysis.

Run with: uv run --with duckdb scripts/export_telemetry.py --output telemetry.parquet
"""
from __future__ import annotations

import argparse
import pathlib


def quoted(path: pathlib.Path) -> str:
    return str(path.resolve()).replace("'", "''")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database", type=pathlib.Path,
        default=pathlib.Path.home() / ".local/share/clarp/telemetry.sqlite")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        import duckdb
    except ImportError as exc:
        raise SystemExit(
            "DuckDB is optional; run this through `uv run --with duckdb ...`") from exc
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute("INSTALL sqlite; LOAD sqlite")
    connection.execute(
        f"ATTACH '{quoted(args.database)}' AS telemetry (TYPE sqlite)")
    connection.execute(
        f"COPY (SELECT * FROM telemetry.diagnostic_events ORDER BY ts,event_id) "
        f"TO '{quoted(output)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    hourly = output.with_name(f"{output.stem}-hourly.parquet")
    buckets = output.with_name(f"{output.stem}-latency-buckets.parquet")
    connection.execute(
        f"COPY (SELECT * FROM telemetry.hourly_metrics ORDER BY bucket_ms) "
        f"TO '{quoted(hourly)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    connection.execute(
        f"COPY (SELECT * FROM telemetry.hourly_latency_buckets "
        f"ORDER BY bucket_ms,upper_ms) TO '{quoted(buckets)}' "
        f"(FORMAT PARQUET, COMPRESSION ZSTD)")
    rows = connection.execute(
        "SELECT count(*) FROM telemetry.diagnostic_events").fetchone()[0]
    print(
        f"exported_rows={rows} output={output} hourly={hourly} "
        f"latency_buckets={buckets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
