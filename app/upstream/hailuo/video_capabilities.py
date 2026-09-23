#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行期读**视频**能力表 —— 零成本、免鉴权、不建任务。

与图片链路用的是**同一个 GET 端点**（`capabilities.py` 各自打开了那一半），
这里取的是响应里的**另外两个键**：

| 端点 | 取哪个键 | 给什么 |
|---|---|---|
| `GET /public/api/config/web/common_config` | `create_video_models` | 模型清单、`type`、`mode`、`maxSupportImageCount`、`maxPromptLength`、`endFrameRequiredStartFrame`、`disablePromptOptimization`、所属族 |
| `GET /public/v2/api/multimodal/video/model/info` | `videoModels` | `resolutions[]` / `durations[]` / `aspectRatios[]`（含 `defaultSelect`）、`costs[]`、`addition.supportFrame` |

⚠️ 两个端点都带"这条的一类"，但**互相都不能替代**：前者没有档位与价格，
后者没有"能垫几张图/是不是文生这类"的语义。

读不到 ⇒ **不静默**：退回 `video_models.FROZEN_VIDEO_SNAPSHOT`，并把原因写进降级说明。
"""
from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from .video_models import UpstreamVideoModel

#: 公开端点（无鉴权、GET）。路径与图片侧同源。
COMMON_CONFIG_PATH = "/public/api/config/web/common_config"
MODEL_INFO_PATH = "/public/v2/api/multimodal/video/model/info"

#: ffmpeg 之外的兜底时长。上游没给 duration 时用它的 defaultSelect；
#: 连 defaultSelect 都没有时才走这里（video_models.snap_duration 会说明"未校验"）。
FALLBACK_DURATION = 6


def _enum(param: dict[str, Any], key: str, *, cast=str):
    items = param.get(key) or []
    values = tuple(cast(r["value"]) for r in items if r.get("value") is not None)
    defaults = tuple(cast(r["value"]) for r in items if r.get("defaultSelect"))
    return values, defaults


def parse_video_common_config(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`create_video_models` → `model_id -> 语义类字段`。"""
    out: dict[str, dict[str, Any]] = {}
    conf = ((payload or {}).get("data") or {}).get("create_video_models") or {}
    for group in conf.get("models") or []:
        group_key = str(group.get("modelKey") or "")
        #: 🔴 **组级** `filterTags` 里的 `end_frame` 才是"首尾帧"的第一手声明：
        #: `Hailuo 2.0` / `MiniMax H3*` / `Veo 3.1*` / `Sora 2` 有；
        #: `Hailuo 2.3` / `2.3-Fast` / `1.0*` 与 seedance 各支**没有** ⇒ 只能收首帧。
        group_end_frame = "end_frame" in (group.get("filterTags") or [])
        for item in group.get("modelList") or []:
            mid = item.get("id")
            if not mid:
                continue
            mode = item.get("mode")
            modes = tuple(mode) if isinstance(mode, list) else ((mode,) if mode else ())
            out[str(mid)] = {
                "family": group_key or str(mid),
                "kind": str(item.get("type") or ""),
                "modes": modes,
                "max_images": item.get("maxSupportImageCount"),
                "max_prompt": item.get("maxPromptLength"),
                "declared_end_frame": group_end_frame,
                "end_frame_requires_start": (
                    bool(item["endFrameRequiredStartFrame"])
                    if item.get("endFrameRequiredStartFrame") is not None else None),
                "disable_prompt_optimization": (
                    bool(item["disablePromptOptimization"])
                    if item.get("disablePromptOptimization") is not None else None),
            }
    return out


