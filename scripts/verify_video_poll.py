#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**零计费**复核：拿一个真实上游批次，重放本服务的轮询收口。

```
<venv>/bin/python scripts/verify_video_poll.py --batch 558881070296965128 \
                                               --record 558881070888361993
```

## 为什么需要它

轮询路径的形态**不能靠读代码判断对错**（我们就是这么错过一次：照参考实现只走 v4，
结果上游 2 分钟出片、我们轮询 15 分钟没看见、最后被看门狗判 `expired` ——
**钱花了、片出来了、没拿到**）。这个脚本把"已存在的真实批次"灌进本地任务表，
然后**调用本服务真正的轮询代码**，看它能否把任务收口到 `succeeded` 并取出 URL。

🔴 它只发**查询**请求（`my/batch` + v4 点名），**不建任何任务、不计费**。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.upstream.hailuo import upload as up_mod  # noqa: E402
from app.upstream.hailuo.client import HailuoClient  # noqa: E402
from app.video_service import VideoService, video_view  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True, help="上游 batchID（建任务响应的 data.task.batchID）")
    ap.add_argument("--record", default="", help="上游记录 id（建任务响应的 data.id）")
    ap.add_argument("--token", default=os.environ.get("HAILUO_TOKEN", ""))
    ap.add_argument("--base-url", default="https://hailuoai.video")
    args = ap.parse_args()

    if not args.token:
        print("缺 HAILUO_TOKEN", file=sys.stderr)
        return 2

    st = Settings(task_db=f"sqlite+pysqlite:///{tempfile.mkdtemp()}/verify.db")
    client = HailuoClient(token=args.token, base_url=args.base_url,
                          device=st.device_profile())
    uploader = up_mod.Uploader(client=client, cache_ttl=1.0)
    svc = VideoService(st, client=client, uploader=uploader, fetch_capabilities=False)

    tid = svc.store.create_task(
        task_id="cgt-verify-000000000000", credential_id="verify",
        capability="hailuo-video", model="hailuo-video", upstream_model="23210",
        status="in_progress", request_json="{}",
        plan_json=json.dumps({"quantity": 1, "duration": 6, "resolution": "768",
                              "capability": "hailuo-video"}),
        degradations_json="[]",
    )["task_id"]
    svc.store.update_task(tid, upstream_batch_id=args.batch,
                          upstream_feed_id=args.record)

    tasks = svc.store.in_flight()
    print(f"本地任务：{tid}（batch={args.batch} record={args.record or '（未给）'}）")
    res = svc.poll_many(tasks)
    brief = json.dumps({k: v for k, v in res.items() if k != "trace"},
                       ensure_ascii=False)
    print(f"poll_many -> {brief}")
    full = svc.store.get_full(tid)
    print(f"\n本地状态：{full['status']}  upstream_status={full['upstream_status']}")
    result = full.get("result") or {}

    code, payload = video_view(full)
    print(f"\n=== 对外的 Seedance 响应（HTTP {code}）===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if full["status"] == "succeeded":
        print(f"\n✅ 收口成功：{result.get('video_url')}")
        print(f"   尺寸 {result.get('width')}×{result.get('height')} "
              f"| 时长 {result.get('duration_ms')}ms")
        print(f"   去水印 {result.get('url_no_watermark')}")
        return 0
    print(f"\n❌ 未收口：{json.dumps(full.get('error') or {}, ensure_ascii=False)}")
    print("   ⇒ 说明轮询仍未命中该批次；请检查 my/batch 的 feedTypes 与产物路径")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
