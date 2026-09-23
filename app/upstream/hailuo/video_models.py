#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**视频**能力注册表 —— 视频服务唯一的"能做什么/要多少钱"真相。

改动这里 = 改动对外能承接哪些模型、单价多少、首帧/首尾帧怎么落。

## 数据从哪来（两条源，都**公开、免鉴权、零计费**）

1. **`create_video_models`** —— `GET /public/api/config/web/common_config`
   ⇒ 模型**清单**、每个模型的 `type`（`t2v`/`i2v`/`s2v`）、`mode`
   （`start-end-frames` / `all-reference` / `image-reference` / `subject-reference`）、
   `maxSupportImageCount`、`maxPromptLength`、`endFrameRequiredStartFrame`、
   `disablePromptOptimization`，以及所属模型族（`modelKey`）。
2. **`videoModels`** —— `GET /public/v2/api/multimodal/video/model/info`
   ⇒ 每个模型的 `resolutions[]` / `durations[]` / `aspectRatios[]`（**含 defaultSelect**）
   与 `costs[]`（**按 resolution × duration 的真实积分单价**）。

⇒ 运行期零成本实读；读不到时退回 `FROZEN_VIDEO_SNAPSHOT` **并留降级痕**。

## 三种输入形态 ⇒ 内部自判，暴露**一个**能力名

调用方按 Seedance 的 `content[]` 说话：给不给图、给首帧还是首尾帧，是**输入**说了算的，
不该要求调用方记住 hailuo 内部的 26 个 modelID。所以每个族登记三个槽位：

| 输入形态 | 槽位 | 含义 |
|---|---|---|
| 只有文本 | `t2v` | 文生视频 |
| 只给首帧 | `first` | 图生视频（首帧） |
| 首帧 + 尾帧 | `pair` | 首尾帧生视频（未登记时回落到 `first`） |

默认族 `hailuo-video` 的三个槽位是 **`23204` / `23218` / `23210`** ——
逐条对齐参考实现 `meutils/apis/hailuoai/openai_videos.create_task` 里正在生产跑的那段：

```python
if last_frame or len(images) == 2:  model = "23210"   # 首尾帧 2.0
elif first_frame or len(images) == 1: model = "23218" # 首帧 2.3-fast
# 否则                                model = "23204"  # 文生 2.3
```

## 调用方直接给 upstream modelID 时**零映射零说明**

`model="23218"` 就是那个模型本身（的精确枚举值）⇒ 照用，**不替它做决定**。
它接不住你的输入（比如拿 `23204` 去跑首尾帧）⇒ 明确 400 并指出该用哪个，
**不静默换模型** —— 视频单价差异最高到 6 倍（`23218` 15 积分 vs `veo3.1` 180 积分）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...errors import InvalidParameterError

#: 快照日期。**改 `FROZEN_VIDEO_SNAPSHOT` 必须同步改它** —— 它是"数据有多旧"的唯一线索。
SNAPSHOT_DATE = "2026-09-23"

#: 输入形态。Seedance 的 `content[]` 判定出来就这三种之一。
MODE_T2V = "t2v"
MODE_FIRST_FRAME = "first_frame"
MODE_FIRST_LAST = "first_last_frame"

MODE_LABELS: dict[str, str] = {
    MODE_T2V: "文生视频（只有文本）",
    MODE_FIRST_FRAME: "图生视频（首帧）",
    MODE_FIRST_LAST: "首尾帧生视频（首帧 + 尾帧）",
}

#: 上游 `mode` 取值——**首尾帧**需要它。
MODE_START_END_FRAMES = "start-end-frames"
MODE_ALL_REFERENCE = "all-reference"
MODE_IMAGE_REFERENCE = "image-reference"
MODE_SUBJECT_REFERENCE = "subject-reference"


def _cost(resolutions: list[str], durations: list[int], real_cost: float) -> dict[str, Any]:
    """一条计价行。与图片链路同一形状（`UpstreamModel.cost_for` 也吃这个）。"""
    return {"resolutions": resolutions, "durations": durations,
            "realCost": real_cost, "rawCost": 0}