def parse_video_model_info(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`videoModels` → `model_id -> 档位与价格`。"""
    out: dict[str, dict[str, Any]] = {}
    data = (payload or {}).get("data") or {}
    for item in data.get("videoModels") or []:
        mid = item.get("modelID")
        if not mid:
            continue
        param = item.get("parameter") or {}
        resolutions, res_defaults = _enum(param, "resolutions")
        durations, dur_defaults = _enum(param, "durations", cast=int)
        ratios, ratio_defaults = _enum(param, "aspectRatios")
        #: 🔴 `supportFrame` 是**按档位**声明的，必须逐档抠出来：
        #: `23210` 的 768/1080 是 True，而 512 一个字都没声明 ——
        #: 那就是"512 跑首尾帧会 `code 2400001`"的全部线索（2026-09-23 实测）。
        frame_ok: list[str] = []
        frame_no: list[str] = []
        for r in param.get("resolutions") or []:
            if r.get("value") is None:
                continue
            sf = (r.get("addition") or {}).get("supportFrame")
            if sf is True:
                frame_ok.append(str(r["value"]))
            elif sf is False:
                frame_no.append(str(r["value"]))
        out[str(mid)] = {
            "resolution_options": resolutions,
            "default_resolutions": res_defaults,
            "frame_resolutions": tuple(frame_ok),
            "non_frame_resolutions": tuple(frame_no),
            "durations": durations,
            "default_durations": dur_defaults,
            "aspect_ratio_options": ratios,
            "default_aspect_ratios": ratio_defaults,
            "support_frame": (item.get("addition") or {}).get("supportFrame"),
            "costs": tuple(item.get("costs") or ()),
            "default_cost": item.get("defaultCost"),
        }
    return out


def merge_video(
    meta: dict[str, dict[str, Any]], info: dict[str, dict[str, Any]]
) -> list[UpstreamVideoModel]:
    """两份数据按 `model_id` 合并。**缺一半也保留** —— 少一半信息好过整条丢掉。"""
    merged: list[UpstreamVideoModel] = []
    for mid in sorted(set(meta) | set(info)):
        m = meta.get(mid) or {}
        i = info.get(mid) or {}
        merged.append(UpstreamVideoModel(
            model_id=mid,
            family=str(m.get("family") or mid),
            kind=str(m.get("kind") or ""),
            modes=tuple(m.get("modes") or ()),
            max_images=m.get("max_images"),
            max_prompt=m.get("max_prompt"),
            end_frame_requires_start=m.get("end_frame_requires_start"),
            disable_prompt_optimization=m.get("disable_prompt_optimization"),
            declared_end_frame=m.get("declared_end_frame"),
            resolution_options=tuple(i.get("resolution_options") or ()),
            default_resolutions=tuple(i.get("default_resolutions") or ()),
            frame_resolutions=tuple(i.get("frame_resolutions") or ()),
            non_frame_resolutions=tuple(i.get("non_frame_resolutions") or ()),
            durations=tuple(i.get("durations") or ()),
            default_durations=tuple(i.get("default_durations") or ()),
            aspect_ratio_options=tuple(i.get("aspect_ratio_options") or ()),
            default_aspect_ratios=tuple(i.get("default_aspect_ratios") or ()),
            support_frame=i.get("support_frame"),
            has_spec=bool(i),
            costs=tuple(i.get("costs") or ()),
            default_cost=i.get("default_cost"),
        ))
    return merged


async def fetch_video_models(
    client: httpx.AsyncClient,
) -> tuple[list[UpstreamVideoModel], list[str]]:
    """拉取并合并视频能力表。返回 `(模型表, 降级说明)`。**任一子请求失败都不抛。**"""
    degradations: list[str] = []
    meta: dict[str, dict[str, Any]] = {}
    info: dict[str, dict[str, Any]] = {}

    for label, path, parser, sink in (
        ("create_video_models", COMMON_CONFIG_PATH, parse_video_common_config, meta),
        ("videoModels", MODEL_INFO_PATH, parse_video_model_info, info),
    ):
        try:
            resp = await client.get(path, timeout=10.0)
            resp.raise_for_status()
            sink.update(parser(resp.json()))
        except Exception as e:  # noqa: BLE001
            degradations.append(
                f"上游视频能力表 {label} 读取失败（{type(e).__name__}: {e}）⇒ "
                f"该部分退回冻结快照。")
            logger.warning(f"视频能力表 {label} 读取失败：{type(e).__name__}: {e}")

    return merge_video(meta, info), degradations


def coverage_video(models: list[UpstreamVideoModel]) -> dict[str, int]:
    return {
        "models": len(models),
        "with_spec": sum(1 for m in models if m.has_spec),
        "with_durations": sum(1 for m in models if m.durations),
        "with_costs": sum(1 for m in models if m.costs or m.default_cost),
        "with_frame_limit": sum(1 for m in models if m.max_images is not None),
    }


__all__ = [
    "COMMON_CONFIG_PATH",
    "MODEL_INFO_PATH",
    "coverage_video",
    "fetch_video_models",
    "merge_video",
    "parse_video_common_config",
    "parse_video_model_info",
]
