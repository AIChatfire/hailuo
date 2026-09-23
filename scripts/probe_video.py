#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频能力表**零计费**探针：实读两个公开端点，与冻结快照逐条对账。

```
<venv>/bin/python scripts/probe_video.py            # 对账 + 打印差异
<venv>/bin/python scripts/probe_video.py --dump     # 附上每个模型的档位与单价
```

🔴 **本脚本永远不建任务、不带 token、不带 yy** —— 它只打
`/public/api/config/web/common_config` 与
`/public/v2/api/multimodal/video/model/info` 两个**免鉴权** GET。
视频单价最高 180 积分一次，任何"顺手验证一下"的真实提交都必须由人显式发起。

用途：
1. 上游加了新模型 / 改了档位价格 ⇒ 这里能看出来，然后手动更新
   `app/upstream/hailuo/video_models.py::FROZEN_VIDEO_SNAPSHOT`；
2. 运行期实读坏了 ⇒ 至少知道冻结快照与现实的差距有多大。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.upstream.hailuo import video_capabilities as vcap  # noqa: E402
from app.upstream.hailuo import video_models as vmodels  # noqa: E402
from app.upstream.hailuo.video_models import VIDEO_FAMILIES  # noqa: E402


def _cost_triples(model: vmodels.UpstreamVideoModel) -> list[tuple]:
    """计价行的**可比形态**：只取 `(resolutions, durations, realCost)`。

    ⚠️ 不能直接比字典 —— 冻结快照里的行是本地归一化的
    `{resolutions, durations, realCost, rawCost}`，而实读保留的是上游原样
    （可能多带 `qualities` / `unitFileCost`）⇒ 逐键比会把"形状不同"报成"价格变了"。
    """
    return sorted(
        (tuple(sorted(str(r) for r in (row.get("resolutions") or []))),
         tuple(sorted(int(d) for d in (row.get("durations") or []))),
         row.get("realCost"))
        for row in model.costs)


def _cmp(a: vmodels.UpstreamVideoModel, b: vmodels.UpstreamVideoModel) -> list[str]:
    diffs: list[str] = []
    if a.durations != b.durations:
        diffs.append(f"durations {b.durations} → {a.durations}")
    if a.resolution_options != b.resolution_options:
        diffs.append(f"resolutions {b.resolution_options} → {a.resolution_options}")
    if a.aspect_ratio_options != b.aspect_ratio_options:
        diffs.append("aspectRatios 变了")
    if a.max_images != b.max_images:
        diffs.append(f"maxImages {b.max_images} → {a.max_images}")
    if a.kind != b.kind:
        diffs.append(f"type {b.kind} → {a.kind}")
    if tuple(a.modes) != tuple(b.modes):
        diffs.append(f"mode {b.modes} → {a.modes}")
    if _cost_triples(a) != _cost_triples(b):
        diffs.append(f"计价 {_cost_triples(b)} → {_cost_triples(a)}")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get(
        "HAILUO_BASE_URL", "https://hailuoai.video"))
    ap.add_argument("--dump", action="store_true", help="打印每个模型的档位与单价")
    args = ap.parse_args()

    #: 🔴 不走环境代理：`trust_env=False` ⇒ 本地 HTTP_PROXY 不会改写路由
    #: （代理会静默吞掉带 Authorization 的请求，而这里虽是免鉴权 GET，
    #: 也绝不该被一个中间人有选择地转出去）
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=20.0,
                      trust_env=False) as c:
        meta = vcap.parse_video_common_config(c.get(vcap.COMMON_CONFIG_PATH).json())
        info = vcap.parse_video_model_info(c.get(vcap.MODEL_INFO_PATH).json())

    live = {m.model_id: m for m in vcap.merge_video(meta, info)}
    snap = {m.model_id: m for m in vmodels.FROZEN_VIDEO_SNAPSHOT}

    print(f"上游实读：{len(live)} 个视频模型 | 冻结快照：{len(snap)} 个")
    print(f"覆盖率：{json.dumps(vcap.coverage_video(list(live.values())), ensure_ascii=False)}")
    print()

    #: 刻意不登记的（见 `VIDEO_DELIBERATE_ABSENCES`）不算"新增"，单列出来
    absent_keys: set[str] = set()
    for key in vmodels.VIDEO_DELIBERATE_ABSENCES:
        absent_keys.update(p.strip() for p in key.split("/") if p.strip())

    added = sorted(set(live) - set(snap))
    deliberate = [m for m in added if m in absent_keys]
    unexpected = [m for m in added if m not in absent_keys]
    gone = sorted(set(snap) - set(live))

    if unexpected:
        print(f"🟡 上游新增（快照里没有，且**未在刻意不登记清单里**）：{unexpected}")
    if deliberate:
        print(f"⚪ 上游存在但本服务**刻意不登记**：{deliberate}")
        print("   （理由见 video_models.VIDEO_DELIBERATE_ABSENCES）")
    if gone:
        print(f"🔴 快照里有、上游没了（**能力名可能指向不存在的模型**）：{gone}")
    if not unexpected and not gone:
        print("✅ 模型清单一致（差别只在刻意不登记的那几个）。")

    print()
    for mid in sorted(set(live) & set(snap)):
        diffs = _cmp(live[mid], snap[mid])
        if diffs:
            print(f"  {mid}: " + "; ".join(diffs))

    print()
    print("—— 族路由自检（每个槽位是否仍在上游表里）——")
    for f in VIDEO_FAMILIES:
        for slot, mid in (("t2v", f.t2v), ("first", f.first), ("pair", f.pair)):
            if not mid:
                continue
            mark = "✅" if mid in live else "❌ 上游已无此模型"
            print(f"  {f.name:20} {slot:6} → {mid:22} {mark}")

    if args.dump:
        print()
        print("—— 明细 ——")
        for mid in sorted(live):
            m = live[mid]
            print(f"{mid:24} {m.family:18} {m.kind:4} "
                  f"res={list(m.resolution_options)} dur={list(m.durations)} "
                  f"ar={len(m.aspect_ratio_options)} maxImg={m.max_images}")
            for row in m.costs:
                print(f"      {row.get('resolutions')} × {row.get('durations')} "
                      f"= {row.get('realCost')} 积分")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
