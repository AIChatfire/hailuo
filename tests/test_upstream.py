#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上游适配层：客户端（信封 / 状态 / dry_run）+ 上传（OSS 签名 / fileList）+ 能力表解析。

**全部走 `httpx.MockTransport`** —— 零真实上游调用，且不需要数据库。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.errors import (
    AuthError,
    ContentPolicyError,
    InvalidParameterError,
    RiskControlChallenge,
    UpstreamError,
    UpstreamQuotaExhausted,
    UpstreamRateLimited,
)
from app.upstream.hailuo import capabilities as caps
from app.upstream.hailuo import sign
from app.upstream.hailuo import upload as up
from app.upstream.hailuo.client import (
    FAILED,
    IN_PROGRESS,
    SUCCEEDED,
    Feed,
    HailuoClient,
    parse_batches,
    parse_feed,
    parse_v4_batches,
    status_name,
)

from .conftest import PNG_1PX, FakeHailuo

DEVICE = sign.CAPTURE_DEVICE | {
    "lang": "zh-Intl", "os_name": "Mac", "browser_name": "chrome",
    "device_memory": 32, "cpu_core_num": 10, "browser_language": "zh-CN",
    "browser_platform": "MacIntel", "screen_width": 2560, "screen_height": 1440,
}


def _client(fake: FakeHailuo) -> HailuoClient:
    return HailuoClient(token="tok", base_url="https://hailuoai.video",
                        device=DEVICE, transport=fake.transport())


# ---------------------------------------------------------------------------
# 信封与错误映射
# ---------------------------------------------------------------------------


def test_ok_envelope_returns_data(fake) -> None:
    data, trace = _client(fake).fetch_processing()
    assert isinstance(data, dict) and "processing" in data
    assert trace["http_status"] == 200
    #: 成功时**不写** `upstream_code` —— 它只在真的拿到非 0 code 时才出现
    assert "upstream_code" not in trace


def test_http_401_maps_to_auth_error(fake) -> None:
    fake.force_error["/api/feed/creation/my/processing"] = httpx.Response(
        401, json={"statusInfo": {"code": 1, "message": "未登录"}})
    with pytest.raises(AuthError):
        _client(fake).fetch_processing()


def test_http_429_maps_to_rate_limited_with_retry_after(fake) -> None:
    fake.force_error["/api/feed/creation/my/processing"] = httpx.Response(
        429, headers={"Retry-After": "12"}, json={"statusInfo": {"code": 7, "message": "频繁"}})
    with pytest.raises(UpstreamRateLimited) as ei:
        _client(fake).fetch_processing()
    assert ei.value.retry_after == 12.0
    assert ei.value.status_code == 429


def test_http_429_without_header_gives_no_retry_after(fake) -> None:
    """上游**没说**多久 ⇒ 不给 `Retry-After` —— 编一个数字等于伪造事实。"""
    fake.force_error["/api/feed/creation/my/processing"] = httpx.Response(
        429, json={"statusInfo": {"code": 7, "message": "频繁"}})
    with pytest.raises(UpstreamRateLimited) as ei:
        _client(fake).fetch_processing()
    assert ei.value.retry_after is None


def test_http_500_maps_to_upstream_error(fake) -> None:
    fake.force_error["/api/feed/creation/my/processing"] = httpx.Response(502, text="bad gw")
    with pytest.raises(UpstreamError):
        _client(fake).fetch_processing()


def test_non_json_maps_to_upstream_error(fake) -> None:
    """WAF 页 / 裸 HTML ⇒ UpstreamError（不是"任务失败"）。"""
    fake.force_error["/api/feed/creation/my/processing"] = httpx.Response(
        200, text="<html>captcha</html>")
    with pytest.raises(UpstreamError) as ei:
        _client(fake).fetch_processing()
    assert "非 JSON" in str(ei.value)


