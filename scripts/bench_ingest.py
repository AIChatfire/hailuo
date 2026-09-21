#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行 ingest 基准 —— 串行 vs 并行（**零计费**）。

只跑输入图流水线：**下载 → 归一化 → OSS 三段上传**，**不建任务**（建任务才计费）。
⇒ 这个基准可以随便跑，不消耗额度。

设计要点：
- 每轮**新建 Service**（全新上传缓存）⇒ md5 缓存不会把后续轮次"变快"；
- 大小/字节互异的三张真实 CDN 图 ⇒ 不会触发上传去重（去重是正确行为，
  但会掩盖并行效果）；
- 交替执行 `INGEST_PARALLELISM=1` 与 `4` 各两轮，取中位数，削弱上游抖动；
- 顺带报告单张耗时（串行轮里直接就是每张的真实耗时）。

用法：`python scripts/bench_ingest.py`（可加 `--rounds 3 --parallelism 4,8`）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: 三条**字节互异**的真实产物 URL（今日实测链路生成，CDN 可直取）
REFS = [
    ("cube-1k",
     "https://cdn.hailuoai.video/moss/prod/2026-09-21-10/user/multi_chat_file/"
     "1789958465020177483-302833867771949058_1789958463.png"),
    ("pyramid-1k",
     "https://cdn.hailuoai.video/moss/prod/2026-09-21-10/user/multi_chat_file/"
     "1789959286880141814-302833867771949058_1789959285.png"),
    ("single-1k",
     "https://cdn.hailuoai.video/moss/prod/2026-09-21-10/user/multi_chat_file/"
     "1789959341236494651-302833867771949058_1789959338.png"),
]


def collect_pool(limit: int) -> list[tuple[str, str]]:
    """图池 = 内置 3 张 + `e2e_matrix.db` 里成功任务的产物 URL（去重）。

    ⚠️ 必须**字节互异**：相同内容会命中上传缓存去重（那是省额度的正确行为，
    但会让并发测试失真 —— 三张同图实际只传一次）。
    """
    pool = list(REFS)
    seen = {u for _, u in pool}
    try:
        conn = sqlite3.connect("e2e_matrix.db")
        rows = conn.execute(
            "SELECT result_json FROM tasks WHERE status='succeeded'").fetchall()
        conn.close()
    except Exception:  # noqa: BLE001 —— 没有 DB 也能跑（只用内置 3 张）
        rows = []
    for (raw,) in rows:
        try:
            data = json.loads(raw or "{}").get("data") or []
        except Exception:  # noqa: BLE001
            continue
        for d in data:
            u = str((d or {}).get("url") or "")
            if not u or u in seen:
                continue
            seen.add(u)
            pool.append((f"matrix-{len(pool)}", u))
            if len(pool) >= limit:
                return pool
    return pool


def fresh_service(*, parallelism: int):
    """每轮新建（全新上传缓存 + 独立 sqlite）。"""
    from app.config import Settings
    from app.service import Service

    tmp = Path(tempfile.mkdtemp(prefix="bench-ingest-"))
    st = Settings.from_env().replace(
        task_db=f"sqlite+pysqlite:///{tmp}/bench.db",
        ingest_parallelism=parallelism,
        coordinator_enabled=False,
    )
    return Service(st, fetch_capabilities=False)


def one_round(parallelism: int, urls: list[str]) -> tuple[float | None, list[float], str]:
    """跑一轮 ingest。返回 `(总耗时|None, [每张耗时], 错误说明)`。单轮失败不中断基准。"""
    svc = fresh_service(parallelism=parallelism)
    per_image: list[float] = []
    try:
        t0 = time.perf_counter()
        if parallelism == 1:
            for u in urls:
                s = time.perf_counter()
                svc._ingest_one(u)  # noqa: SLF001 —— 脚本层直接量流水线
                per_image.append(time.perf_counter() - s)
        else:
            def timed(u: str) -> float:
                s = time.perf_counter()
                svc._ingest_one(u)  # noqa: SLF001
                return time.perf_counter() - s

            with ThreadPoolExecutor(max_workers=parallelism,
                                    thread_name_prefix="bench") as pool:
                per_image = list(pool.map(timed, urls))
        return time.perf_counter() - t0, per_image, ""
    except Exception as e:  # noqa: BLE001
        return None, per_image, f"{type(e).__name__}: {str(e)[:100]}"
    finally:
        svc.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="并行 ingest 基准（零计费）")
    ap.add_argument("--rounds", type=int, default=2, help="每种并行度跑几轮")
    ap.add_argument("--parallelism", default="1,4",
                    help="逗号分隔的并行度（会**交替**执行以削弱上游抖动）")
    ap.add_argument("--images", type=int, default=3,
                    help="用几张输入图（>3 时从 e2e_matrix.db 的成功产物补足；"
                         "想真测高并发就得给够图数）")
    args = ap.parse_args(argv)

    from app.config import Settings  # noqa: PLC0415

    if not Settings.from_env().upstream_configured:
        print("未配置 HAILUO_TOKEN（上传凭据需要它）—— 无法基准。")
        return 2

    pool = collect_pool(args.images)
    urls = [u for _, u in pool]
    levels = [int(x) for x in args.parallelism.split(",") if x.strip()]
    print(f"输入图：{len(urls)} 张（{', '.join(n for n, _ in pool)}）")
    if max(levels) > len(urls):
        print(f"⚠️ 最大并行度 {max(levels)} > 图数 {len(urls)} ⇒ 实际并发被图数钳制到 {len(urls)}")
    print(f"并行度：{levels} × {args.rounds} 轮（交替）\n")

    results: dict[int, list[float]] = {p: [] for p in levels}
    failures: dict[int, int] = {p: 0 for p in levels}
    for r in range(1, args.rounds + 1):
        for p in levels:
            total, per_image, err = one_round(p, urls)
            if total is None:
                failures[p] += 1
                print(f"  轮{r} 并行度={p:<2} ❌ 失败：{err}", flush=True)
                continue
            results[p].append(total)
            detail = " ".join(f"{d:.1f}s" for d in per_image)
            print(f"  轮{r} 并行度={p:<2} 总耗时 {total:6.2f}s   [每张 {detail}]",
                  flush=True)

    print("\n" + "=" * 60)
    medians = {p: (statistics.median(v) if v else None) for p, v in results.items()}
    base = medians[levels[0]]
    for p in levels:
        med = medians[p]
        if med is None:
            print(f"  并行度={p:<3} 全部失败（{failures[p]} 轮）")
            continue
        speed = ("—" if p == levels[0] or not base
                 else f"快 {base / med:.2f}×")
        tail = f"  失败 {failures[p]} 轮" if failures[p] else ""
        print(f"  并行度={p:<3} 中位数 {med:6.2f}s   {speed}{tail}")
    print("=" * 60)
    print("ℹ️ 零计费：只测 ingest（下载+上传），未建任何任务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