@dataclass(frozen=True)
class UpstreamVideoModel:
    """一个上游视频模型。字段全部来自服务端，无一是经验值。"""

    model_id: str
    #: 所属模型族（upstream `modelKey`，如 `Hailuo 2.3` / `Veo 3.1`）
    family: str
    #: upstream `type`：`t2v` / `i2v` / `s2v`
    kind: str
    modes: tuple[str, ...] = ()
    #: `maxSupportImageCount`（能垫几张框架图；`None` = 上游未声明）
    max_images: int | None = None
    #: `maxPromptLength`
    max_prompt: int | None = None
    #: `endFrameRequiredStartFrame`：给了尾帧就**必须**也给首帧
    end_frame_requires_start: bool | None = None
    #: `disablePromptOptimization`：上游是否会改写 prompt
    disable_prompt_optimization: bool | None = None
    #: 🔴 **组级**声明（upstream `modelKey` 那组的 `filterTags` 里有没有 `end_frame`）。
    #: 这是"这个族支不支持**尾帧**"的**第一手声明** —— 比逐个模型猜可靠：
    #: `Hailuo 2.0` 有 ⇒ `23210` 能吃两张；`Hailuo 2.3` / `2.3-Fast` / `1.0*` 都**没有**
    #: ⇒ 它们的 i2v 只能收首帧（实测 `23217` 带尾帧被 400 拦下，与此吻合）。
    declared_end_frame: bool | None = None

    # ---- 来自 `video/model/info` ----
    resolution_options: tuple[str, ...] = ()
    default_resolutions: tuple[str, ...] = ()
    #: 🔴 **按档位声明**的"支持首尾帧"（`parameter.resolutions[].addition.supportFrame == true`）。
    #: 这不是模型级属性：`23210` 的 768/1080 是 True，而 **512 一个字都没声明** ——
    #: 2026-09-23 实测：拿 512 去跑首尾帧，上游 6 秒后回 `code 2400001 生成内容出错了`。
    #: ⇒ 有框架图时**必须**落在这张表里的档位。
    frame_resolutions: tuple[str, ...] = ()
    #: 明确声明 `supportFrame == false` 的档位（如 `23218` 的 1080）。
    non_frame_resolutions: tuple[str, ...] = ()
    durations: tuple[int, ...] = ()
    default_durations: tuple[int, ...] = ()
    aspect_ratio_options: tuple[str, ...] = ()
    default_aspect_ratios: tuple[str, ...] = ()
    #: `addition.supportFrame`：该模型整体上是否支持框架图
    support_frame: bool | None = None
    #: 是否在 `video/model/info` 里出现过 —— 没有 ⇒ **无档位无价格**，不敢哑定
    has_spec: bool = False
    costs: tuple[dict[str, Any], ...] = ()
    default_cost: float | None = None

    # ------------------------------------------------------------------ 查询

    def cost_for(self, resolution: str, duration: int) -> float | None:
        """`(resolution, duration)` 的真实单价。查不到返回 `None`（**不猜**）。"""
        for row in self.costs:
            if resolution not in (row.get("resolutions") or []):
                continue
            ds = row.get("durations") or []
            if ds and int(duration) not in [int(d) for d in ds]:
                continue
            return float(row.get("realCost") or 0)
        return None

    def supports_frames(self) -> bool:
        """能不能吃框架图（首帧/首尾帧）。

        判据优先级：`mode` Declared > `addition.supportFrame` > `type == i2v`。
        `all-reference` 形态的 i2v 虽然没列 `start-end-frames`，但它的
        `endFrameRequiredStartFrame=True` ⇒ 首尾帧语义是被承认的。
        """
        if MODE_START_END_FRAMES in self.modes:
            return True
        if self.support_frame is True:
            return True
        if self.support_frame is False:
            return False
        return self.kind == "i2v"

    def max_frames(self) -> int:
        """能吃几张框架图。

        判据优先级（**全部来自上游声明**，无一是经验值）：
          1. 逐模型 `maxSupportImageCount`（最精确）；
          2. 否则看**组级** `end_frame` 声明：声明了 ⇒ 2，没声明 ⇒ 1；
          3. `t2v` ⇒ 0（它压根不收框架图）。

        「没声明 ⇒ 1」是**保守**的：宁可在本地 400，也不要拿两张图去打一个
        只认首帧的模型（那会白花一次积分，而且错误码不指向原因）。
        """
        if self.max_images is not None:
            return int(self.max_images)
        if self.kind == "t2v":
            return 0
        if self.declared_end_frame:
            return 2
        return 1

    def supports_frames_at(self, resolution: str | None) -> bool | None:
        """该**档位**能否吃框架图。

        返回 `True/False` 是上游**显式声明**的；`None` = 该档位没声明
        （既不是 True 也不是 False）⇒ 调用方自行决定怎么处理。

        ⚠️ 只有声明了 `supportFrame` 的档位才有确定答案 —— 这正是
        `23210@512` 那次失败的全部教训：**"表里没有"不等于"能用"**，
        它只是"没人说过它能用"。而有框架图时，赌错就是一次真实计费。
        """
        if resolution is None:
            return None
        if resolution in self.frame_resolutions:
            return True
        if resolution in self.non_frame_resolutions:
            return False
        return None

    def frame_capable_resolutions(self) -> tuple[str, ...]:
        """**明确**支持框架图的档位（按上游声明）。空 ⇒ 没声明过任何一档。"""
        return tuple(r for r in self.resolution_options if r in self.frame_resolutions)

    def to_public(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": self.model_id,
            "object": "model",
            "owned_by": "hailuo",
            "kind": "video_model",
            "family": self.family,
            "upstream_type": self.kind,
            "modes": list(self.modes),
            "max_frames": self.max_frames(),
            "max_prompt_length": self.max_prompt,
            "resolutions": list(self.resolution_options),
            "frame_capable_resolutions": list(self.frame_capable_resolutions()),
            "durations": list(self.durations),
            "aspect_ratios": list(self.aspect_ratio_options),
            "supports_frames": self.supports_frames(),
            "has_spec": self.has_spec,
            "routable": is_routable(self.model_id),
        }
        #: 接不住的模型**明说原因**，别让调用方拿它去试（试一次就是一次真实计费）
        if not entry["routable"]:
            entry["routable_note"] = NON_ROUTABLE_MODELS[self.model_id]
        return entry


@dataclass(frozen=True)
class VideoFamily:
    """一个对外能力名：把"三种输入形态 ⇒ 三个 upstream modelID"收在一处。"""

    name: str
    t2v: str | None = None
    first: str | None = None
    pair: str | None = None
    aliases: tuple[str, ...] = ()
    notes: str = ""

    def pick(self, mode: str) -> str | None:
        if mode == MODE_T2V:
            return self.t2v
        if mode == MODE_FIRST_FRAME:
            return self.first
        return self.pair or self.first

    def members(self) -> tuple[str, ...]:
        return tuple(m for m in (self.t2v, self.first, self.pair) if m)