@pytest.mark.parametrize(("code", "message", "expected"), [
    (2, "请求异常，请检查请求参数", InvalidParameterError),
    (1001, "请先登录", AuthError),
    (30, "积分不足，请充值", UpstreamQuotaExhausted),
    #: 🔴 视频侧的余额叫**贝壳**（图片侧说"积分"），且文案里**没有**"积分/额度/余额"
    #: 任何一个词 —— 2026-09-23 实测建任务回 `code=2200005 贝壳不足`。
    #: 只按文案匹配会漏成通用 upstream_error，调用方拿不到"充值"这个可执行结论。
    (2200005, "贝壳不足", UpstreamQuotaExhausted),
    (31, "操作过于频繁，请稍后再试", RiskControlChallenge),
    (32, "内容包含敏感信息", ContentPolicyError),
    (99, "内部错误", UpstreamError),
])
def test_business_code_mapping(fake, code, message, expected) -> None:
    """🔴 上游**大量业务错误走 HTTP 200 + `code != 0`** ⇒ 只看 HTTP 状态会把失败当成功。"""
    fake.force_error["/api/feed/creation/my/processing"] = httpx.Response(
        200, json={"statusInfo": {"code": code, "message": message}})
    with pytest.raises(expected):
        _client(fake).fetch_processing()


def test_quota_error_tells_the_caller_retry_is_useless(fake) -> None:
    """额度不足是**账户级**结论 ⇒ 必须在错误文案里说清"重试无用"。"""
    from app.errors import UpstreamQuotaExhausted

    fake.force_error["/api/feed/creation/my/processing"] = httpx.Response(
        200, json={"statusInfo": {"code": 2200005, "message": "贝壳不足"}})
    with pytest.raises(UpstreamQuotaExhausted) as ei:
        _client(fake).fetch_processing()
    assert "重试无用" in str(ei.value) and "贝壳" in str(ei.value)


# ---------------------------------------------------------------------------
# 签名接线
# ---------------------------------------------------------------------------


def test_request_signs_with_the_same_unix_as_query(fake) -> None:
    """`unix`、签名里的 `time`、请求体必须是**同一轮**算出来的。

    检查方式：拦下真实发出的请求，用**它自己的 URL 与 body** 复算 `yy`。
    不一致就会立刻暴露"查询串用了 A 时刻、签名用了 B 时刻"这类竞态。
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["yy"] = request.headers["yy"]
        captured["token"] = request.headers["token"]
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"statusInfo": {"code": 0}, "data": {}})

    c = HailuoClient(token="tok", base_url="https://hailuoai.video", device=DEVICE,
                     transport=httpx.MockTransport(handler))
    c.fetch_processing()

    url = str(captured["url"])
    path_with_query = url.replace("https://hailuoai.video", "")
    unix = int(path_with_query.split("unix=")[1].split("&")[0])
    assert unix % 1000 == 0, "unix 必须是整秒毫秒值"
    assert sign.sign_yy(path_with_query=path_with_query,
                        body_json=str(captured["body"]), time_ms=unix,
                        method="POST") == captured["yy"]
    assert captured["token"] == "tok"


def test_get_requests_omit_token_when_not_signed(fake) -> None:
    """能力表那两个公开端点是 **GET 且免鉴权**（不带 token / yy）。"""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        import asyncio

        asyncio.run(caps.fetch_models(c))

    assert seen, "应当至少发了一次请求"
    for headers in seen:
        assert "yy" not in {k.lower() for k in headers}


def test_dry_run_sends_nothing_and_returns_trace(fake) -> None:
    """`dry_run` 是"零消耗验证翻译层"的唯一入口。"""
    before_create = len(fake.create_calls)
    batch_id, trace = _client(fake).create_image(
        model_id="nano_banana21_flash", desc="a cat", file_list=[], quantity=1,
        resolution="4K", dry_run=True)
    assert batch_id == ""
    assert trace["dry_run"] is True and trace["sent"] is False
    assert len(fake.create_calls) == before_create
    # 但请求体确实算出来了（可断言翻译层）
    assert json.loads(trace["request_body"])["parameter"]["desc"] == "a cat"
    assert trace["yy"]


def test_create_returns_batch_id_and_traces_it(fake) -> None:
    batch_id, trace = _client(fake).create_image(
        model_id="nano_banana21_flash", desc="a cat", file_list=[], quantity=1)
    assert batch_id and trace["upstream_submit_id"] == batch_id
    # 🔴 **两个 id 必须分清**（实测教训）：`data.id` 是 feed 记录 id，
    # `data.task.batchID` 才是轮询句柄 —— 拿错 ⇒ my/batch 永远对不上。
    assert trace["upstream_record_id"]
    assert trace["upstream_record_id"] != batch_id


def test_create_without_id_is_upstream_error(fake) -> None:
    fake.force_error["/v2/api/multimodal/generate/image"] = httpx.Response(
        200, json={"statusInfo": {"code": 0}, "data": {"isFirstGenerate": True}})
    with pytest.raises(UpstreamError) as ei:
        _client(fake).create_image(model_id="m", desc="d", file_list=[])
    assert "batch id" in str(ei.value)


def test_create_body_omits_empty_optional_fields(fake) -> None:
    """空字段**省掉**而不是传 `null` —— 抓包里 p2 就是这样。

    传 `null` 的上游行为未取证；"省略"是抓包常态 ⇒ 走有证据的那条路。
    """
    _client(fake).create_image(model_id="nano_banana21_flash", desc="a cat",
                               file_list=[], quantity=1)
    p = fake.create_calls[0]["parameter"]
    assert "quality" not in p
    assert "referenceMode" not in p
    assert "aspectRatio" not in p
    assert p["useOriginPrompt"] is True


def test_token_never_appears_in_trace(fake) -> None:
    """🔴 **凭据不上报** —— 由实现约束保证（token 只进请求头）。
    并且 `http_path` **丢掉整个 query**（设备指纹不进埋点）。"""
    _, trace = _client(fake).fetch_processing()
    blob = json.dumps(trace, ensure_ascii=False)
    assert "tok" not in blob
    assert "?" not in trace["http_path"], "上游埋点只给 pathname"
    assert "unix" not in blob


def test_probe_token_reports_failure_without_raising(fake) -> None:
    fake.force_error["/api/feed/creation/my/processing"] = httpx.Response(401)
    ok, why = _client(fake).probe_token()
    assert ok is False and "AuthError" in why


# ---------------------------------------------------------------------------
# 连接级重试（2026-09-21 实测：冷池并发建连会被掐）
# ---------------------------------------------------------------------------


def test_idempotent_query_retries_connection_failure(fake) -> None:
    """🔴 幂等请求（查询/取凭据）遇**连接级失败**要重试 —— 实测冷池下并发建连
    约 1/3 概率被掐（`RemoteProtocolError`），重试一次即可越过。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without response")
        return fake.handler(request)

    c = HailuoClient(token="tok", base_url="https://hailuoai.video", device=DEVICE,
                     transport=httpx.MockTransport(handler))
    data, trace = c.fetch_processing()
    assert calls["n"] == 2, "第一次连接失败后必须重试"
    assert trace["retry_attempts"] == 2
    assert "processing" in data


