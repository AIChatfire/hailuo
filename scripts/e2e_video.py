#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频链路**真实端到端**实测（🔴 **会计费**）。

```
<venv>/bin/python scripts/e2e_video.py --mode pair --resolution 512p --duration 6
<venv>/bin/python scripts/e2e_video.py --mode t2v      # 文生（25 积分）
<venv>/bin/python scripts/e2e_video.py --mode first    # 仅首帧（15 积分）
```

## 为什么这是一个脚本而不是一段 curl

它把"一次真实视频任务"的全部判据收在一处，**每次都要重新证明**：

1. `content[]` 本地构造（首帧/尾帧用 Pillow 现画 ⇒ **不依赖任何外部图床**）；
2. 受理只回 `id`；`id` 形态是 `cgt-…`；
3. 轮询到终态，并把**完整 Seedance 响应**打出来；
4. `degradations` 逐条打印（**这是本项目最该看的输出**）；
5. 下载产物并校验它**是一个真的 MP4**（ftyp box），不是一段 JSON 错误页。

## 成本（上游计价表实读值，**发之前请再看一眼**）

| mode | 默认模型 | 档位 | 积分 |
|---|---|---|---|
| `pair`（首帧+尾帧） | `23210` | 512p / 6s | **12** |
| `first`（仅首帧） | `23218` | 768p / 6s | **15** |
| `t2v`（文生） | `23204` | 768p / 6s | **25** |
| `veo`（对照） | `veo3.1-i2v` | 1080p / 8s | **120** |