#: 对外能力名。** `hailuo-video` 是默认** —— 它跨族组合 2.3/2.3-Fast/2.0，
#: 正是"把 Hailuo 2.x 的首尾帧 / 文生 / 图生综合到一起"这一要求的落点。
VIDEO_FAMILIES: tuple[VideoFamily, ...] = (
    VideoFamily(
        name="hailuo-video", t2v="23204", first="23218", pair="23210",
        aliases=("auto", "hailuo", "hailuo-auto", "hailuo-2.x", "视频", "video",
                 "hailuo-2", "2.x"),
        notes="**默认**。综合 Hailuo 2.x：纯文本→23204、首帧→23218、首尾帧→23210。"
              "三个槽位逐条对齐生产参考实现 openai_videos.create_task。",
    ),
    VideoFamily(
        name="hailuo-2.3", t2v="23204", first="23217",
        #: 🔴 **没有** pair 槽位：`Hailuo 2.3` 组未声明 `end_frame`，
        #: 且 `23217` 的 `maxSupportImageCount` 为空 ⇒ 只能收首帧。
        #: 给尾帧会在本地 400 并指向 `hailuo-video`（→ `23210`）。
        aliases=("hailuo23", "2.3", "hailuo-23"),
        notes="Hailuo 2.3 本族：文生 23204 / 图生 23217（25 积分，比 fast 慢也贵）。"
              "⚠️ 本族**不支持尾帧**（上游组级 `end_frame` 未声明）。",
    ),
    VideoFamily(
        name="hailuo-2.3-fast", first="23218",
        #: 同上：`Hailuo 2.3-Fast` 也没声明 `end_frame` ⇒ 无 pair 槽位。
        aliases=("hailuo23-fast", "2.3-fast"),
        notes="Hailuo 2.3-Fast：只有图生一档（**没有文生、也没有尾帧**）。"
              "768p/6s 仅需 15 积分，是 2.x 里最便宜的路。",
    ),
    VideoFamily(
        name="hailuo-2.0", t2v="23200", first="23210", pair="23210",
        aliases=("hailuo20", "2.0"),
        notes="Hailuo 2.0：图生 23210 支持首尾帧（`addition.supportFrame=true`，最多 2 张）。",
    ),
    VideoFamily(
        name="hailuo-3.0", t2v="hailuo3.0-t2v", first="hailuo3.0-i2v", pair="hailuo3.0-i2v",
        aliases=("minimax-h3", "h3", "hailuo3", "hailuo-3"),
        notes="MiniMax H3：图生一档能吃 9 张素材（`all-reference`），本服务当前"
              "只用它的**首帧/首尾帧**语义，多参考图未取证 ⇒ 传多了会 400。",
    ),
    VideoFamily(
        name="hailuo-3.0-max", t2v="hailuo_h3_max_t2v", first="hailuo_h3_max_i2v",
        pair="hailuo_h3_max_i2v",
        aliases=("minimax-h3-max", "h3-max"),
        notes="MiniMax H3 Max：`endFrameRequiredStartFrame=true` ⇒ 给尾帧必须同时给首帧。",
    ),
    VideoFamily(
        name="veo3.1", t2v="veo3.1-t2v", first="veo3.1-i2v", pair="veo3.1-i2v",
        aliases=("veo", "veo-3.1", "veo31"),
        notes="Veo 3.1：8s 固定时长，720/1080 同价 120 积分，4k 180 积分。",
    ),
    VideoFamily(
        name="veo3.1-fast", t2v="veo3.1-t2v-fast", first="veo3.1-i2v-fast",
        pair="veo3.1-i2v-fast",
        aliases=("veo-fast", "veo-3.1-fast"),
        notes="Veo 3.1-Fast：分辨率档位与标准版相同（含 3840 的 4k），价格减半"
              "（720/1080 为 60 积分，4k 90 积分）。",
    ),
    VideoFamily(
        name="sora2", t2v="sora2-t2v", first="sora2-i2v", pair=None,
        aliases=("sora-2", "sora"),
        notes="Sora 2：4/8/12s 三档（分别 40/80/120 积分）。"
              "🔴 `maxSupportImageCount=1` ⇒ **不支持尾帧**（给两张会被明确拒绝）。",
    ),
    VideoFamily(
        name="hailuo-1.0", t2v="23000", first="23001", pair=None,
        aliases=("hailuo10", "1.0", "t2v-01", "i2v-01"),
        notes="Hailuo 1.0：最老也最省。图生只有 1 张的额度 ⇒ **不支持尾帧**。",
    ),
    VideoFamily(
        name="hailuo-1.0-director", t2v="23010", first="23102", pair=None,
        aliases=("director", "t2v-01-director"),
        notes="Hailuo 1.0-Director：支持运镜指令的 1.0 系（图生额度 1 张）。",
    ),
    VideoFamily(
        name="hailuo-1.0-live", first="23011",
        #: `Hailuo 1.0-Live` 未声明 `end_frame` ⇒ 无 pair 槽位。
        aliases=("live", "i2v-01-live", "video-01-live2d"),
        notes="Hailuo 1.0-Live：只有图生，**没有文生槽位**，也不支持尾帧。",
    ),
    VideoFamily(
        name="seedance-2.0", t2v="seedance2.0-t2v", first="seedance2.0-i2v",
        pair="seedance2.0-i2v",
        aliases=("seedance", "seedance2", "seedance-2"),
        notes="上游宿主站自己挂的 Seedance 2.0。⚠️ 它**没有**出现在 `video/model/info` 里"
              " ⇒ 拿不到官方档位与价格（`has_spec=false`）⇒ 分辨率/时长无法先验校验。",
    ),
    VideoFamily(
        name="seedance-2.0-fast", t2v="seedance2.0-fast-t2v",
        first="seedance2.0-fast-i2v", pair="seedance2.0-fast-i2v",
        aliases=("seedance-fast",),
        notes="Seedance 2.0 Fast：`mode` 只声明 `all-reference`，但 "
              "`endFrameRequiredStartFrame=true` ⇒ 首尾帧可用；同样 **无档位无价格**。",
    ),
    VideoFamily(
        name="seedance-2.0-mini", t2v="seedance2.0-mini-t2v",
        first="seedance2.0-mini-i2v", pair="seedance2.0-mini-i2v",
        aliases=("seedance-mini",),
        notes="Seedance 2.0 Mini：同 2.0，**无档位无价格**。",
    ),
)

DEFAULT_VIDEO_FAMILY = "hailuo-video"

#: 第三方 SDK 常硬编码的占位名 ⇒ 等价于"没写 model"。
VIDEO_PLACEHOLDER_MODELS: frozenset[str] = frozenset({
    "auto", "seedance", "video", "hailuo-video", "sora_video2", "runway",
    "kling", "default", "",
})

#: 上层 `DELIBERATE_ABSENCES` 之一：存在但**本服务刻意不做**的模型。
VIDEO_DELIBERATE_ABSENCES: dict[str, str] = {
    "veo3.1-s2v / veo3.1-s2v-fast": "上游 `type=s2v`、`mode=image-reference`（**多参考图**语义，"
                                    "最多 3 张），与「首帧/首尾帧」不是一回事。"
                                    "本服务当前只实现首帧/首尾帧 ⇒ 传了会 **400 而非静默丢弃**。",
    "23021": "Hailuo 1.0 的 `type=s2v` + `mode=subject-reference`（主体参考），"
             "需要另一套机制；参考图 + 此模型会被上游拒 ⇒ 明确 400。",
    "hailuo3.0-extend": "**视频延长**（给一段已有视频往前/往后接）。Seedance 的 `extend` 语义"
                        "需要传参考视频，本服务不接受视频素材 ⇒ 不实现。",
    "多参考图（all-reference）": "Hailuo 3.0 / Seedance 2.0 的图生档允许最多 9 张素材，"
                                "但它们的**帧语义**未取证 ⇒ 本服务只接受 1 张首帧 + 1 张尾帧。",
    "generate_audio / 音频参考": "部分族声明 `enableAudio` 与 `maxSupportAudioCount`，"
                                "但音频素材的上传与字段形态**未取证** ⇒ 收到 `generate_audio` 只记降级痕。",
}

#: 🔴 **不可路由的模型**：上游存在（运行期实读会读到），但本服务的
#: `content[]` 语义**服务不了**它们 ⇒ `/v1/models` 里带 `routable: false`，
#: 直接传它们在 `route_exact` 里 **400 并给出原因**。
#:
#: 为什么要单列这张表：清单里混进"看起来能用、实际一传就错"的条目，
#: 等于让调用方按它的预期去花钱 —— 与"不静默降级"是同一条红线。
NON_ROUTABLE_MODELS: dict[str, str] = {
    "veo3.1-s2v": "上游 `type=s2v` / `mode=image-reference`（多参考图语义，最多 3 张），"
                  "本服务的 content[] 只表达首帧/首尾帧 ⇒ 接不住。",
    "veo3.1-s2v-fast": "同 `veo3.1-s2v`（Fast 版）：多参考图语义，本服务接不住。",
    "23021": "上游 `type=s2v` / `mode=subject-reference`（主体参考），本服务接不住。",
    "hailuo3.0-extend": "**视频延长**需要传参考视频，本服务不接受视频素材。",
}