def test_billing_call_never_retries_connection_failure(fake) -> None:
    """🔴 **建任务绝不重试"已送达但响应丢失"** —— 无法排除上游已建任务，重试=重复扣费。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.RemoteProtocolError("Server disconnected without response")

    c = HailuoClient(token="tok", base_url="https://hailuoai.video", device=DEVICE,
                     transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError):
        c.create_image(model_id="nano_banana21_flash", desc="a cat", file_list=[])
    assert calls["n"] == 1, "计费调用必须只发一次"


def test_billing_call_retries_pre_send_connect_failure(fake) -> None:
    """🔴 但"**连接根本没建立**"（`ConnectError`）对**任何**请求都可重试 ——
    请求不可能送达上游 ⇒ 建任务也不可能被创建 ⇒ 重试零计费风险。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection refused")
        return fake.handler(request)

    c = HailuoClient(token="tok", base_url="https://hailuoai.video", device=DEVICE,
                     transport=httpx.MockTransport(handler))
    batch_id, trace = c.create_image(model_id="nano_banana21_flash",
                                     desc="a cat", file_list=[])
    assert calls["n"] == 3, "建连失败（未送达）必须重试"
    assert batch_id and trace["retry_attempts"] == 3


def test_oss_put_retries_connection_failure(fake) -> None:
    """OSS PUT 幂等（同 key 同内容 ⇒ 覆盖写）⇒ 连接失败可重试。"""
    states = {"n": 0}
    base = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("hailuo-video."):
            states["n"] += 1
            if states["n"] == 1:
                raise httpx.ConnectError("connection reset")
        return base(request)

    client = HailuoClient(token="tok", base_url="https://hailuoai.video", device=DEVICE,
                          transport=fake.transport())
    uploader = up.Uploader(client=client,
                           oss_client=httpx.Client(transport=httpx.MockTransport(handler)))
    uploaded, traces = uploader.upload_bytes(content=PNG_1PX, mime="image/png",
                                             name="a.png")
    assert uploaded.file_id
    assert states["n"] == 2, "OSS 首次连接失败后必须重试"
    assert any(t.get("oss_retry_attempts") == 1 for t in traces)


