from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path
import yaml
from infra.storage.db import get_connection
from infra.storage.examples_pending_store import ExamplesPendingStore


async def promote_one(store: ExamplesPendingStore, pending_id: int,
                      yaml_path: Path, *, approved_bucket: str,
                      why_override: str | None = None) -> None:
    pending = await _find_pending(store, pending_id)
    if pending is None:
        raise SystemExit(f"pending id {pending_id} not found or already resolved")

    raw = yaml.safe_load(yaml_path.read_text())
    raw.setdefault("conviction_examples", []).append({
        "msg": pending["msg_text"],
        "bucket": approved_bucket,
        "why": why_override or pending.get("proposed_why") or "",
    })
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    await store.resolve(pending_id, status="approved", resolved_bucket=approved_bucket)


async def _find_pending(store: ExamplesPendingStore, pending_id: int) -> dict | None:
    rows = await store.list_pending()
    for r in rows:
        if r["id"] == pending_id:
            return r
    return None


async def _list(store: ExamplesPendingStore, trader: str | None) -> None:
    rows = await store.list_pending(trader_handle=trader)
    for r in rows:
        print(f"[{r['id']}] {r['trader_handle']}  bucket={r['proposed_bucket']}  "
              f"src={r['source']}  why={r['proposed_why']!r}")
        print(f"     msg: {r['msg_text'][:120]!r}")


async def _async_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="agent.db")
    parser.add_argument("--traders-dir", default="config/traders")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--trader")

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("--id", type=int, required=True)
    p_approve.add_argument("--bucket", required=True, choices=["HIGH", "LOW", "SKIP"])
    p_approve.add_argument("--why")

    p_reject = sub.add_parser("reject")
    p_reject.add_argument("--id", type=int, required=True)

    args = parser.parse_args(argv)
    conn = await get_connection(args.db)
    try:
        store = ExamplesPendingStore(conn)
        if args.cmd == "list":
            await _list(store, args.trader)
            return 0
        if args.cmd == "approve":
            pending = await _find_pending(store, args.id)
            if pending is None:
                print(f"pending id {args.id} not found", file=sys.stderr)
                return 2
            yaml_path = Path(args.traders_dir) / f"{pending['trader_handle']}.yaml"
            if not yaml_path.exists():
                print(f"profile yaml missing: {yaml_path}", file=sys.stderr)
                return 2
            await promote_one(store, args.id, yaml_path,
                              approved_bucket=args.bucket, why_override=args.why)
            print(f"promoted id={args.id} → {pending['trader_handle']} as {args.bucket}")
            return 0
        if args.cmd == "reject":
            await store.resolve(args.id, status="rejected", resolved_bucket=None)
            print(f"rejected id={args.id}")
            return 0
        return 1
    finally:
        await conn.close()


def main() -> int:
    return asyncio.run(_async_main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