def is_routable(model_id: str) -> bool:
    return model_id not in NON_ROUTABLE_MODELS


# ---------------------------------------------------------------------------
# 冻结快照（2026-09-23 从两个公开端点实读，**逐字段照抄**，脚本生成，非手写）
# ---------------------------------------------------------------------------

FROZEN_VIDEO_SNAPSHOT: tuple[UpstreamVideoModel, ...] = (
    UpstreamVideoModel(
        model_id='23204', family='Hailuo 2.3', kind='t2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=None,
        declared_end_frame=False,
        resolution_options=('768', '1080'),
        default_resolutions=('768',),
        frame_resolutions=(),
        non_frame_resolutions=('1080',),
        durations=(6, 10),
        default_durations=(6,),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=True,
        costs=(
            _cost(['768'], [6], 25),
            _cost(['768'], [10], 50),
            _cost(['1080'], [6], 80),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='23217', family='Hailuo 2.3', kind='i2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=None,
        declared_end_frame=False,
        resolution_options=('768', '1080'),
        default_resolutions=('768',),
        frame_resolutions=(),
        non_frame_resolutions=('1080',),
        durations=(6, 10),
        default_durations=(6,),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=True,
        costs=(
            _cost(['768'], [6], 25),
            _cost(['768'], [10], 50),
            _cost(['1080'], [6], 80),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='23218', family='Hailuo 2.3-Fast', kind='i2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=None,
        declared_end_frame=False,
        resolution_options=('768', '1080'),
        default_resolutions=('768',),
        frame_resolutions=(),
        non_frame_resolutions=('1080',),
        durations=(6, 10),
        default_durations=(6,),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=True,
        costs=(
            _cost(['768'], [6], 15),
            _cost(['768'], [10], 30),
            _cost(['1080'], [6], 50),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='23200', family='Hailuo 2.0', kind='t2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=None,
        declared_end_frame=True,
        resolution_options=('768', '1080'),
        default_resolutions=('768',),
        frame_resolutions=(),
        non_frame_resolutions=('1080',),
        durations=(6, 10),
        default_durations=(6,),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=True,
        costs=(
            _cost(['768'], [6], 25),
            _cost(['768'], [10], 50),
            _cost(['1080'], [6], 80),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='23210', family='Hailuo 2.0', kind='i2v',
        modes=('start-end-frames',),
        max_images=2, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=None,
        declared_end_frame=True,
        resolution_options=('512', '768', '1080'),
        default_resolutions=('768',),
        frame_resolutions=('768', '1080'),
        non_frame_resolutions=(),
        durations=(6, 10),
        default_durations=(6,),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=True, has_spec=True,
        costs=(
            _cost(['512'], [6], 12),
            _cost(['512'], [10], 25),
            _cost(['768'], [6], 25),
            _cost(['768'], [10], 50),
            _cost(['1080'], [6], 80),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='hailuo3.0-t2v', family='MiniMax H3', kind='t2v',
        modes=('all-reference', 'start-end-frames'),
        max_images=None, max_prompt=7000,
        end_frame_requires_start=None,
        disable_prompt_optimization=True,
        declared_end_frame=True,
        resolution_options=('768', '1440'),
        default_resolutions=('1440',),
        frame_resolutions=('768', '1440'),
        non_frame_resolutions=(),
        durations=(4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15),
        default_durations=(5,),
        aspect_ratio_options=('Auto', '21:9', '16:9', '4:3', '1:1', '3:4', '9:16'),
        default_aspect_ratios=('16:9',),
        support_frame=True, has_spec=True,
        costs=(
            _cost(['1440'], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 0),
            _cost(['1440'], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 0),
            _cost(['768'], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 0),
            _cost(['768'], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 0),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='hailuo3.0-i2v', family='MiniMax H3', kind='i2v',
        modes=('all-reference', 'start-end-frames'),
        max_images=9, max_prompt=7000,
        end_frame_requires_start=False,
        disable_prompt_optimization=True,
        declared_end_frame=True,
        resolution_options=('768', '1440'),
        default_resolutions=('1440',),
        frame_resolutions=('768', '1440'),
        non_frame_resolutions=(),
        durations=(4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15),
        default_durations=(5,),
        aspect_ratio_options=('Auto', '21:9', '16:9', '4:3', '1:1', '3:4', '9:16'),
        default_aspect_ratios=('Auto',),
        support_frame=True, has_spec=True,
        costs=(
            _cost(['1440'], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 0),
            _cost(['1440'], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 0),
            _cost(['768'], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 0),
            _cost(['768'], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 0),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='hailuo_h3_max_t2v', family='MiniMax H3 Max', kind='t2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=7000,
        end_frame_requires_start=None,
        disable_prompt_optimization=False,
        declared_end_frame=True,
        resolution_options=('480', '768'),
        default_resolutions=('768',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15),
        default_durations=(5,),
        aspect_ratio_options=('Auto', '21:9', '16:9', '4:3', '1:1', '3:4', '9:16'),
        default_aspect_ratios=('16:9',),
        support_frame=False, has_spec=True,
        costs=(
            _cost(['480'], [5], 20),
            _cost(['768'], [5], 35),
            _cost(['480'], [6], 24),
            _cost(['768'], [6], 42),
            _cost(['480'], [7], 28),
            _cost(['768'], [7], 49),
            _cost(['480'], [8], 32),
            _cost(['768'], [8], 56),
            _cost(['480'], [9], 36),
            _cost(['768'], [9], 63),
            _cost(['480'], [10], 40),
            _cost(['768'], [10], 70),
            _cost(['480'], [11], 44),
            _cost(['768'], [11], 77),
            _cost(['480'], [12], 48),
            _cost(['768'], [12], 84),
            _cost(['480'], [13], 52),
            _cost(['768'], [13], 91),
            _cost(['480'], [14], 56),
            _cost(['768'], [14], 98),
            _cost(['480'], [15], 60),
            _cost(['768'], [15], 105),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='hailuo_h3_max_i2v', family='MiniMax H3 Max', kind='i2v',
        modes=('start-end-frames',),
        max_images=2, max_prompt=7000,
        end_frame_requires_start=True,
        disable_prompt_optimization=False,
        declared_end_frame=True,
        resolution_options=('480', '768'),
        default_resolutions=('768',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15),
        default_durations=(5,),
        aspect_ratio_options=('Auto', '21:9', '16:9', '4:3', '1:1', '3:4', '9:16'),
        default_aspect_ratios=('Auto',),
        support_frame=True, has_spec=True,
        costs=(
            _cost(['480'], [5], 20),
            _cost(['768'], [5], 35),
            _cost(['480'], [6], 24),
            _cost(['768'], [6], 42),
            _cost(['480'], [7], 28),
            _cost(['768'], [7], 49),
            _cost(['480'], [8], 32),
            _cost(['768'], [8], 56),
            _cost(['480'], [9], 36),
            _cost(['768'], [9], 63),
            _cost(['480'], [10], 40),
            _cost(['768'], [10], 70),
            _cost(['480'], [11], 44),
            _cost(['768'], [11], 77),
            _cost(['480'], [12], 48),
            _cost(['768'], [12], 84),
            _cost(['480'], [13], 52),
            _cost(['768'], [13], 91),
            _cost(['480'], [14], 56),
            _cost(['768'], [14], 98),
            _cost(['480'], [15], 60),
            _cost(['768'], [15], 105),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='veo3.1-t2v', family='Veo 3.1', kind='t2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=True,
        declared_end_frame=True,
        resolution_options=('720', '1080', '3840'),
        default_resolutions=('720',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(8,),
        default_durations=(8,),
        aspect_ratio_options=('16:9', '9:16'),
        default_aspect_ratios=('16:9',),
        support_frame=False, has_spec=True,
        costs=(
            _cost(['720', '1080'], [8], 120),
            _cost(['3840'], [8], 180),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='veo3.1-i2v', family='Veo 3.1', kind='i2v',
        modes=('start-end-frames',),
        max_images=2, max_prompt=None,
        end_frame_requires_start=True,
        disable_prompt_optimization=True,
        declared_end_frame=True,
        resolution_options=('720', '1080', '3840'),
        default_resolutions=('720',),
        frame_resolutions=('720', '1080', '3840'),
        non_frame_resolutions=(),
        durations=(8,),
        default_durations=(8,),
        aspect_ratio_options=('16:9', '9:16'),
        default_aspect_ratios=('16:9',),
        support_frame=True, has_spec=True,
        costs=(
            _cost(['720', '1080'], [8], 120),
            _cost(['3840'], [8], 180),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='veo3.1-t2v-fast', family='Veo 3.1-Fast', kind='t2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=True,
        declared_end_frame=True,
        resolution_options=('720', '1080', '3840'),
        default_resolutions=('720',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(8,),
        default_durations=(8,),
        aspect_ratio_options=('16:9', '9:16'),
        default_aspect_ratios=('16:9',),
        support_frame=False, has_spec=True,
        costs=(
            _cost(['720', '1080'], [8], 60),
            _cost(['3840'], [8], 90),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='veo3.1-i2v-fast', family='Veo 3.1-Fast', kind='i2v',
        modes=('start-end-frames',),
        max_images=2, max_prompt=None,
        end_frame_requires_start=True,
        disable_prompt_optimization=True,
        declared_end_frame=True,
        resolution_options=('720', '1080', '3840'),
        default_resolutions=('720',),
        frame_resolutions=('720', '1080', '3840'),
        non_frame_resolutions=(),
        durations=(8,),
        default_durations=(8,),
        aspect_ratio_options=('16:9', '9:16'),
        default_aspect_ratios=('16:9',),
        support_frame=True, has_spec=True,
        costs=(
            _cost(['720', '1080'], [8], 60),
            _cost(['3840'], [8], 90),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='sora2-t2v', family='Sora 2', kind='t2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=True,
        declared_end_frame=True,
        resolution_options=('720',),
        default_resolutions=('720',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(4, 8, 12),
        default_durations=(4, 8, 12),
        aspect_ratio_options=('16:9', '9:16'),
        default_aspect_ratios=('16:9',),
        support_frame=False, has_spec=True,
        costs=(
            _cost(['720'], [4], 40),
            _cost(['720'], [8], 80),
            _cost(['720'], [12], 120),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='sora2-i2v', family='Sora 2', kind='i2v',
        modes=('start-end-frames',),
        max_images=1, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=True,
        declared_end_frame=True,
        resolution_options=('720',),
        default_resolutions=('720',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(4, 8, 12),
        default_durations=(4, 8, 12),
        aspect_ratio_options=('16:9', '9:16'),
        default_aspect_ratios=('16:9',),
        support_frame=False, has_spec=True,
        costs=(
            _cost(['720'], [4], 40),
            _cost(['720'], [8], 80),
            _cost(['720'], [12], 120),
        ),
        default_cost=0,
    ),
    UpstreamVideoModel(
        model_id='23000', family='Hailuo 1.0', kind='t2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=None,
        declared_end_frame=False,
        resolution_options=('720',),
        default_resolutions=('720',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(6,),
        default_durations=(6,),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=True,
        costs=(),
        default_cost=25,
    ),
    UpstreamVideoModel(
        model_id='23001', family='Hailuo 1.0', kind='i2v',
        modes=('start-end-frames',),
        max_images=1, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=None,
        declared_end_frame=False,
        resolution_options=('720',),
        default_resolutions=('720',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(6,),
        default_durations=(6,),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=True,
        costs=(),
        default_cost=25,
    ),
    UpstreamVideoModel(
        model_id='23010', family='Hailuo 1.0-Director', kind='t2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=None,
        declared_end_frame=False,
        resolution_options=('720',),
        default_resolutions=('720',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(6,),
        default_durations=(6,),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=True,
        costs=(),
        default_cost=25,
    ),
    UpstreamVideoModel(
        model_id='23102', family='Hailuo 1.0-Director', kind='i2v',
        modes=('start-end-frames',),
        max_images=1, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=None,
        declared_end_frame=False,
        resolution_options=('720',),
        default_resolutions=('720',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(6,),
        default_durations=(6,),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=True,
        costs=(),
        default_cost=25,
    ),
    UpstreamVideoModel(
        model_id='23011', family='Hailuo 1.0-Live', kind='i2v',
        modes=('start-end-frames',),
        max_images=None, max_prompt=None,
        end_frame_requires_start=None,
        disable_prompt_optimization=None,
        declared_end_frame=False,
        resolution_options=('720',),
        default_resolutions=('720',),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(6,),
        default_durations=(6,),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=True,
        costs=(),
        default_cost=25,
    ),
    UpstreamVideoModel(
        model_id='seedance2.0-t2v', family='seedance-2.0', kind='t2v',
        modes=('start-end-frames', 'all-reference'),
        max_images=None, max_prompt=7500,
        end_frame_requires_start=None,
        disable_prompt_optimization=True,
        declared_end_frame=False,
        resolution_options=(),
        default_resolutions=(),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(),
        default_durations=(),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=False,
        costs=(),
        default_cost=None,
    ),
    UpstreamVideoModel(
        model_id='seedance2.0-i2v', family='seedance-2.0', kind='i2v',
        modes=('start-end-frames', 'all-reference'),
        max_images=9, max_prompt=7500,
        end_frame_requires_start=True,
        disable_prompt_optimization=True,
        declared_end_frame=False,
        resolution_options=(),
        default_resolutions=(),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(),
        default_durations=(),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=False,
        costs=(),
        default_cost=None,
    ),
    UpstreamVideoModel(
        model_id='seedance2.0-fast-t2v', family='seedance-2.0-fast', kind='t2v',
        modes=('all-reference',),
        max_images=None, max_prompt=7500,
        end_frame_requires_start=None,
        disable_prompt_optimization=True,
        declared_end_frame=False,
        resolution_options=(),
        default_resolutions=(),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(),
        default_durations=(),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=False,
        costs=(),
        default_cost=None,
    ),
    UpstreamVideoModel(
        model_id='seedance2.0-fast-i2v', family='seedance-2.0-fast', kind='i2v',
        modes=('all-reference',),
        max_images=9, max_prompt=7500,
        end_frame_requires_start=True,
        disable_prompt_optimization=True,
        declared_end_frame=False,
        resolution_options=(),
        default_resolutions=(),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(),
        default_durations=(),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=False,
        costs=(),
        default_cost=None,
    ),
    UpstreamVideoModel(
        model_id='seedance2.0-mini-t2v', family='seedance2.0-mini', kind='t2v',
        modes=('start-end-frames', 'all-reference'),
        max_images=None, max_prompt=7500,
        end_frame_requires_start=None,
        disable_prompt_optimization=True,
        declared_end_frame=False,
        resolution_options=(),
        default_resolutions=(),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(),
        default_durations=(),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=False,
        costs=(),
        default_cost=None,
    ),
    UpstreamVideoModel(
        model_id='seedance2.0-mini-i2v', family='seedance2.0-mini', kind='i2v',
        modes=('start-end-frames', 'all-reference'),
        max_images=9, max_prompt=7500,
        end_frame_requires_start=True,
        disable_prompt_optimization=True,
        declared_end_frame=False,
        resolution_options=(),
        default_resolutions=(),
        frame_resolutions=(),
        non_frame_resolutions=(),
        durations=(),
        default_durations=(),
        aspect_ratio_options=(),
        default_aspect_ratios=(),
        support_frame=None, has_spec=False,
        costs=(),
        default_cost=None,
    ),
)


# ---------------------------------------------------------------------------
# 索引与解析
# ---------------------------------------------------------------------------

_FAMILY_INDEX: dict[str, str] = {}
for _f in VIDEO_FAMILIES:
    _FAMILY_INDEX[_f.name.lower()] = _f.name
    for _a in _f.aliases:
        _FAMILY_INDEX[_a.lower()] = _f.name

_RUNTIME: list[UpstreamVideoModel] = []


def install_runtime_video_models(models: list[UpstreamVideoModel]) -> None:
    """装入运行期实读的视频模型表（`video_capabilities.py` 解析后调用）。"""
    global _RUNTIME
    _RUNTIME = list(models)


def runtime_video_models() -> list[UpstreamVideoModel]:
    return list(_RUNTIME)


def all_video_models() -> list[UpstreamVideoModel]:
    """当前生效的视频模型表：**优先运行期实读，退回冻结快照**。"""
    return _RUNTIME or list(FROZEN_VIDEO_SNAPSHOT)


def get_video_model(model_id: str) -> UpstreamVideoModel | None:
    for m in all_video_models():
        if m.model_id == model_id:
            return m
    return None


def get_family(name: str) -> VideoFamily | None:
    for f in VIDEO_FAMILIES:
        if f.name == name:
            return f
    return None


def resolve_family(value: str | None) -> VideoFamily:
    """把调用方的 `model` 解析成 `VideoFamily`。认不出 ⇒ 400（**列可用值**）。"""
    raw = (value or "").strip()
    if raw.lower() in VIDEO_PLACEHOLDER_MODELS:
        return require_family(DEFAULT_VIDEO_FAMILY)
    hit = _FAMILY_INDEX.get(raw.lower())
    if hit:
        return require_family(hit)
    #: 精确的上游 modelID 也算"认识" —— 但那**不是**族，走 `route_exact`
    if get_video_model(raw) is not None:
        raise InvalidParameterError(
            f"'{raw}' 是**上游 modelID**（精确模型），不是能力名。"
            f"直接传它也可以 —— 但那样本服务**不会**替你按输入形态挑模型"
            f"（给不了的形态会直接 400）。想要自动判定请传能力名，如 "
            f"'{DEFAULT_VIDEO_FAMILY}'。",
            param="model")
    raise InvalidParameterError(
        f"未知 model={raw!r}。可用能力名：{', '.join(f.name for f in VIDEO_FAMILIES)}；"
        f"或直接传上游 modelID（如 23218 / hailuo3.0-i2v / sora2-i2v）。",
        param="model")


def require_family(name: str) -> VideoFamily:
    f = get_family(name)
    if f is None:  # pragma: no cover —— 注册表自洽时到不了
        raise InvalidParameterError(f"能力名 {name!r} 不存在。", param="model")
    return f


def _reject_non_routable(model_id: str) -> None:
    """上游有此模型、但本服务的 `content[]` 语义服务不了它 ⇒ **直接 400**。

    ⚠️ 不加这道闸的话，`model="veo3.1-s2v"` 这种"看起来能用"的条目会被原样转发
    给上游（多参考图语义），结果是一次**真实的失败计费** —— 而调用方完全不知道
    自己踩的是"本服务没实现"。
    """
    reason = NON_ROUTABLE_MODELS.get(model_id)
    if not reason:
        return
    raise InvalidParameterError(
        f"模型 {model_id} 上游存在，但**本服务接不住**：{reason} "
        f"可用能力名：{', '.join(f.name for f in VIDEO_FAMILIES)}。",
        param="model")


def route_exact(model_id: str, mode: str) -> tuple[str, list[str]]:
    """调用方直给了 upstream modelID ⇒ **原样用**。返回 `(model_id, 降级说明)`。

    🔴 它接不住的输入一律 **400**（由调用方 `build_plan` 抛）。视频单价差 6 倍，
    "悄咪咪换个模型"等于让人按 A 的预期为 B 付钱 —— 那是本项目的红线。
    """
    m = get_video_model(model_id)
    if m is None:  # pragma: no cover —— 调用方先校验过
        raise InvalidParameterError(f"上游模型 {model_id!r} 不在能力表里。", param="model")
    if mode != MODE_T2V:
        #: 先看它是文生还是图生 —— 这条提示必须说中根因：
        #: `23204` 这类 t2v 模型虽然 `mode` 里写了 `start-end-frames`，
        #: 但它的 `maxSupportImageCount` 是空 ⇒ 一张框架图都收不下。
        limit = m.max_frames()
        if limit < (2 if mode == MODE_FIRST_LAST else 1):
            fam = _family_of(model_id)
            if m.kind == "t2v":
                why = "它是**文生**模型（upstream type=t2v），收不下任何框架图"
            else:
                why = f"它声明最多接受 {limit} 张框架图（upstream maxSupportImageCount）"
            advice = ""
            if mode == MODE_FIRST_LAST:
                advice = "请只给 first_frame，"
            if fam:
                alt = fam.pick(mode)
                if alt and alt != model_id:
                    advice += (f"或改传能力名 '{fam.name}'（同形态会落到 {alt}）")
                else:
                    advice += (f"或换一个支持{MODE_LABELS[mode]}的能力名"
                               f"（如 '{DEFAULT_VIDEO_FAMILY}'）")
            raise InvalidParameterError(
                f"模型 {model_id} 无法承接{MODE_LABELS[mode]}：{why}。{advice}。",
                param="model" if m.kind == "t2v" else "content")
    return model_id, []


def resolve_route(value: str | None, mode: str) -> tuple[str, str, list[str]]:
    """``model`` + 输入形态 ⇒ `(upstream_model_id, 能力名, 降级说明)`。**此为唯一出口。**"""
    raw = (value or "").strip()
    notes: list[str] = []

    if raw and get_video_model(raw) is not None:
        _reject_non_routable(raw)
        mid = route_exact(raw, mode)[0]
        return mid, f"upstream:{raw}", notes

    family = resolve_family(raw)
    if not raw or raw.lower() in VIDEO_PLACEHOLDER_MODELS:
        notes.append(
            f"未指定 model（或给了占位名 {raw or '（空）'!r}）⇒ 用默认能力 "
            f"'{family.name}'。")

    picked = family.pick(mode)
    if not picked:
        #: 该族没有这个槽位 ⇒ **响亮失败**并给出可执行建议
        have = ", ".join(f"{k}→{v}" for k, v in
                         (("文生", family.t2v), ("首帧", family.first),
                          ("首尾帧", family.pair or family.first)) if v)
        raise InvalidParameterError(
            f"能力名 '{family.name}' 不支持{MODE_LABELS[mode]}。"
            f"它登记的槽位：{have}。"
            f"请换一个能力名（如 '{DEFAULT_VIDEO_FAMILY}'），或直接传上游 modelID。",
            param="model")

    model = get_video_model(picked)
    if model is None:  # pragma: no cover —— 注册表自洽时到不了
        raise CapabilityGone(f"能力名 '{family.name}' 指向的 {picked} 已不在能力表里。")

    if mode == MODE_FIRST_LAST and model.max_frames() < 2:
        raise InvalidParameterError(
            f"'{family.name}' 在本形态下落到 {picked}，而它声明最多 "
            f"{model.max_frames()} 张框架图 ⇒ 不支持尾帧。请只给 first_frame，"
            f"或换一个支持首尾帧的能力名（如 '{DEFAULT_VIDEO_FAMILY}' / hailuo-3.0）。",
            param="content")

    notes.append(f"model={raw or '（空）'} + {MODE_LABELS[mode]} ⇒ "
                 f"上游 modelID={picked}（能力名 '{family.name}'）。")
    return picked, family.name, notes


def _family_of(model_id: str) -> VideoFamily | None:
    for f in VIDEO_FAMILIES:
        if model_id in f.members():
            return f
    return None


class CapabilityGone(InvalidParameterError):
    """能力名指向的模型已从上游能力表消失。"""


# ---------------------------------------------------------------------------
# 参数吸附（**方向一律向下** —— 让人少花钱而不是多花钱）
# ---------------------------------------------------------------------------


def snap_duration(model: UpstreamVideoModel, want: int | None) -> tuple[int, list[str]]:
    """把想要的秒数落到上游声明的 `durations[]`。返回 `(秒数, 说明)`。

    · 没声明目标档位（或无 `has_spec`）⇒ **照原样转交**，说明"未校验"；
    · 命中 ⇒ 直接用；
    · 未命中 ⇒ **向下吸附**到最近的可用档（向上等于替人加钱）；
    · 比最小档还小 ⇒ 取最小档。
    """
    if want is None:
        if model.default_durations:
            return model.default_durations[0], [
                f"未指定 duration ⇒ 用上游默认 {model.default_durations[0]}s。"]
        return 6, ["模型未声明 duration 档位，也未给 defaultSelect ⇒ 按 6s 兜底（**未校验**）。"]
    if not model.durations:
        return int(want), [f"模型 {model.model_id} 未声明 duration 档位 ⇒ "
                           f"duration={want} 原样转发（上游可能拒）。"]
    options = sorted(model.durations)
    if want in options:
        return want, []
    lower = [d for d in options if d <= want]
    if lower:
        chosen, why = lower[-1], "向下吸附"
    else:
        #: 想要的比所有档都短（如 sora2 要 3s）⇒ 只能取最小档 ——
        #: 这是**向上**，必须说清楚：下游付费时长变长了。
        chosen, why = options[0], "没有任何可用档 ≤ 它 ⇒ 取最小档（**注意：时长被拉长了**）"
    return chosen, [f"duration={want}s 不在 {model.model_id} 的可用档 "
                    f"{list(options)} ⇒ **{why}**为 {chosen}s。"]


def snap_resolution(model: UpstreamVideoModel,
                    want: str | None,
                    *, require_frames: bool = False) -> tuple[str | None, list[str]]:
    """把想要的分辨率落到上游声明的 `resolutions[]`（单位已被剥成裸数字）。

    `require_frames=True`（本次要带框架图）时**多一道门禁**：只许落在
    **上游明确声明 `supportFrame: true` 的档位**上。

    🔴 这道门禁是 2026-09-23 实测逼出来的：`23210` 的 512 档在价目表里最便宜
    （12 积分），但它**没有**声明 `supportFrame` —— 拿它跑首尾帧，上游 6 秒后
    回 `code 2400001 生成内容出错了，请重新生成`（**一次真实计费的白跑**，
    而且这行错误不会告诉你"是档位选错了"）。
    """
    notes: list[str] = []
    options = list(model.resolution_options)
    frame_ok = list(model.frame_capable_resolutions())

    def _guard(chosen: str | None) -> str | None:
        """有框架图时把档位约束到 frame_ok 里；无声明则**不猜**（返回 None 由调用方决定）。"""
        if not require_frames or chosen is None:
            return chosen
        if chosen in frame_ok:
            return chosen
        if chosen in model.non_frame_resolutions:
            #: 上游**明确**说这一档不支持框架图（如 23218 的 1080）⇒ 不许放行
            if frame_ok:
                best = min(frame_ok, key=lambda r: (abs(_num(r) - _num(chosen)), _num(r)))
                notes.append(
                    f"resolution={chosen} 被 {model.model_id} **明确声明**不支持框架图"
                    f"（`supportFrame: false`）⇒ 已改到 {best}。")
                return best
            raise InvalidParameterError(
                f"resolution={chosen} 被 {model.model_id} 明确声明不支持框架图"
                f"（`supportFrame: false`），而该模型没有任何一档声明支持 ⇒ "
                f"带框架图时无法用这个模型，请换能力名或去掉框架图。",
                param="resolution")
        if not frame_ok:
            #: 该模型**一档都没声明** supportFrame —— 不能因此判它不支持
            #: （`23218` 这种 i2v 模型本来就没逐档声明）⇒ 原样放行，但**留痕**说明未校验
            notes.append(
                f"模型 {model.model_id} 未逐档声明 `supportFrame` ⇒ "
                f"resolution={chosen} 的框架图能力**未经声明校验**"
                f"（若上游报 `code 2400001`，先换到默认档 "
                f"{list(model.default_resolutions) or '上游默认'} 再试）。")
            return chosen
        best = min(frame_ok, key=lambda r: (abs(_num(r) - _num(chosen)), _num(r)))
        notes.append(
            f"resolution={chosen}（{model.model_id} 上）**未声明** supportFrame ⇒ "
            f"带框架图时不可用（实测会得到 `code 2400001`）⇒ 已改到 "
            f"{best}（该模型声明支持框架图的档位：{frame_ok}）。")
        return best

    if not want:
        if model.default_resolutions:
            chosen = _guard(model.default_resolutions[0])
            notes.append(
                f"未指定 resolution ⇒ 用上游默认档 {model.default_resolutions[0]}"
                + (f"，并因带框架图约束到 {chosen}。" if chosen != model.default_resolutions[0] else "。"))
            return chosen, notes
        return None, notes
    if not options:
        return want, [f"模型 {model.model_id} 未声明 resolution 档位 ⇒ "
                      f"resolution={want} 原样转发（上游可能拒）。"]
    if want in options:
        return _guard(want), notes
    #: 归一化工程量级以后比距离（768 与 720 应被视为同一档）
    target = _num(want)
    best = min(options, key=lambda r: (abs(_num(r) - target), _num(r)))
    notes.append(f"resolution={want} 不在 {model.model_id} 的可用档 {options} ⇒ "
                 f"吸附为 {best}。")
    return _guard(best), notes


def _num(value: str) -> int:
    head = ""
    for ch in str(value):
        if ch.isdigit():
            head += ch
        elif head:
            break
    return int(head) if head else 0


def snap_ratio(model: UpstreamVideoModel, want: str | None) -> tuple[str | None, list[str]]:
    """`ratio` → 上游 `aspectRatio`。**未声明比例档位的模型一律不转发**（2.x 系就这样）。"""
    if not want:
        if model.default_aspect_ratios:
            return model.default_aspect_ratios[0], []
        return None, []
    options = list(model.aspect_ratio_options)
    if not options:
        return None, [f"模型 {model.model_id} 未声明比例档位 ⇒ ratio={want} "
                      f"**不予转发**（画面比例由上游自行决定）。"]
    if want in options:
        return want, []
    if "Auto" in options:
        return "Auto", [f"ratio={want} 不被 {model.model_id} 支持（可选 {options}）"
                        f" ⇒ 退到 Auto（由上游按输入推断）。"]
    return options[0], [f"ratio={want} 不被 {model.model_id} 支持（可选 {options}）"
                        f" ⇒ 退到 {options[0]}。"]


# ---------------------------------------------------------------------------
# 对外清单
# ---------------------------------------------------------------------------


def family_public(f: VideoFamily) -> dict[str, Any]:
    return {
        "id": f.name,
        "object": "model",
        "owned_by": "hailuo-service",
        "kind": "capability",
        "created": 0,
        "slots": {"t2v": f.t2v, "first_frame": f.first,
                  "first_last_frame": f.pair or f.first},
        "aliases": list(f.aliases),
        "notes": f.notes,
    }


def video_catalog() -> list[dict[str, Any]]:
    """`/v1/models` 里**视频**部分的数据。

    🔴 不可路由的模型（`NON_ROUTABLE_MODELS`）**也要出现**，带 `routable: false`
    与原因 —— 运行期实读会把它们读进表里（上游确实有），
    把它们从清单里吞掉会让调用方以为"这服务没有这个模型"，
    而实际上传了会 400。**两者都要能解释**。
    """
    data: list[dict[str, Any]] = [m.to_public() for m in all_video_models()]
    known = {entry["id"] for entry in data}
    for mid, reason in NON_ROUTABLE_MODELS.items():
        if mid in known:
            continue
        #: 冻结快照里刻意没登记它们 ⇒ 这里补一条"存在但接不住"的最简条目
        data.append({
            "id": mid, "object": "model", "owned_by": "hailuo",
            "kind": "video_model", "routable": False, "routable_note": reason,
            "has_spec": False,
            "note": "上游存在，但本服务的 content[] 语义接不住（列出仅供对账）。",
        })
    data += [family_public(f) for f in VIDEO_FAMILIES]
    return data


def status() -> dict[str, Any]:
    return {
        "source": "runtime" if _RUNTIME else "frozen_snapshot",
        "snapshot_date": SNAPSHOT_DATE,
        "model_count": len(all_video_models()),
        "family_count": len(VIDEO_FAMILIES),
        "default_family": DEFAULT_VIDEO_FAMILY,
        "families": [f.name for f in VIDEO_FAMILIES],
    }


__all__ = [
    "DEFAULT_VIDEO_FAMILY",
    "FROZEN_VIDEO_SNAPSHOT",
    "MODE_FIRST_FRAME",
    "MODE_FIRST_LAST",
    "MODE_T2V",
    "MODE_LABELS",
    "NON_ROUTABLE_MODELS",
    "SNAPSHOT_DATE",
    "VIDEO_DELIBERATE_ABSENCES",
    "VIDEO_FAMILIES",
    "VIDEO_PLACEHOLDER_MODELS",
    "VideoFamily",
    "UpstreamVideoModel",
    "all_video_models",
    "family_public",
    "get_family",
    "get_video_model",
    "install_runtime_video_models",
    "is_routable",
    "resolve_family",
    "resolve_route",
    "route_exact",
    "runtime_video_models",
    "snap_duration",
    "snap_ratio",
    "snap_resolution",
    "status",
    "video_catalog",
]