# ---------------------------------------------------------------------------
# 结果解析
# ---------------------------------------------------------------------------


def test_parse_feed_prefers_no_watermark_url() -> None:
    """产物有两条 URL ⇒ 默认给**去水印**那条（调用方拿图是要用的）。"""
    raw = {
        "feedType": 1,
        "commonInfo": {"id": "f1", "batchID": "b1", "createTime": 1789922888112, "status": 2},
        "feedMessage": {"message": ""},
        "modelParameter": {"imageParameter": {"modelID": "nano_banana21_flash",
                                             "desc": "a cat", "fileList": []}},
        "metaInfo": {"imageMetaInfo": {"mediaInfo": {
            "url": "https://cdn/watermarked.png",
            "downloadURL": {"watermarkURL": "https://cdn/watermarked.png",
                            "withoutWatermarkURL": "https://cdn/clean.png",
                            "fileName": "Hailuo_Image_a cat", "fileID": "9"},
            "width": 4096, "height": 4096}}},
    }
    feed = parse_feed(raw)
    assert feed.url == "https://cdn/clean.png"
    assert feed.url_no_watermark == "https://cdn/clean.png"
    assert feed.status == 2 and feed.feed_type == 1
    assert feed.width == 4096 and feed.model_id == "nano_banana21_flash"
    assert feed.is_succeeded and not feed.is_in_progress and not feed.is_failed
    # 原始报文保留（trace 可查）
    assert feed.raw is raw


def test_parse_feed_falls_back_to_cover_when_no_media() -> None:
    raw = {"feedType": 1, "commonInfo": {"id": "f", "status": 2},
           "feedCoverInfo": {"coverURL": "https://cdn/cover.png"}}
    assert parse_feed(raw).url == "https://cdn/cover.png"


def test_parse_batches_groups_by_batch() -> None:
    payload = {"data": {"batchFeeds": [
        {"batchID": "b1", "feeds": [{"feedType": 1, "commonInfo": {"id": "f1", "status": 2}}]},
        {"batchID": "b2", "feeds": [{"feedType": 1, "commonInfo": {"id": "f2", "status": 1}},
                                    {"feedType": 1, "commonInfo": {"id": "f3", "status": 1}}]},
    ]}}
    out = parse_batches(payload)
    assert [b for b, _ in out] == ["b1", "b2"]
    assert len(out[1][1]) == 2


def test_status_sets_match_frontend_enums() -> None:
    """状态集合逐字对齐前端 chunk `9206`：`xE` / `fV` / `R`。"""
    assert SUCCEEDED == {2, 10}
    assert IN_PROGRESS == {1, 11, 12, 6, 16, 8}
    assert set(FAILED) == {3, 5, 14, 7, 9}
    assert status_name(2) == "SUCCESS" and status_name(3) == "Fail"
    assert status_name(999) == "UNKNOWN(999)"


def test_feed_failure_message_is_actionable() -> None:
    feed = Feed(feed_id="f", batch_id="b", status=5, feed_type=1, create_time=1)
    code, message = feed.failure()  # type: ignore[misc]
    assert code == "content_policy_violation"
    assert "换 prompt" in message


def test_failed_status_3_warns_about_billing() -> None:
    """失败**不等于没花钱** —— 文案必须说出来。"""
    code, message = FAILED[3]
    assert code == "task_failed"
    assert "计费" in message or "花钱" in message


# ---------------------------------------------------------------------------
# v4 按 id 直查（图片与视频共用端点，两种 body 形态）
# ---------------------------------------------------------------------------