⚠️ 建任务是计费动作，**上游失败也可能扣费**。默认取最省的 `pair/512p/6s`。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 本机自测**必须**绕开代理：`HTTP_PROXY` 会静默吞掉带 Authorization 的 POST
#: （服务器两侧零日志、请求挂起），是本地联调最阴的一类故障。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _frame_png(top: tuple[int, int, int], bottom: tuple[int, int, int],
               size: tuple[int, int] = (1280, 720)) -> bytes:
    """画一张可辨识的渐变帧（首帧/尾帧要**肉眼可分**，产物才可验证）。"""
    from PIL import Image, ImageDraw

    w, h = size
    img = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        k = y / max(1, h - 1)
        draw.line([(0, y), (w, y)],
                  fill=tuple(int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))
    #: 中间画一个方块，让"首尾帧是否真的被用作首尾"有可辨识的锚点
    draw.rectangle([w // 2 - 90, h // 2 - 90, w // 2 + 90, h // 2 + 90],
                   fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _data_url(payload: bytes, mime: str) -> str:
    """🔴 `mime` 必须与字节**真实格式**一致：服务端按魔数嗅探，声明不符会直接 400
    （"不静默改判"）—— 第一次就踩了这个：JPEG 字节标了 `image/png`。"""
    return f"data:{mime};base64," + base64.b64encode(payload).decode()


def _photo_pair(size: tuple[int, int] = (1280, 720),
                top: str = "/tmp/p1.jpg",
                bottom: str = "/tmp/p2.jpg") -> tuple[tuple[bytes, str], tuple[bytes, str]]:
    """**真实照片**帧对：同一个场景的"微推近"（zoom in），尾帧 = 轻微放大 + 提亮。

    为什么要用真照片而不是合成渐变：合成图近乎无纹理，上游的前置校验
    （或模型本身）可能直接判"内容出错"（实测 `code 2400001`）——
    那会把"我的请求构造错了"和"我喂的图不是正常图"混成同一个现象。
    换成真照片对，才能让**结论与图内容无关**。
    """
    from PIL import Image, ImageEnhance

    a = Image.open(top).convert("RGB").resize(size, Image.LANCZOS)
    w, h = size
    zoom = 0.86
    box = (int(w * (1 - zoom) / 2), int(h * (1 - zoom) / 2),
           int(w * (1 + zoom) / 2), int(h * (1 + zoom) / 2))
    b = a.crop(box).resize(size, Image.LANCZOS)
    b = ImageEnhance.Brightness(b).enhance(1.08)

    out: list[tuple[bytes, str]] = []
    for img in (a, b):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        out.append((buf.getvalue(), "image/jpeg"))
    return out[0], out[1]


def _post(url: str, body: dict, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with _OPENER.open(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _get(url: str) -> tuple[int, dict]:
    with _OPENER.open(url, timeout=60) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _build_content(mode: str, frames: str = "photo") -> list[dict]:
    """`frames="photo"`（默认）用真实照片对；`"synthetic"` 用合成渐变图。

    ⚠️ 合成图近乎无纹理，容易被上游前置校验判成"内容出错"
    （实测 `code 2400001`）⇒ **默认走真照片**，让结论与"图是不是正常图"无关。
    """
    text = ("镜头缓慢推近，画面逐渐变亮，整体保持电影感" if mode == "t2v"
            else "镜头平滑推近（push in），画面逐渐变亮")
    content: list[dict] = [{"type": "text", "text": text}]
    if mode == "t2v":
        return content

    if frames == "synthetic":
        first = _data_url(_frame_png((20, 40, 160), (40, 140, 220)), "image/png")
        last = _data_url(_frame_png((200, 60, 20), (250, 180, 40)), "image/png")
    else:
        (a, am), (b, bm) = _photo_pair()
        first, last = _data_url(a, am), _data_url(b, bm)

    content.append({"type": "image_url", "image_url": {"url": first},
                    "role": "first_frame"})
    if mode == "pair":
        content.append({"type": "image_url", "image_url": {"url": last},
                        "role": "last_frame"})
    return content


DEFAULT_MODEL = {"pair": "hailuo-video", "first": "hailuo-video", "t2v": "hailuo-video",
                 "veo": "veo3.1"}
DEFAULT_RES = {"pair": "512p", "first": "768p", "t2v": "768p", "veo": "1080p"}
EXPECTED_MODEL = {"pair": "23210", "first": "23218", "t2v": "23204", "veo": "veo3.1-i2v"}
EXPECTED_COST = {"pair": 12, "first": 15, "t2v": 25, "veo": 120}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8301")
    ap.add_argument("--token", default=os.environ.get("HAILUO_TOKEN", ""))
    ap.add_argument("--mode", choices=("pair", "first", "t2v", "veo"), default="pair")
    ap.add_argument("--model", default=None)
    ap.add_argument("--resolution", default=None)
    ap.add_argument("--duration", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--out", default="e2e-output/video")
    ap.add_argument("--frames", choices=("photo", "synthetic"), default="photo",
                    help="帧素材来源（默认真实照片；合成图曾被上游判 code 2400001）")
    ap.add_argument("--plan-only", action="store_true",
                    help="**零成本预检**：本地算出真实 modelID / 档位 / 预估积分后退出，不发任何请求")
    args = ap.parse_args()

    #: `none` / `auto` / 空 ⇒ 不传该字段，让服务落到上游默认档
    res_arg = (args.resolution or "").strip()
    if res_arg.lower() in ("none", "auto", "default"):
        res_arg = ""
    dur_arg = args.duration or 0

    #: 🔴 `--plan-only` 在**取 token 之前**：预检是纯本地计算，不需要凭据，
    #: 这样才能在"没配 token 的机器上"先把价格算清楚再决定要不要花钱。
    if not args.token and not args.plan_only:
        print("缺 HAILUO_TOKEN（入口鉴权要 hailuo JWT 透传）", file=sys.stderr)
        return 2

    model_arg = args.model or DEFAULT_MODEL[args.mode]
    body: dict = {"model": model_arg, "content": _build_content(args.mode, args.frames)}
    if dur_arg:
        body["duration"] = dur_arg
    if res_arg:
        body["resolution"] = res_arg

    #: ---- 零成本预检：本地 builder 算出真实 modelID / 档位 / 预估积分 ----
    if args.plan_only:
        from app.config import Settings  # noqa: PLC0415
        from app.video_service import VideoService  # noqa: PLC0415

        svc = VideoService(Settings(task_db="sqlite+pysqlite:///:memory:"),
                           fetch_capabilities=False)
        try:
            plan, cap, upstream = svc.build_video_plan(body)
        except Exception as e:  # noqa: BLE001
            print(f"❌ {model_arg} ({args.mode}): {type(e).__name__}: {str(e)[:180]}")
            return 3
        print(json.dumps({
            "请求 model": model_arg, "形态": args.mode, "上游 modelID": upstream,
            "能力名": cap, "resolution": plan.resolution, "duration": plan.duration,
            "ratio": plan.aspect_ratio, "预估积分": plan.forecast_credits,
            "degradations": plan.degradations,
        }, ensure_ascii=False, indent=1))
        return 0

    print(f"🔴 即将**真实建任务**：mode={args.mode} model={model_arg} "
          f"（计划模型={EXPECTED_MODEL.get(args.mode)} 参考价={EXPECTED_COST.get(args.mode)} 积分）")
    shape = [c["type"] + (":" + c["role"] if c.get("role") else "")
             for c in body["content"]]
    print(f"请求：{json.dumps({k: v for k, v in body.items() if k != 'content'}, ensure_ascii=False)}"
          f" content={shape}")

    url = args.base_url.rstrip("/") + "/api/v3/contents/generations/tasks"
    code, created = _post(url, body, args.token)
    print(f"\n① 受理 HTTP {code}")
    print(json.dumps(created, ensure_ascii=False, indent=2))
    if code != 200 or "id" not in created:
        return 1
    task_id = created["id"]
    assert set(created) == {"id"}, "受理响应必须只有 id"

    started = time.time()
    last = ""
    while time.time() - started < args.timeout:
        time.sleep(5)
        _c, task = _get(f"{url}/{task_id}")
        line = (f"   {task.get('status'):10} {int(time.time() - started):4}s "
                f"elapsed={task.get('duration')}s res={task.get('resolution')}")
        if line != last:
            print(line)
            last = line
        if task.get("status") in ("succeeded", "failed", "expired", "cancelled"):
            break

    print(f"\n② 终态响应（{int(time.time() - started)}s）")
    print(json.dumps(task, ensure_ascii=False, indent=2))

    if task.get("degradations"):
        print("\n③ degradations（**每一次'请求了 A 实际做了 B'都在这里**）")
        for note in task["degradations"]:
            print(f"   - {note}")

    if task.get("status") != "succeeded":
        print("\n❌ 未成功；上游失败也可能已扣费。", file=sys.stderr)
        return 1

    video_url = (task.get("content") or {}).get("video_url") or ""
    print(f"\n④ 产物 URL：{video_url[:120]}…")
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{args.mode}-{task_id}.mp4")
    with _OPENER.open(video_url, timeout=180) as resp, open(path, "wb") as fh:
        fh.write(resp.read())
    size = os.path.getsize(path)
    head = open(path, "rb").read(16)
    is_mp4 = b"ftyp" in head
    print(f"   已下载 {path}（{size / 1e6:.2f} MB）| 真 MP4（ftyp box）：{is_mp4}")

    print("\n⑤ 实测结论")
    print(f"   路由：mode={args.mode} ⇒ 期望 {EXPECTED_MODEL[args.mode]}"
          f"（真实 modelID 见 /stats 或服务日志的 create_video trace）")
    print(f"   计费：预计 {EXPECTED_COST[args.mode]} 积分")
    print(f"   产物：{'✅ 可下载且是 MP4' if is_mp4 and size > 10_000 else '⚠️ 需人工看'}")
    return 0 if is_mp4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
