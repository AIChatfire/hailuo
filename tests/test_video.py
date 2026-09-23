#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频链路（火山 Seedance 协议出口）测试。

## 这套用例守的两条红线

1. **零出网**：全程走 `FakeHailuo`（`httpx.MockTransport`）⇒ 任何"不小心真建了任务"
   都会立刻以连接失败暴露，而不是变成一笔真实的积分账单；
2. **不可静默降级**：每一个被改动的参数（时长/档位/比例/模型）都必须在
   `degradations` 里说出来 —— 视频单价差最高 6 倍，静默换档是最贵的一类 bug。
"""
from __future__ import annotations

import re

import pytest

from app.errors import InvalidParameterError
from app.upstream.hailuo.client import ST_SUCCESS
from app.upstream.hailuo.video_models import (
    MODE_FIRST_FRAME,
    MODE_FIRST_LAST,
    MODE_T2V,
    VIDEO_FAMILIES,
    get_family,
    resolve_route,
    snap_duration,
    get_video_model,
)
from app.video_service import (
    FRAME_TYPE_FIRST,
    FRAME_TYPE_LAST,
    detect_mode,
    parse_content,
    video_view,
)

from .conftest import FAKE_JWT, PNG_1PX

AUTH = {"Authorization": f"Bearer {FAKE_JWT}"}
REF = "https://cdn.hailuoai.video/ref/frame.png"


# ---------------------------------------------------------------------------
# content[] 解析与形态判定 —— 这就是"内部自行判断"的全部
# ---------------------------------------------------------------------------


def test_parse_content_text_only_is_t2v() -> None:
    prompt, frames, notes = parse_content([{"type": "text", "text": "a cat"}])
    assert prompt == "a cat" and frames == {} and notes == []
    assert detect_mode(frames) == MODE_T2V


def test_parse_content_single_image_defaults_to_first_frame() -> None:
    """没写 role 的图 = **首帧**（Seedance 图生视频的默认语义）。"""
    prompt, frames, _ = parse_content([
        {"type": "text", "text": "p"},
        {"type": "image_url", "image_url": {"url": REF}},
    ])
    assert frames == {"first_frame": REF}
    assert detect_mode(frames) == MODE_FIRST_FRAME


def test_parse_content_first_and_last_frame() -> None:
    _p, frames, _ = parse_content([
        {"type": "image_url", "image_url": {"url": REF}, "role": "first_frame"},
        {"type": "image_url", "image_url": {"url": REF + "2"}, "role": "last_frame"},
    ])
    assert frames["first_frame"] == REF and frames["last_frame"] == REF + "2"
    assert detect_mode(frames) == MODE_FIRST_LAST


def test_parse_content_multiple_texts_are_joined_and_declared() -> None:
    """多条 text 是**本服务的合并口径**（原生只允许一条）⇒ 必须留痕。"""
    prompt, _frames, notes = parse_content([
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert prompt == "a\nb"
    assert any("拼接" in n for n in notes)


def test_parse_content_rejects_reference_role_instead_of_silently_using_it() -> None:
    """🔴 多参考图（全能参考）语义未取证 ⇒ **响亮 400**，不静默当首帧。"""
    with pytest.raises(InvalidParameterError) as ei:
        parse_content([{"type": "image_url", "image_url": {"url": REF},
                        "role": "reference_image"}])
    assert "role" in str(ei.value)


def test_parse_content_rejects_duplicate_role_and_bad_types() -> None:
    with pytest.raises(InvalidParameterError):
        parse_content([{"type": "image_url", "image_url": {"url": REF}, "role": "first_frame"},
                       {"type": "image_url", "image_url": {"url": REF}, "role": "first_frame"}])
    with pytest.raises(InvalidParameterError):
        parse_content([{"type": "audio_url", "url": "x"}])
    with pytest.raises(InvalidParameterError):
        parse_content([])


# ---------------------------------------------------------------------------
# 模型路由
# ---------------------------------------------------------------------------


def test_default_family_route_matches_production_reference() -> None:
    """默认族三个槽位 = 参考实现生产代码的 23204 / 23218 / 23210。"""
    assert resolve_route(None, MODE_T2V)[0] == "23204"
    assert resolve_route(None, MODE_FIRST_FRAME)[0] == "23218"
    assert resolve_route(None, MODE_FIRST_LAST)[0] == "23210"


def test_explicit_upstream_id_is_used_verbatim() -> None:
    mid, cap, notes = resolve_route("23218", MODE_FIRST_FRAME)
    assert mid == "23218" and cap == "upstream:23218" and notes == []


def test_upstream_id_that_cannot_take_frames_fails_loudly() -> None:
    """23204 是文生模型 ⇒ 给框架图必须 400 并指出正确的族（**不静默换模型**）。"""
    with pytest.raises(InvalidParameterError) as ei:
        resolve_route("23204", MODE_FIRST_FRAME)
    msg = str(ei.value)
    assert "文生" in msg and "hailuo-video" in msg


def test_sora2_rejects_last_frame_because_only_one_image_allowed() -> None:
    with pytest.raises(InvalidParameterError) as ei:
        resolve_route("sora2", MODE_FIRST_LAST)
    assert "1 张" in str(ei.value) or "尾帧" in str(ei.value)


def test_family_without_t2v_slot_fails_with_suggestion() -> None:
    with pytest.raises(InvalidParameterError) as ei:
        resolve_route("hailuo-2.3-fast", MODE_T2V)
    assert "不支持文生" in str(ei.value)


def test_every_family_slot_points_at_a_registered_model() -> None:
    """注册表自洽门禁：族里写下的每个 modelID 都必须真的在表里。"""
    for f in VIDEO_FAMILIES:
        for mid in f.members():
            assert get_video_model(mid) is not None, f"{f.name} -> {mid} 未登记"


def test_frame_limit_comes_from_upstream_declarations() -> None:
    """🔴 框架图张数上限**全部来自上游声明**，不是经验值。

    判据优先级：逐模型 `maxSupportImageCount` > **组级 `end_frame` 声明** > 保守取 1。
    这条把"哪些族支持尾帧"钉死 —— 曾经 `hailuo-2.3` 的 pair 槽位错指向 `23217`，
    而 `Hailuo 2.3` 组**根本没声明 `end_frame`**（`23217` 实测也被 400 拦下）。
    """
    assert get_video_model("23210").max_frames() == 2        # 声明了 maxSupportImageCount=2
    assert get_video_model("23210").declared_end_frame is True   # Hailuo 2.0 组有 end_frame
    assert get_video_model("23217").max_frames() == 1        # Hailuo 2.3 组无 end_frame
    assert get_video_model("23217").declared_end_frame is False
    assert get_video_model("23218").max_frames() == 1        # Hailuo 2.3-Fast 组无 end_frame
    assert get_video_model("hailuo3.0-i2v").max_frames() == 9    # 逐模型声明优先
    assert get_video_model("hailuo_h3_max_i2v").max_frames() == 2
    assert get_video_model("veo3.1-i2v").max_frames() == 2
    #: 🔴 逐模型声明**优先于**组级声明：Sora 2 组有 end_frame，但只允许 1 张
    assert get_video_model("sora2-i2v").max_frames() == 1
    #: t2v 一律 0（它压根不收框架图）
    for mid in ("23204", "23200", "hailuo3.0-t2v", "23000"):
        assert get_video_model(mid).max_frames() == 0, mid


def test_families_without_end_frame_declaration_have_no_pair_slot() -> None:
    """没有尾帧能力的族**不登记 pair 槽位** ⇒ 给尾帧得到可执行的 400。"""
    for name in ("hailuo-2.3", "hailuo-2.3-fast", "hailuo-1.0-live",
                 "hailuo-1.0", "hailuo-1.0-director"):
        fam = get_family(name)
        assert fam is not None and fam.pair is None, f"{name} 不该有 pair 槽位"
        with pytest.raises(InvalidParameterError) as ei:
            resolve_route(name, MODE_FIRST_LAST)
        assert "尾帧" in str(ei.value)
    #: 有尾帧能力的族反过来必须有 pair 槽位
    for name in ("hailuo-video", "hailuo-2.0", "hailuo-3.0", "veo3.1"):
        assert get_family(name).pair, f"{name} 应当有 pair 槽位"


# ---------------------------------------------------------------------------
# 参数吸附（方向向下 + 必留痕）
# ---------------------------------------------------------------------------


def test_non_routable_models_are_rejected_with_a_reason() -> None:
    """🔴 上游存在但**本服务接不住**的模型（s2v / extend）必须 400。

    不加这道闸的话，`model="veo3.1-s2v"` 会被原样转发（多参考图语义）⇒
    一次**真实的失败计费**，而调用方以为只是换了个模型。

    ⚠️ 这条只在**运行期实读**把该模型读进表里时才走"接不住"分支；
    冻结快照下它压根不在表里 ⇒ 走"未知模型"。两条路径都要说清（下面各测一次）。
    """
    from app.upstream.hailuo.video_models import (
        FROZEN_VIDEO_SNAPSHOT,
        NON_ROUTABLE_MODELS,
        UpstreamVideoModel,
        install_runtime_video_models,
        is_routable,
    )

    #: ① 冻结快照：不在表里 ⇒ "未知 model"（但仍然列了可用能力名）
    with pytest.raises(InvalidParameterError) as ei:
        resolve_route("veo3.1-s2v", MODE_T2V)
    assert "未知 model" in str(ei.value) and "能力名" in str(ei.value)

    #: ② 运行期实读到它 ⇒ 走"接不住"，且**带上原因**
    extra = [UpstreamVideoModel(model_id=mid, family="Veo 3.1", kind="s2v",
                                modes=("image-reference",), max_images=3,
                                has_spec=True)
             for mid in NON_ROUTABLE_MODELS]
    install_runtime_video_models(list(FROZEN_VIDEO_SNAPSHOT) + extra)
    for mid, reason in NON_ROUTABLE_MODELS.items():
        assert not is_routable(mid)
        with pytest.raises(InvalidParameterError) as ei:
            resolve_route(mid, MODE_T2V)
        assert "接不住" in str(ei.value)
        assert re.search(r"[\u4e00-\u9fff]", reason), "原因必须是可读的中文说明"
    #: 正常模型不受影响
    assert is_routable("23218")


def test_catalog_marks_routable_flag() -> None:
    """`/v1/models` 的每一条都要带 `routable`；不可路由的要带原因。"""
    from app.upstream.hailuo.video_models import NON_ROUTABLE_MODELS, video_catalog

    catalog = {e["id"]: e for e in video_catalog()}
    assert catalog["23218"]["routable"] is True
    assert "routable_note" not in catalog["23218"]
    for mid in NON_ROUTABLE_MODELS:
        assert mid in catalog, f"{mid} 应当出现在清单里（带 routable:false）"
        assert catalog[mid]["routable"] is False
        assert catalog[mid]["routable_note"], "必须给出为什么接不住"
    #: 能力名条目不该被误标 routable
    assert catalog["hailuo-video"]["kind"] == "capability"


def test_duration_snaps_down_and_says_so() -> None:
    m = get_video_model("23218")           # durations = (6, 10)
    assert snap_duration(m, 6) == (6, [])
    value, notes = snap_duration(m, 8)
    assert value == 6 and "向下吸附" in notes[0]


def test_duration_below_every_option_is_loud() -> None:
    m = get_video_model("veo3.1-i2v")      # durations = (8,)
    value, notes = snap_duration(m, 3)
    assert value == 8 and "拉长" in notes[0]


def test_resolution_gate_blocks_frame_incapable_tier() -> None:
    """🔴 **实测逼出来的门禁**（2026-09-23）：`23210` 的 512 档没有声明
    `supportFrame`，拿它跑首尾帧 ⇒ 上游回 `code 2400001`（一次真实计费的次品请求）。
    ⇒ 带框架图时必须落到**明确声明**支持框架图的档位。"""
    from app.upstream.hailuo.video_models import snap_resolution

    m = get_video_model("23210")
    assert m.frame_capable_resolutions() == ("768", "1080")

    chosen, notes = snap_resolution(m, "512", require_frames=True)
    assert chosen == "768", "512 未声明 supportFrame ⇒ 必须改到 768"
    assert any("2400001" in n for n in notes), "改动必须留痕，并说清实测症状"

    #: 不带框架图时 512 照用（它确实是合法的便宜档）
    assert snap_resolution(m, "512", require_frames=False) == ("512", [])


def test_resolution_gate_rejects_explicitly_false_tier_without_alternative() -> None:
    """`23218` 的 1080 被**明确**声明 `supportFrame: false`，而它一档都没声明支持
    ⇒ 带框架图时**响亮 400**（不放行、也不悄悄换档）。"""
    from app.upstream.hailuo.video_models import snap_resolution

    m = get_video_model("23218")
    assert m.frame_capable_resolutions() == ()
    with pytest.raises(InvalidParameterError) as ei:
        snap_resolution(m, "1080", require_frames=True)
    assert "supportFrame" in str(ei.value)

    #: 未逐档声明的档位（768）放行，但要留痕说明"未校验"
    chosen, notes = snap_resolution(m, "768", require_frames=True)
    assert chosen == "768" and any("未经声明校验" in n for n in notes)


# ---------------------------------------------------------------------------
# 翻译层（build_video_plan）
# ---------------------------------------------------------------------------


def test_build_plan_translates_seedance_request(video_service) -> None:
    plan, cap, upstream = video_service.build_video_plan({
        "model": "hailuo-video",
        "content": [
            {"type": "text", "text": "a cat"},
            {"type": "image_url", "image_url": {"url": REF}},
        ],
        "resolution": "720p",
        "duration": 6,
        "ratio": "16:9",
    })
    assert upstream == "23218" and cap == "hailuo-video"
    #: 720p → 768（2.x 的档位是 768/1080）——必须留痕
    assert plan.resolution == "768"
    assert any("768" in n for n in plan.degradations)
    assert plan.duration == 6
    #: 2.x 未声明 aspectRatios ⇒ **不转发**，且留痕
    assert plan.aspect_ratio is None
    assert any("比例" in n for n in plan.degradations)
    assert plan.forecast_credits == 15.0


def test_build_plan_ratio_adaptive_maps_to_auto_on_hailuo3(video_service) -> None:
    plan, _c, upstream = video_service.build_video_plan({
        "model": "hailuo-3.0",
        "content": [{"type": "text", "text": "p"},
                    {"type": "image_url", "image_url": {"url": REF}}],
        "ratio": "adaptive",
    })
    assert upstream == "hailuo3.0-i2v"
    assert plan.aspect_ratio == "Auto"
    assert any("adaptive" in n for n in plan.degradations)


def test_build_plan_frames_convert_to_seconds(video_service) -> None:
    plan, _c, _m = video_service.build_video_plan({
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"}],
        "frames": 144,
    })
    assert plan.duration == 6
    assert any("24fps" in n for n in plan.degradations)


def test_build_plan_unsupported_fields_become_degradations(video_service) -> None:
    plan, _c, _m = video_service.build_video_plan({
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"}],
        "watermark": True, "seed": 7, "generate_audio": True,
        "callback_url": "https://x/y",
    })
    joined = " ".join(plan.degradations)
    for key in ("watermark", "seed", "generate_audio", "callback_url"):
        assert key in joined


def test_build_plan_unknown_field_is_rejected(video_service) -> None:
    with pytest.raises(InvalidParameterError):
        video_service.build_video_plan({
            "model": "hailuo-video",
            "content": [{"type": "text", "text": "p"}],
            "bogus_field": 1,
        })


def test_build_plan_rejects_last_frame_without_first_when_upstream_requires(
        video_service) -> None:
    """Veo 3.1 声明 `endFrameRequiredStartFrame` ⇒ 只给尾帧必须 400。"""
    with pytest.raises(InvalidParameterError) as ei:
        video_service.build_video_plan({
            "model": "veo3.1",
            "content": [{"type": "text", "text": "p"},
                        {"type": "image_url", "image_url": {"url": REF},
                         "role": "last_frame"}],
        })
    assert "endFrameRequiredStartFrame" in str(ei.value)


# ---------------------------------------------------------------------------
# 提交：发出去的 body 才是真相
# ---------------------------------------------------------------------------


def test_submit_sends_video_endpoint_with_frame_types(video_service, fake) -> None:
    """🔴 断言**实际出站的 body**：`frameType` 0=首帧 / 1=尾帧，顺序固定。"""
    for url in (REF, REF + "2"):
        fake.add_image(url, PNG_1PX)

    rec = video_service.create_video({
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"},
                    {"type": "image_url", "image_url": {"url": REF},
                     "role": "first_frame"},
                    {"type": "image_url", "image_url": {"url": REF + "2"},
                     "role": "last_frame"}],
        "duration": 6, "resolution": "1080p",
    }, credential=video_service.credential_of(FAKE_JWT))

    res = video_service.submit(rec["task_id"])
    assert res["submitted"]

    assert len(fake.video_create_calls) == 1
    body = fake.video_create_calls[0]
    param = body["parameter"]
    assert param["modelID"] == "23210"          # 首尾帧 ⇒ 2.0 那一档
    assert param["duration"] == 6
    assert param["resolution"] == "1080"
    assert [f["frameType"] for f in param["fileList"]] == [FRAME_TYPE_FIRST, FRAME_TYPE_LAST]
    #: 五键形态（真实抓包），**不含**图片链路那 12 键里的 characterID / assetFileType
    assert set(param["fileList"][0]) == {"id", "url", "name", "type", "frameType"}
    #: 🔴 逐字段对齐参考实现：`aspectRatio` **恒存在**（无比例时空串）、**不带 projectID**
    assert param["aspectRatio"] == ""           # 23210 未声明比例档位 ⇒ 空串
    assert "projectID" not in body

    #: 存储里记的是**真正的上游 batchID**（不是 data.id 那个记录 id）
    stored = video_service.store.get(rec["task_id"])
    assert stored["upstream_batch_id"] == res["upstream_batch_id"]
    assert stored["upstream_batch_id"].startswith("66812")


def test_submit_t2v_sends_empty_file_list(video_service, fake) -> None:
    rec = video_service.create_video({
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"}],
        "duration": 6,
    }, credential=video_service.credential_of(FAKE_JWT))
    video_service.submit(rec["task_id"])
    assert fake.video_create_calls[0]["parameter"]["fileList"] == []
    assert fake.video_create_calls[0]["parameter"]["modelID"] == "23204"


def test_dry_run_sends_nothing(video_service, fake) -> None:
    """🔴 计费动作的零消耗验证入口：body 全算出来，一个字节都不出网。"""
    rec = video_service.create_video({
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"}],
    }, credential=video_service.credential_of(FAKE_JWT))
    res = video_service.submit(rec["task_id"], dry_run=True)
    assert res["dry_run"] is True
    assert fake.video_create_calls == []


# ---------------------------------------------------------------------------
# 轮询：只用 v4，不碰 my/batch
# ---------------------------------------------------------------------------


def test_poll_uses_my_batch_video_feed(video_service, fake) -> None:
    """🔴 **主路径是 `my/batch`（`feedTypes=[0]`），不是 v4。**

    这是实测纠正过来的一条：v4 的 `batchType=0` 对视频批次恒回空（4 种 `type` 组合
    实测全空），而 `my/batch` 一次就能覆盖全部在途任务，产物在
    `metaInfo.videoMetaInfo.mediaInfo`。最初的实现照参考实现只走 v4，
    结果上游 2 分钟就出片、我们轮询 15 分钟没看见、最后被看门狗判 `expired` ——
    **钱花了、片出来了、没拿到**。
    """
    from app.coordinator import Coordinator

    rec = video_service.create_video({
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"}],
    }, credential=video_service.credential_of(FAKE_JWT))
    video_service.submit(rec["task_id"])
    batch_id = video_service.store.get(rec["task_id"])["upstream_batch_id"]

    coord = Coordinator(video_service, video_service.settings,
                        task_timeout=3600.0, poll_interval=0.0)
    before = fake.batch_calls
    coord.tick()
    assert fake.batch_calls == before + 1, "视频主路径必须打 my/batch"
    assert fake.v4_calls == [], "主路径命中时不该动用 v4 兜底"

    #: 上游尚未出片 ⇒ **保持非终态**（不是失败）
    assert video_service.store.get(rec["task_id"])["status"] == "in_progress"

    fake.set_video_feed(batch_id, status=ST_SUCCESS,
                        url="https://cdn.hailuoai.video/fake/out.mp4")
    coord.tick()
    full = video_service.store.get_full(rec["task_id"])
    assert full["status"] == "succeeded"
    assert full["result"]["video_url"] == "https://cdn.hailuoai.video/fake/out.mp4"
    assert full["result"]["width"] == 1364 and full["result"]["height"] == 768
    assert full["result"]["duration_ms"] == 5920


def test_record_id_is_stored_for_v4_fallback(video_service, fake) -> None:
    """`data.id`（记录 id）必须落库 —— v4 点名直查**只认它**，而它是唯一的兜底。"""
    rec = video_service.create_video({
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"}],
    }, credential=video_service.credential_of(FAKE_JWT))
    video_service.submit(rec["task_id"])
    row = video_service.store.get(rec["task_id"])
    assert row["upstream_feed_id"].startswith("66813"), "记录 id 要存进 upstream_feed_id"
    assert row["upstream_batch_id"].startswith("66812")
    assert row["upstream_feed_id"] != row["upstream_batch_id"], "两个 id 不是同一个值"


def test_poll_falls_back_to_v4_with_record_id(video_service, fake) -> None:
    """主窗口没覆盖到 ⇒ 用**记录 id** 走 v4 兜底，任务照样能收口。"""
    from app.coordinator import Coordinator

    rec = video_service.create_video({
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"}],
    }, credential=video_service.credential_of(FAKE_JWT))
    video_service.submit(rec["task_id"])
    row = video_service.store.get(rec["task_id"])
    record_id = row["upstream_feed_id"]

    #: 从 my/batch 窗口里抽走（模拟"被账号历史挤出最近 N 条"）
    fake.hide_video_from_batch(row["upstream_batch_id"])
    fake.v4_batches[record_id] = {
        "batchID": record_id, "status": ST_SUCCESS,
        "downloadURL": "https://cdn.hailuoai.video/fake/v4.mp4",
        "percent": None, "message": "", "createTime": 1789922888112}

    coord = Coordinator(video_service, video_service.settings,
                        task_timeout=3600.0, poll_interval=0.0)
    coord.tick()
    assert fake.v4_calls, "应当动用 v4 兜底"
    assert fake.v4_calls[-1]["batchInfoList"][0]["batchID"] == record_id, \
        "v4 必须按**记录 id** 查（拿 batchID 查恒回空）"
    full = video_service.store.get_full(rec["task_id"])
    assert full["status"] == "succeeded"
    assert full["result"]["video_url"] == "https://cdn.hailuoai.video/fake/v4.mp4"


# ---------------------------------------------------------------------------
# Seedance 响应形状
# ---------------------------------------------------------------------------


def test_video_view_shapes_are_native() -> None:
    record = {
        "task_id": "cgt-20260923004105-abcd123456",
        #: ⚠️ 这里要的是**本地**状态机取值（queued/in_progress/…），
        #: 不是上游那套数值状态（那是 `ST_CREATING=1`，别混）
        "status": "in_progress",
        "request": {"model": "hailuo-video", "ratio": "16:9"},
        "plan": {"duration": 6, "resolution": "768"},
        "result": None, "error": None, "degradations": [],
        "created_at": 1789922888.0, "updated_at": 1789922890.0,
    }
    code, body = video_view(record)
    assert code == 200 and body["status"] == "running"
    assert body["content"] == {"video_url": ""}
    assert body["duration"] == 6 and body["framespersecond"] == 24
    assert body["resolution"] == "768p" and body["ratio"] == "16:9"
    assert body["seed"] == -1


def test_video_view_marks_watchdog_timeout_as_expired() -> None:
    """看门狗超时 ≠ 上游判失败 —— Seedance 里这叫 expired。"""
    _code, body = video_view({
        "task_id": "cgt-1", "status": "failure", "request": {}, "plan": {},
        "result": {}, "error": {"code": "task_timeout", "message": "超时"},
        "degradations": [], "created_at": 1.0, "updated_at": 2.0,
    })
    assert body["status"] == "expired" and body["error"]["code"] == "task_timeout"


def test_video_view_failure_carries_native_error_and_200() -> None:
    code, body = video_view({
        "task_id": "cgt-1", "status": "failure", "request": {}, "plan": {},
        "result": {}, "error": {"code": "content_policy_violation", "message": "敏感"},
        "degradations": [], "created_at": 1.0, "updated_at": 2.0,
    })
    assert code == 200 and body["status"] == "failed"
    assert body["error"]["code"] == "content_policy_violation"


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------


def test_post_returns_only_id(video_client) -> None:
    r = video_client.post("/api/v3/contents/generations/tasks", headers=AUTH, json={
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"}],
        "duration": 6,
    })
    assert r.status_code == 200
    assert set(r.json()) == {"id"}
    assert r.json()["id"].startswith("cgt-")
    assert r.headers["location"].endswith(r.json()["id"])


def test_post_requires_credential(video_client) -> None:
    r = video_client.post("/api/v3/contents/generations/tasks", json={
        "model": "hailuo-video", "content": [{"type": "text", "text": "p"}]})
    assert r.status_code == 401


def test_get_single_task_needs_no_auth(video_client, video_service) -> None:
    rec = video_service.create_video({
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"}],
    }, credential=video_service.credential_of(FAKE_JWT))
    r = video_client.get(f"/api/v3/contents/generations/tasks/{rec['task_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == rec["task_id"] and body["status"] == "queued"


def test_get_unknown_task_is_404(video_client) -> None:
    r = video_client.get("/api/v3/contents/generations/tasks/cgt-nope")
    assert r.status_code == 404


def test_list_requires_auth_by_default(video_client) -> None:
    """🔴 列表会把任务 id 交给未带凭据的人，而 id 就是读接口的凭据 ⇒ 默认拦住。"""
    r = video_client.get("/api/v3/contents/generations/tasks")
    assert r.status_code == 401


def test_list_returns_native_envelope_with_credential(video_client, video_service) -> None:
    video_service.create_video({
        "model": "hailuo-video", "content": [{"type": "text", "text": "p"}],
    }, credential=video_service.credential_of(FAKE_JWT))
    r = video_client.get("/api/v3/contents/generations/tasks", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "total", "page_num", "page_size"}
    assert body["total"] == 1 and body["items"][0]["status"] == "queued"


def test_delete_running_task_fails_loudly(video_client, video_service) -> None:
    rec = video_service.create_video({
        "model": "hailuo-video", "content": [{"type": "text", "text": "p"}],
    }, credential=video_service.credential_of(FAKE_JWT))
    r = video_client.delete(f"/api/v3/contents/generations/tasks/{rec['task_id']}",
                            headers=AUTH)
    assert r.status_code == 400
    assert "取消" in r.json()["error"]["message"] or "终态" in r.json()["error"]["message"]


def test_v1_models_is_openai_shaped_and_lists_video_plus_image(video_client) -> None:
    r = video_client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = {d["id"] for d in body["data"]}
    #: 视频能力名 + 视频上游模型 + 图片模型
    assert {"hailuo-video", "hailuo-2.3", "23218", "veo3.1-i2v"} <= ids
    assert "nano_banana21_flash" in ids
    sample = next(d for d in body["data"] if d["id"] == "hailuo-video")
    assert set(sample) >= {"id", "object", "created", "owned_by"}
    assert sample["object"] == "model"


def test_video_tasks_do_not_leak_into_image_listing(client, video_service) -> None:
    """两张表 ⇒ 图片列表里绝不会冒出视频任务（反之亦然）。"""
    video_service.create_video({
        "model": "hailuo-video", "content": [{"type": "text", "text": "p"}],
    }, credential=video_service.credential_of(FAKE_JWT))
    r = client.get("/async/v1/images/generations", headers=AUTH)
    assert r.status_code == 200 and r.json()["data"] == []


def test_two_coordinators_do_not_starve_each_other(service, video_service, fake) -> None:
    """🔴 **图片与视频协调器必须各有一把选主锁。**

    这是**真实端到端实测**才暴露出来的缺陷：两个协调器共用一个租约行
    （`leader_key="coordinator"`）⇒ 先启动的那个把另一个**永久饿死** ——
    视频任务恒停在 `queued`、`attempts=0`，而日志上一切正常（两个协调器都
    打印了"已启动"）。单测原先碰不到，因为每个用例只建一个协调器。
    """
    from app.coordinator import Coordinator

    image_coord = Coordinator(service, service.settings)
    video_coord = Coordinator(video_service, video_service.settings,
                              task_timeout=video_service.settings.video_task_timeout,
                              poll_interval=0.0, leader_key="coordinator-video")

    #: 图片协调器先抢锁（也是真实启动顺序）
    image_coord.tick()
    assert image_coord.stats.leader_skips == 0

    rec = video_service.create_video({
        "model": "hailuo-video",
        "content": [{"type": "text", "text": "p"}],
    }, credential=video_service.credential_of(FAKE_JWT))

    video_coord.tick()
    assert video_coord.stats.leader_skips == 0, "视频协调器不该被图片的锁饿死"
    assert video_coord.stats.submitted == 1
    assert fake.video_create_calls, "🔴 真的建到上游了才算没被饿死"
    assert video_service.store.get(rec["task_id"])["status"] == "in_progress"


def test_expired_video_task_uses_video_timeout(video_service, fake) -> None:
    """`VIDEO_TASK_TIMEOUT` 必须**真被消费**（否则是假配置）。"""
    import time as _t

    from app.coordinator import Coordinator

    rec = video_service.create_video({
        "model": "hailuo-video", "content": [{"type": "text", "text": "p"}],
    }, credential=video_service.credential_of(FAKE_JWT))
    with video_service.store.session() as s:
        from app.store import VideoTaskRow
        s.query(VideoTaskRow).filter_by(task_id=rec["task_id"]).update(
            {"created_at": _t.time() - 5000})

    coord = Coordinator(video_service, video_service.settings,
                        task_timeout=video_service.settings.video_task_timeout,
                        poll_interval=0.0)
    coord.tick()
    full = video_service.store.get_full(rec["task_id"])
    assert full["status"] == "failure"
    assert full["error"]["code"] == "task_timeout"
    #: 3600s 的看门狗**不该**收掉一个 5000s 前创建的任务？——它该收：
    #: 5000 > 3600。这条断言同时证明用的是 video 的超时（图片的 900 会收得更早）。
    assert full["status"] == "failure"