def test_fetch_by_ids_image_body_matches_reference_captures(fake) -> None:
    """图片形态（MeUtils `images.get_task`，生产在跑）：
    `{"batchInfoList":[{"batchID":…,"batchType":1}]}` —— 顶层**不带** `type`。"""
    fake.set_v4("b1", status=2, url="https://cdn/x.png")
    batches, trace = _client(fake).fetch_by_ids(["b1", ""])
    body = fake.v4_calls[0]
    assert body == {"batchInfoList": [{"batchID": "b1", "batchType": 1}]}
    assert "type" not in body, "图片查询不带顶层 type（对齐参考实现）"
    assert trace["requested_ids"] == 1, "空 id 必须被滤掉"
    assert [b for b, _ in batches] == ["b1"]


def test_fetch_by_ids_video_body_includes_type_field(fake) -> None:
    """视频形态（MeUtils `openai_videos.get_task`，内嵌真实抓包）：
    `{"batchInfoList":[…,"batchType":0],"type":1}` —— 顶层**带** `"type":1`。"""
    from app.upstream.hailuo.client import BATCH_TYPE_VIDEO

    fake.set_v4("v1", status=2, url="https://cdn/v.mp4")
    batches, _ = _client(fake).fetch_by_ids(["v1"], batch_type=BATCH_TYPE_VIDEO)
    body = fake.v4_calls[0]
    assert body["batchInfoList"] == [{"batchID": "v1", "batchType": 0}]
    assert body["type"] == 1, "视频查询带顶层 type=1（对齐抓包注释）"
    assert [b for b, _ in batches] == ["v1"]


def test_parse_v4_batches_reads_string_download_url() -> None:
    """v4 的 `downloadURL` 是**字符串直链**（实测=去水印版）—— 与 `my/batch` 的字典形态同名不同型。
    asset 顶层还带 `id/modelID/width/height/fileID`，全部照实_lift。"""
    payload = {"data": {"batchVideos": [{"batchID": "b1", "batchType": 1, "assets": [
        {"id": "feed-9", "batchID": "b1", "status": 2,
         "downloadURL": "https://cdn/direct.png", "percent": None,
         "createTime": 1789922888112, "desc": "a cat", "message": None,
         "modelID": "nano_banana21_flash", "fileID": "f-1",
         "width": 1408, "height": 768},
    ]}]}}
    (batch_id, feeds), = parse_v4_batches(payload)
    assert batch_id == "b1"
    f = feeds[0]
    assert f.url == "https://cdn/direct.png"
    assert f.url_no_watermark == "https://cdn/direct.png"
    assert f.status == 2 and f.percent is None and f.desc == "a cat"
    assert f.feed_id == "feed-9" and f.model_id == "nano_banana21_flash"
    assert f.file_id == "f-1" and f.width == 1408 and f.height == 768
    assert f.is_succeeded and not f.is_failed
    assert f.create_time == 1789922888112


def test_parse_v4_batches_defends_dict_download_url() -> None:
    """万一上游回的是 `my/batch` 那种**字典** downloadURL —— 去水印优先，不炸。"""
    payload = {"data": {"batchVideos": [{"batchID": "b1", "assets": [
        {"status": 2,
         "downloadURL": {"watermarkURL": "https://cdn/wm.png",
                         "withoutWatermarkURL": "https://cdn/clean.png"}},
    ]}]}}
    (_, feeds), = parse_v4_batches(payload)
    assert feeds[0].url == "https://cdn/clean.png"
    assert feeds[0].url_no_watermark == "https://cdn/clean.png"


def test_parse_v4_batches_takes_first_nonempty_container() -> None:
    """容器名只对视频有抓包证据（batchVideos）；图片形态未取证 ⇒
    扫三个候选取第一个非空，**不编造第四种**。"""
    payload = {"data": {"batchImages": [{"batchID": "i1", "assets": [
        {"status": 1, "downloadURL": "", "percent": 42}]}]}}
    (batch_id, feeds), = parse_v4_batches(payload)
    assert batch_id == "i1"
    assert feeds[0].status == 1 and feeds[0].percent == 42
    assert feeds[0].is_in_progress


def test_parse_v4_batches_empty_payload_gives_nothing() -> None:
    assert parse_v4_batches({}) == []
    assert parse_v4_batches({"data": {}}) == []
    assert parse_v4_batches({"data": {"batchVideos": []}}) == []


def test_fetch_by_ids_empty_ids_never_hits_network(fake) -> None:
    """空请求不出网 —— 一个字节都不该为"没东西可查"花掉。"""
    batches, trace = _client(fake).fetch_by_ids([])
    assert batches == [] and fake.v4_calls == []
    assert trace["skipped"] == "no_ids"


def test_fetch_by_ids_dry_run_builds_body_without_sending(fake) -> None:
    batches, trace = _client(fake).fetch_by_ids(["b1"], dry_run=True)
    assert batches == [] and fake.v4_calls == []
    assert trace["dry_run"] is True and trace["sent"] is False
    body = json.loads(trace["request_body"])
    assert body["batchInfoList"] == [{"batchID": "b1", "batchType": 1}]


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


def test_oss_signature_is_deterministic_and_covers_security_token() -> None:
    kwargs = dict(access_key_secret="sec", method="PUT", content_md5="md5",
                  content_type="image/png", date="Wed, 01 Jan 2030 00:00:00 GMT",
                  security_token="TOK", bucket="hailuo-video",
                  object_key="dir/f.png")
    a = up.oss_signature(**kwargs)
    b = up.oss_signature(**kwargs)
    assert a == b
    # security token 必须参与签名：换掉它签名就得变
    assert up.oss_signature(**{**kwargs, "security_token": "OTHER"}) != a


def test_uploaded_file_entry_matches_captured_filelist_shape() -> None:
    """`fileList[]` 一项的字段集**逐字段**对齐抓包（缺字段上游就不认参考图）。"""
    uf = up.UploadedFile(file_id="123", url="https://cdn/x.png", name="a.png",
                         file_type="png")
    entry = uf.to_file_list_entry()
    assert entry["id"] == "123"
    assert entry["url"] == "https://cdn/x.png" and entry["coverUrl"] == "https://cdn/x.png"
    assert entry["frameType"] == 3
    assert entry["referenceType"] == 0
    assert entry["assetFileType"] == 1
    assert entry["duration"] == 0 and entry["durationMs"] == 0
    assert entry["characterID"] == "" and entry["characterUrl"] == "" and entry["videoID"] == ""


def test_upload_full_flow_and_cache(fake) -> None:
    client = _client(fake)
    oss = httpx.Client(transport=fake.transport())
    uploader = up.Uploader(client=client, oss_client=oss, cache_ttl=60.0)

    first, traces = uploader.upload_bytes(content=PNG_1PX, mime="image/png", name="a.png")
    assert first.file_id and not first.from_cache
    assert [t["stage"] for t in traces] == ["request_policy", "oss_put", "policy_callback"]
    # 回调体逐字段对齐抓包
    body = fake.callback_calls[0]
    assert body["fileScene"] == 10
    assert body["assetFileType"] == 1
    assert body["durationMs"] == 0
    assert body["mimeType"] == "png"
    assert body["fileName"].endswith(".png") and body["originFileName"] == "a.png"
    assert body["dir"].startswith("moss/prod/")
    assert body["endpoint"] == "oss-us-east-1.aliyuncs.com"
    assert body["bucketName"] == "hailuo-video"
    assert body["size"] == str(len(PNG_1PX))

    second, traces2 = uploader.upload_bytes(content=PNG_1PX, mime="image/png", name="a.png")
    assert second.from_cache is True
    assert second.file_id == first.file_id
    assert traces2[0]["stage"] == "upload_cache_hit"
    assert fake.policy_calls == 1, "缓存命中不该再取上传凭据"


def test_upload_dry_run_writes_nothing(fake) -> None:
    uploader = up.Uploader(client=_client(fake),
                           oss_client=httpx.Client(transport=fake.transport()))
    uf, traces = uploader.upload_bytes(content=PNG_1PX, mime="image/png",
                                       name="a.png", dry_run=True)
    assert uf.file_id == "<dry-run-file-id>"
    assert fake.policy_calls == 0 and fake.callback_calls == []


def test_upload_without_fileid_is_error(fake) -> None:
    fake.force_error["/v1/api/files/policy_callback"] = httpx.Response(
        200, json={"statusInfo": {"code": 0}, "data": {}})
    uploader = up.Uploader(client=_client(fake),
                           oss_client=httpx.Client(transport=fake.transport()))
    with pytest.raises(UpstreamError) as ei:
        uploader.upload_bytes(content=PNG_1PX, mime="image/png", name="a.png")
    assert "fileID" in str(ei.value)


def test_missing_policy_field_is_error(fake) -> None:
    fake.force_error["/v1/api/files/request_policy"] = httpx.Response(
        200, json={"statusInfo": {"code": 0}, "data": {"accessKeyId": "x"}})
    uploader = up.Uploader(client=_client(fake),
                           oss_client=httpx.Client(transport=fake.transport()))
    with pytest.raises(UpstreamError) as ei:
        uploader.upload_bytes(content=PNG_1PX, mime="image/png", name="a.png")
    assert "accessKeySecret" in str(ei.value)


def test_oss_failure_is_error(fake) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("hailuo-video."):
            return httpx.Response(403, text="SignatureDoesNotMatch")
        return fake.handler(request)

    uploader = up.Uploader(client=_client(fake),
                           oss_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(UpstreamError) as ei:
        uploader.upload_bytes(content=PNG_1PX, mime="image/png", name="a.png")
    assert "OSS 上传失败" in str(ei.value)


def test_upload_cache_purge(fake) -> None:
    uploader = up.Uploader(client=_client(fake),
                           oss_client=httpx.Client(transport=fake.transport()),
                           cache_ttl=0.0)
    uploader.upload_bytes(content=PNG_1PX, mime="image/png", name="a.png")
    assert uploader.cache_stats()["alive"] == 0
    assert uploader.purge_expired() == 1


# ---------------------------------------------------------------------------
# 能力表解析
# ---------------------------------------------------------------------------


def test_parse_common_config_extracts_limits() -> None:
    payload = {"data": {"create_image_models": {"models": [{
        "modelKey": "GPT Image 1.5", "filterTags": ["edit"],
        "modelList": [{"id": "gpt-image-1.5", "mode": "image-reference",
                       "maxSupportImageCount": 3, "maxPromptLength": 2000}],
    }]}}}
    out = caps.parse_common_config(payload)
    assert out["gpt-image-1.5"] == {"display_name": "GPT Image 1.5", "max_images": 3,
                                    "max_prompt": 2000, "tags": ("edit",),
                                    "mode": "image-reference"}


def test_parse_model_info_extracts_enums_and_costs() -> None:
    payload = {"data": {"videoModels": [{"modelID": "23000"}], "imageModels": [{
        "modelID": "nano_banana21_flash",
        "parameter": {
            "resolutions": [{"value": "1K", "defaultSelect": True},
                            {"value": "4K", "defaultSelect": False}],
            "aspectRatios": [{"value": "Auto", "defaultSelect": True}],
        },
        "costs": [{"resolutions": ["1K"], "realCost": 4}], "defaultCost": 0,
    }]}}
    out = caps.parse_model_info(payload)
    assert set(out) == {"nano_banana21_flash"}, "只取 imageModels（本服务不做视频）"
    entry = out["nano_banana21_flash"]
    assert entry["resolutions"] == ("1K", "4K")
    assert entry["default_resolutions"] == ("1K",)
    assert entry["aspect_ratios"] == ("Auto",)
    assert entry["default_aspect_ratios"] == ("Auto",)
    assert entry["costs"][0]["realCost"] == 4


def test_merge_keeps_union_and_invents_nothing() -> None:
    """两边键取并集；缺的字段留空，**不编造**。"""
    meta = {"a": {"display_name": "A", "max_images": 3, "max_prompt": None,
                  "tags": (), "mode": "image-reference"}}
    info = {"b": {"resolutions": ("1K",), "default_resolutions": ("1K",),
                  "aspect_ratios": ("Auto",), "default_aspect_ratios": ("Auto",),
                  "costs": (), "default_cost": 1}}
    merged = {m.model_id: m for m in caps.merge(meta, info)}
    assert set(merged) == {"a", "b"}
    assert merged["a"].resolutions == () and merged["a"].max_support_image_count == 3
    assert merged["b"].max_support_image_count is None and merged["b"].costs == ()


def test_fetch_models_degrades_loudly_on_failure() -> None:
    """能力表读不到 ⇒ **不抛**，但要把降级说出来。"""
    import asyncio

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
        found, notes = asyncio.run(caps.fetch_models(c))
    assert found == []
    assert len(notes) == 2, "两个端点各一条降级说明"
    assert all("退回冻结快照" in n for n in notes)
