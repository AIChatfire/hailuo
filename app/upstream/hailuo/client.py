#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hailuo 上游客户端：建任务 / 批量查任务 / 按 id 直查 / 查在途。

## 四条端点（除公开配置外全部 POST + JSON，全部要 `token` + `yy`）

| 用途 | 端点 | body |
|---|---|---|
| **建任务** | `/v2/api/multimodal/generate/image` | `{quantity, parameter:{…}, projectID:"0"}` |
| **批量查** | `/api/feed/creation/my/batch` | `{cursor, limit, type:"next", scene:"create", projectID:"0", feedTypes}` |
| **按 id 直查** | `/v4/api/multimodal/video/processing` | `{batchInfoList:[{batchID, batchType}], type?}` |
| **在途查** | `/api/feed/creation/my/processing` | `{projectID:"0"}` |

⚠️ **用户给的抓包里把 `my/processing` 标成了"创建任务" —— 那不是创建端点。**
真正的创建端点由前端 chunk `9563` 里的 `MS()` 指明：
`POST /v2/api/multimodal/generate/image`。`my/processing` 只回"我有没有任务在跑"
（`{processing: bool, onProcessingImageNum: int}`）。**建任务是计费动作**，
认错端点等于对着一个查询接口不停地"创建"。

## 按 id 直查（v4）—— 图片与视频**共用同一端点**，但两种形态不一样

`POST /v4/api/multimodal/video/processing` 用 body 里的 `batchType` 区分类型
（**与 feedType 同数值**：0=video，1=image）。参考实现（MeUtils hailuoai，
两份都在生产跑）给出两种**逐字段对齐**的形态：

· **图片**（`images.get_task`，batchType=1）：
  `{"batchInfoList":[{"batchID":…,"batchType":1}]}` —— **顶层不带 `type`**；
· **视频**（`openai_videos.get_task`，batchType=0，带真实抓包注释）：
  `{"batchInfoList":[…],"type":1}` —— **顶层带 `"type":1`**。

历史形态：`GET /api/multimodal/video/processing?idList=…`（无 v4、query 传 id）
已被上两者取代 —— 本服务不实现它，只在此留档。

## 响应信封

```json
{"statusInfo": {"code": 0, "httpCode": 0, "message": "成功", …}, "data": {…}}
```

`code == 0` 才是成功；非 0 一律抛错（`401` 是**没带 token 的 HTTP 状态**，
而 `code:2` 是**参数错**——两者在不同层，不要混）。

## 任务状态的**权威取值**（前端 chunk `9206` 的 `FeedStatus`，逐字照抄）

| 值 | 名字 | 本服务语义 |
|---|---|---|
| 12 | BEFORE_WAIT_CREATE | 排队 |
| 11 | WAIT_FOR_CREATE | 排队 |
| 1 | CREATING | 进行中 |
| **2** | **SUCCESS** | **成功**（本轮 50 条历史样本全是 2） |
| 10 | APPEAL_APPROVED | 成功（走完申诉后放行） |
| 3 | Fail | 失败 |
| 5 | SENSITIVE | 失败（涉敏感） |
| 14 | SENSITIVE_FAIL | 失败（涉敏感） |
| 6 | WAIT_FORE_REVIEW | 非终态（等审核） |
| 7 | REJECTED | 失败（被拒） |
| 16 | REAL_PERSON_REVIEW | 非终态（人工复核） |
| 8 | APPEALING | 非终态（申诉中） |
| 9 | APPEAL_REJECTED | 失败 |

`feedType`：**0=video，1=image**，2/3=模板，4/5=Tool，6=Audio。本服务只认 **1**。

v4 直查的 asset 状态取值与上表**同一套**（参考实现 `openai_videos.get_task`：
12=排队、1=进行中、2=成功、{3,5,7,14}=失败 —— 是本表子集）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

from ...errors import (
    AdapterError,
    AuthError,
    ContentPolicyError,
    InvalidParameterError,
    RiskControlChallenge,
    UpstreamError,
    UpstreamQuotaExhausted,
    UpstreamRateLimited,
    UpstreamTimeout,
)
from . import sign

# ---------------------------------------------------------------------------
# 端点常量
# ---------------------------------------------------------------------------

PATH_CREATE_IMAGE = "/v2/api/multimodal/generate/image"
PATH_BATCH = "/api/feed/creation/my/batch"
PATH_PROCESSING = "/api/feed/creation/my/processing"
#: 按 id 直查。图片（batchType=1）与视频（batchType=0）共用；
#: 旧形态 `GET /api/multimodal/video/processing?idList=…` 已过时，不实现。
PATH_V4_PROCESSING = "/v4/api/multimodal/video/processing"

#: 图片 feedType。视频是 0 —— 本服务不做视频，列表里一并过滤掉。
FEED_TYPE_IMAGE = 1

#: v4 直查 body 里的 `batchType` —— **与 feedType 同数值同语义**（0=video，1=image）。
BATCH_TYPE_VIDEO = 0
BATCH_TYPE_IMAGE = FEED_TYPE_IMAGE

# ---------------------------------------------------------------------------
# 状态枚举（前端 chunk 9206 的 FeedStatus，逐字照抄）
# ---------------------------------------------------------------------------

ST_BEFORE_WAIT_CREATE = 12
ST_WAIT_FOR_CREATE = 11
ST_CREATING = 1
ST_SUCCESS = 2
ST_APPEAL_APPROVED = 10
ST_FAIL = 3
ST_SENSITIVE = 5
ST_SENSITIVE_FAIL = 14
ST_WAIT_FOR_REVIEW = 6
ST_REJECTED = 7
ST_REAL_PERSON_REVIEW = 16
ST_APPEALING = 8
ST_APPEAL_REJECTED = 9

#: 非终态（前端 `xE` = `[CREATING, WAIT_FOR_CREATE, BEFORE_WAIT_CREATE]`）+
#: 两个"上游还在处理"的审核态（前端把它们归到别处，但对本服务而言同样是**还没好**）。
IN_PROGRESS: frozenset[int] = frozenset({
    ST_CREATING, ST_WAIT_FOR_CREATE, ST_BEFORE_WAIT_CREATE,
    ST_WAIT_FOR_REVIEW, ST_REAL_PERSON_REVIEW, ST_APPEALING,
})

#: 终态成功（前端 `fV` = `[APPEAL_APPROVED, SUCCESS]`）。
SUCCEEDED: frozenset[int] = frozenset({ST_SUCCESS, ST_APPEAL_APPROVED})

#: 终态失败（前端 `R` 集合）—— 每一项都带一句**下一步**。
FAILED: dict[int, tuple[str, str]] = {
    ST_FAIL: ("task_failed", "上游生成失败。⚠️ hailuo 的建任务是**计费动作**，"
                             "失败**不等于没花钱** —— 以额度和账单为准。"),
    ST_SENSITIVE: ("content_policy_violation", "命中敏感内容策略 ⇒ 换 prompt 或换图。"),
    ST_SENSITIVE_FAIL: ("content_policy_violation", "命中敏感内容策略（生成失败）⇒ 换 prompt 或换图。"),
    ST_REJECTED: ("content_policy_violation", "审核未通过 ⇒ 换 prompt 或换图。"),
    ST_APPEAL_REJECTED: ("content_policy_violation", "申诉被驳回 ⇒ 换 prompt 或换图。"),
}


def status_name(code: int | None) -> str:
    """上游数值状态 → 可读名（日志与降级说明用）。"""
    return {
        ST_BEFORE_WAIT_CREATE: "BEFORE_WAIT_CREATE", ST_WAIT_FOR_CREATE: "WAIT_FOR_CREATE",
        ST_CREATING: "CREATING", ST_SUCCESS: "SUCCESS", ST_FAIL: "Fail",
        ST_SENSITIVE: "SENSITIVE", ST_SENSITIVE_FAIL: "SENSITIVE_FAIL",
        ST_WAIT_FOR_REVIEW: "WAIT_FORE_REVIEW", ST_REJECTED: "REJECTED",
        ST_REAL_PERSON_REVIEW: "REAL_PERSON_REVIEW", ST_APPEALING: "APPEALING",
        ST_APPEAL_REJECTED: "APPEAL_REJECTED", ST_APPEAL_APPROVED: "APPEAL_APPROVED",
    }.get(code if code is not None else -1, f"UNKNOWN({code})")


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass
class Feed:
    """一条上游产物记录（`batchFeeds[].feeds[]` 或 v4 的 `batchVideos[].assets[]`）。"""

    feed_id: str
    batch_id: str
    status: int | None
    feed_type: int | None
    create_time: int | None
    #: 产物 URL（`metaInfo.imageMetaInfo.mediaInfo.url`；v4 形态是 asset.downloadURL 直链）
    url: str = ""
    #: 去水印 URL（`downloadURL.withoutWatermarkURL`）
    url_no_watermark: str = ""
    width: int | None = None
    height: int | None = None
    #: 生成进度 0~100（**只有 v4 形态带**；`my/batch` 没有这个字段 ⇒ None）
    percent: int | None = None
    file_id: str = ""
    file_name: str = ""
    model_id: str = ""
    desc: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_image(self) -> bool:
        return self.feed_type == FEED_TYPE_IMAGE

    @property
    def is_succeeded(self) -> bool:
        return self.status in SUCCEEDED

    @property
    def is_in_progress(self) -> bool:
        return self.status in IN_PROGRESS

    @property
    def is_failed(self) -> bool:
        return self.status in FAILED

    def failure(self) -> tuple[str, str] | None:
        """终态失败时返回 `(code, message)`。"""
        return FAILED.get(self.status) if self.status is not None else None


def parse_feed(raw: dict[str, Any]) -> Feed:
    """把上游一条 feed 解析成 `Feed`。

    ⚠️ **产物 URL 有两条**（`url` 与 `withoutWatermarkURL`）。本服务**默认给未带水印的**
    那条，因为调用方拿到图是要用的，而水印版是给网页展示的。两条都在
    `raw` 里保留，trace 上可见。取不到未水印版时退回 `url`（**不报错**，
    因为"有水印"仍然是可用产物），但这件事不产生降级痕迹 —— 它是上游的常态分支。
    """
    common = raw.get("commonInfo") or {}
    meta = ((raw.get("metaInfo") or {}).get("imageMetaInfo") or {}).get("mediaInfo") or {}
    dl = meta.get("downloadURL") or {}
    param = ((raw.get("modelParameter") or {}).get("imageParameter")) or {}

    url = str(meta.get("url") or "")
    no_wm = str(dl.get("withoutWatermarkURL") or "")
    feed_id = str(common.get("id") or "")

    # 兜底：有些 feed 形态把产物放在 feedCoverInfo（实测样本里它在，但没水印版更全）
    if not url:
        url = str((raw.get("feedCoverInfo") or {}).get("coverURL") or "")

    return Feed(
        feed_id=feed_id,
        batch_id=str(common.get("batchID") or ""),
        status=common.get("status"),
        feed_type=raw.get("feedType"),
        create_time=common.get("createTime"),
        url=no_wm or url,
        url_no_watermark=no_wm,
        width=meta.get("width"),
        height=meta.get("height"),
        file_id=str(dl.get("fileID") or ""),
        file_name=str(dl.get("fileName") or ""),
        model_id=str(param.get("modelID") or ""),
        desc=str(param.get("desc") or ""),
        message=str((raw.get("feedMessage") or {}).get("message") or ""),
        raw=raw,
    )


def parse_batches(payload: dict[str, Any]) -> list[tuple[str, list[Feed]]]:
    """`batch` 响应 → `[(batch_id, [Feed, …]), …]`。"""
    out: list[tuple[str, list[Feed]]] = []
    for batch in ((payload or {}).get("data") or {}).get("batchFeeds") or []:
        feeds = [parse_feed(f) for f in (batch.get("feeds") or [])]
        out.append((str(batch.get("batchID") or ""), feeds))
    return out


def parse_v4_batches(payload: dict[str, Any]) -> list[tuple[str, list[Feed]]]:
    """`/v4/.../processing` 响应 → `[(batch_id, [Feed, …]), …]`。

    ⚠️ **同一个键名 `downloadURL`，两种类型**（这是 v4 与 `my/batch` 最容易踩的差异）：

    · v4 的 asset 里是**字符串直链**（参考实现直接 `asset.get("downloadURL")` 当 URL 用）；
    · `my/batch` 的 `mediaInfo.downloadURL` 是**字典** `{watermarkURL, withoutWatermarkURL}`。

    两种都接：字符串直接当 URL；字典按"去水印优先"取。

    容器名：**图片与视频都用 `data.batchVideos[].assets[]`** ——
    2026-09-21 用真实图片批次（batchType=1）实测确认：容器名不带 Image，
    就是 batchVideos（历史命名）；条目里回显 `batchType: 1`。
    `batchImages / batchFeeds` 两个候选只是防御性扫描，取第一个非空容器。

    ⚠️ 查不到时上游**不报错**：`batchVideos: []` + `processInfo.unReadProcessedList`
    （那是已读清单之类的无关噪音，**别解析它**）。
    """
    data = (payload or {}).get("data") or {}
    out: list[tuple[str, list[Feed]]] = []
    for key in ("batchVideos", "batchImages", "batchFeeds"):
        containers = data.get(key) or []
        if not containers:
            continue
        for batch in containers:
            batch_id = str(batch.get("batchID") or "")
            #: `assets[]` 有证据；缺失时把容器本身当单条 asset 处理 ——
            #: 状态/URL 字段名一致，至少能把终态抠出来（好于整条丢弃）。
            assets = batch.get("assets")
            if assets is None:
                assets = [batch]
            out.append((batch_id, [_parse_v4_asset(a, batch_id) for a in assets]))
        break  # 取第一个非空容器 —— 不做跨容器合并（未取证）
    return out


def _parse_v4_asset(asset: dict[str, Any], batch_id: str) -> Feed:
    """v4 的一条 asset → `Feed`。

    形态 **2026-09-21 用真实图片批次实测钉死**（此前只靠参考实现的部分键名）：
    asset 顶层就有 `id`（= feed 记录 id）、`modelID`、`width/height`、`fileID`、
    `coverURL`、`modelParameter` 等 —— 全部照实_lift 进 Feed；
    `downloadURL` 是**字符串直链且就是去水印版**（与 my/batch 的
    `withoutWatermarkURL` 同一条 URL）；`percent` 在已完成时为 null。
    """
    dl = asset.get("downloadURL")
    if isinstance(dl, dict):  # 防御：万一上游回的是 my/batch 那种字典形态
        no_wm = str(dl.get("withoutWatermarkURL") or "")
        url = no_wm or str(dl.get("watermarkURL") or "")
    else:
        url = str(dl or "")
        no_wm = url  # 实测：v4 的直链即去水印版
    return Feed(
        feed_id=str(asset.get("id") or ""),
        batch_id=str(asset.get("batchID") or batch_id),
        status=asset.get("status"),
        feed_type=None,  # 类型由请求的 batchType 决定（响应里不回 feedType）
        create_time=asset.get("createTime"),
        url=url,
        url_no_watermark=no_wm,
        width=asset.get("width"),
        height=asset.get("height"),
        percent=asset.get("percent"),
        file_id=str(asset.get("fileID") or ""),
        model_id=str(asset.get("modelID") or ""),
        desc=str(asset.get("desc") or ""),
        message=str(asset.get("message") or ""),
        raw=asset,
    )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class HailuoClient:
    """同步 HTTP 客户端（协调器是同步线程，用 `httpx.Client` 更省事）。

    **唯一持有 `token` 的地方** —— 绝不把它传给任何埋点（见 observability 的纪律）。
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://hailuoai.video",
        device: dict[str, Any] | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.device = dict(device or {})
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
                ),
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/zh-Intl/create/image-generation",
            },
            transport=transport,
        )

    # ------------------------------------------------------------------ 内核

    def close(self) -> None:
        self._client.close()

    def _sign_and_url(self, path: str, body: dict[str, Any] | None) -> tuple[str, str, str]:
        """返回 `(full_path_with_query, body_json, yy)`。

        **三件事必须同源**：查询串里的 `unix`、签名里的 `time`、请求体的 JSON 字符串。
        任何一处用了不同的值 ⇒ 上游回 `code:2 请求异常`。
        """
        unix = sign.now_ms_truncated()
        params = sign.public_params(unix=unix, **self.device)
        query = sign.build_query(params)
        full = f"{path}?{query}"
        body_json = sign.compact_json(body) if body else "{}"
        yy = sign.sign_yy(path_with_query=full, body_json=body_json,
                          time_ms=unix, method="POST")
        return full, body_json, yy

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        dry_run: bool = False,
        idempotent: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """发一次请求。返回 `(payload, trace)`；`trace` 用于埋点（**不含凭据**）。

        `dry_run=True` —— **只构造、不发送**（返回 `payload={}`）。
        这是"零消耗验证翻译层"的唯一入口：签名/URL/body 全部算出来，
        但一个字节都不出网。建任务是计费动作，这条路径必须存在。

        🔴 重试判据按**请求是否可能已送达**两级划分（2026-09-21 实测补充）：

        · **连接根本没建立**（`ConnectError` / `ConnectTimeout`）⇒ 请求**不可能**
          到达上游 ⇒ 连建任务都允许重试（没送达就不可能计费）；
        · **已送达但响应丢失/连接中途被掐**（`RemoteProtocolError` / 读超时等）
          ⇒ 只有 `idempotent=True` 的请求才重试。**建任务在这种情形下绝不重试**
          —— 无法排除"上游其实收到了并已建任务"，重试就是重复扣费。

        实测依据（2026-09-21）：冷连接池下 N 个并发请求同时**新建连接**
        约 1/3 概率被掐（`RemoteProtocolError: Server disconnected without sending
        response`），而连接复用后的并发稳定通过 ⇒ 重试一次即可越过这个窗口。
        """
        full, body_json, yy = self._sign_and_url(path, body)
        trace: dict[str, Any] = {
            "http_method": method.upper(),
            #: 🔴 **只给 pathname，丢掉整个 query** —— query 里没有凭据，
            #: 但它是签名输入的一部分，长度与内容会暴露设备指纹。
            "http_path": path,
            "request_body": body_json,
            "yy": yy,
            "dry_run": dry_run,
        }
        if dry_run:
            trace["sent"] = False
            return {}, trace

        attempts = 3
        resp: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            #: 每次重试都要重签（`unix` 参与签名，且时间窗过期会被判 `code:2`）
            if attempt > 1:
                full, body_json, yy = self._sign_and_url(path, body)
                time.sleep(0.2 * attempt)  # 退避很小：这是建连抖动，不是限流
                trace["retry_attempts"] = attempt
            try:
                resp = self._client.request(
                    method.upper(), full, content=body_json.encode("utf-8"),
                    headers={"token": self.token, "yy": yy},
                )
                break
            except httpx.TimeoutException as e:
                #: `ConnectTimeout` = 连接没建成 ⇒ 未送达 ⇒ 任何请求都可重试
                if attempt < attempts and (idempotent
                                           or isinstance(e, httpx.ConnectTimeout)):
                    continue
                raise UpstreamTimeout(f"上游超时：{path} ({e})") from e
            except httpx.HTTPError as e:
                #: `ConnectError` = 连接没建成 ⇒ 未送达 ⇒ 任何请求都可重试
                if attempt < attempts and (idempotent
                                           or isinstance(e, httpx.ConnectError)):
                    continue
                raise UpstreamError(f"上游连接失败：{path} ({type(e).__name__}: {e})") from e
        assert resp is not None  # 循环要么 break 要么 raise

        trace["http_status"] = resp.status_code
        trace["response_text"] = resp.text[:4000]

        if resp.status_code == 401:
            raise AuthError(
                "hailuo 拒绝了 token（HTTP 401）—— 凭据已失效或未登录，请更换 HAILUO_TOKEN。")
        if resp.status_code == 429:
            retry_after = _retry_after(resp)
            raise UpstreamRateLimited("上游限流（HTTP 429）。", retry_after=retry_after)
        if resp.status_code >= 500:
            raise UpstreamError(f"上游 {resp.status_code}：{resp.text[:200]}")

        try:
            payload = resp.json()
        except Exception as e:  # noqa: BLE001
            raise UpstreamError(
                f"上游返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}") from e

        self._raise_for_status_info(payload, trace)
        return payload, trace

    @staticmethod
    def _raise_for_status_info(payload: dict[str, Any], trace: dict[str, Any]) -> None:
        """信封里的 `statusInfo.code != 0` ⇒ 抛错。

        ⚠️ 这一层与 HTTP 状态**不是一回事**：上游大量业务错误走 HTTP 200 + `code!=0`。
        只看 HTTP 状态会把失败当成功（反过来，`code:2` 也常配 HTTP 400）。
        """
        info = (payload or {}).get("statusInfo") or {}
        code = info.get("code")
        if code in (0, None):
            return
        msg = str(info.get("message") or "")
        trace["upstream_code"] = code
        low = msg.lower()
        if any(k in msg for k in ("登录", "token", "未授权")) or code in (1001, 1002):
            raise AuthError(f"hailuo 业务层鉴权失败（code={code}）：{msg}")
        if "积分" in msg or "额度" in msg or "余额" in msg or "quota" in low:
            raise UpstreamQuotaExhausted(f"上游额度不足（code={code}）：{msg}")
        if "频繁" in msg or "限流" in msg or "风控" in msg or "verify" in low:
            raise RiskControlChallenge(f"上游风控/频控（code={code}）：{msg}")
        if "敏感" in msg or "审核" in msg or "违规" in msg or "policy" in low:
            raise ContentPolicyError(f"上游内容策略拦截（code={code}）：{msg}")
        if code == 2:
            raise InvalidParameterError(f"上游拒绝该请求（code=2）：{msg}")
        raise UpstreamError(f"上游业务错误（code={code}）：{msg}")

    # ------------------------------------------------------------------ 业务

    def create_image(
        self,
        *,
        model_id: str,
        desc: str,
        file_list: list[dict[str, Any]],
        quantity: int = 1,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        quality: str | None = None,
        reference_mode: str | None = None,
        use_origin_prompt: bool = True,
        project_id: str = "0",
        dry_run: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """建一次图片生成任务。返回 `(upstream_batch_id, trace)`。

        🔴 **这是计费动作。** `dry_run=True` 时零消耗，只验证翻译层。

        请求体逐字段对齐前端 `MS()`（chunk `9563`）：

        ```js
        let r = {quantity: e.quantity,
                 parameter: {modelID, desc, fileList, useOriginPrompt,
                             aspectRatio, resolution, quality, referenceMode},
                 projectID: e.projectID};
        // 仅在 e.extra 存在时才带 imageExtra
        ```

        ⚠️ **空字段要省掉而不是传 `null`**：上游对 `null` 的处理未取证，
        而"省略"在抓包里是常态（t2i 那次 `modelParameter` 里就没有 `resolution` 之外的键）。
        所以这里**只放有值的键**。
        """
        parameter: dict[str, Any] = {
            "modelID": model_id,
            "desc": desc,
            "fileList": file_list,
            "useOriginPrompt": bool(use_origin_prompt),
        }
        if aspect_ratio:
            parameter["aspectRatio"] = aspect_ratio
        if resolution:
            parameter["resolution"] = resolution
        if quality:
            parameter["quality"] = quality
        if reference_mode:
            parameter["referenceMode"] = reference_mode

        body = {"quantity": int(quantity), "parameter": parameter, "projectID": project_id}

        payload, trace = self.request("POST", PATH_CREATE_IMAGE, body, dry_run=dry_run)
        if dry_run:
            return "", trace

        data = payload.get("data") or {}
        # 🔴 **响应里有两个 id，别拿错**（2026-09-21 真实文生图实测钉死）：
        #   data.id            = **feed 记录 id**（= 之后 my/batch 里 commonInfo.id）
        #   data.task.batchID  = **批次 id**（= my/batch 的 batchFeeds[].batchID、v4 的 batchID）
        # 拿 data.id 当 batchID 轮询 ⇒ my/batch 永远对不上、v4 恒回空 ——
        # 实测教训：图已成功、钱已花，轮询却"查无此任务"。
        # 参考 MeUtils openai_videos 的抓包注释同构：id 与 task.batchID 是两个值。
        task = data.get("task") or {}
        batch_id = str(task.get("batchID") or "")
        record_id = str(data.get("id") or "")
        trace["upstream_record_id"] = record_id
        trace["is_first_generate"] = data.get("isFirstGenerate")
        if not batch_id:
            #: task 缺失时的兜底：拿记录 id 总比没有强（旧形态/异常报文），
            #: 但要在 trace 里留痕，别让"用了次优 id"变成看不见的事。
            batch_id = record_id
            trace["batch_id_fallback_to_record"] = True
        trace["upstream_submit_id"] = batch_id
        if not batch_id:
            raise UpstreamError(
                f"上游受理成功但响应里没有 batch id：{json.dumps(data, ensure_ascii=False)[:300]}")
        logger.bind(upstream_submit_id=batch_id, model_id=model_id).info(
            f"上游已受理图片任务：batch={batch_id} record={record_id} model={model_id} n={quantity}")
        return batch_id, trace

    def fetch_batches(
        self, *, limit: int = 30, cursor: str = "", feed_types: list[int] | None = None,
        project_id: str = "0", dry_run: bool = False,
    ) -> tuple[list[tuple[str, list[Feed]]], dict[str, Any]]:
        """批量查任务（"我最近 N 条"，**不带 id 过滤**）。

        🔴 **这是本项目"批量轮询"能力的来源**：一次请求就能覆盖**全部**在途任务。
        所以协调器**每个 tick 只打一次上游**，而不是每个任务打一次。
        逐个 id 的点名查询见 `fetch_by_ids`（互补，不是替代）。
        """
        body = {
            "cursor": cursor,
            "limit": int(limit),
            "type": "next",
            "scene": "create",
            "projectID": project_id,
            "feedTypes": feed_types if feed_types is not None else [FEED_TYPE_IMAGE],
        }
        payload, trace = self.request("POST", PATH_BATCH, body, dry_run=dry_run,
                                      idempotent=True)  # 查询：连接级失败可重试
        if dry_run:
            return [], trace
        batches = parse_batches(payload)
        trace["batch_count"] = len(batches)
        trace["feed_count"] = sum(len(f) for _, f in batches)
        trace["has_next"] = bool((payload.get("data") or {}).get("hasNext"))
        return batches, trace

    def fetch_by_ids(
        self,
        batch_ids: list[str],
        *,
        batch_type: int = BATCH_TYPE_IMAGE,
        include_type_field: bool | None = None,
        dry_run: bool = False,
    ) -> tuple[list[tuple[str, list[Feed]]], dict[str, Any]]:
        """按 batch id **点名直查**（`POST /v4/api/multimodal/video/processing`）。

        与 `fetch_batches`（回"我最近 N 条"，无法按 id 过滤）**互补**：
        这条端点收 `batchInfoList`，专门查指定的 batch —— 一次可带多个 id。

        请求体**逐字段**对齐两份参考实现（都在生产跑）：

        · **图片**（batchType=1）：
          `{"batchInfoList":[{"batchID":…,"batchType":1}]}` —— 顶层**不带** `type`；
        · **视频**（batchType=0）：
          `{"batchInfoList":[…],"type":1}` —— 顶层**带** `"type":1`
          （`openai_videos.get_task` 内嵌的真实抓包即此形态）。

        `include_type_field=None`（默认）= 按上面规则自动选择；显式传布尔可覆盖
        （两份参考实现只分别证实了各自那一种组合，覆盖属于**实验行为**）。

        视频任务（batchType=0）同样走这条端点 —— 返回的
        `data.batchVideos[].assets[]` 带 `status/downloadURL/percent/createTime`，
        状态取值与 `my/batch` 的 FeedStatus **同一套**（见模块 docstring）。
        """
        ids = [str(i) for i in batch_ids if str(i)]
        if not ids:
            #: 空请求不出网 —— 一个字节都不该为"没东西可查"花掉。
            return [], {"http_path": PATH_V4_PROCESSING, "skipped": "no_ids",
                        "dry_run": bool(dry_run)}
        if include_type_field is None:
            include_type_field = int(batch_type) == BATCH_TYPE_VIDEO

        body: dict[str, Any] = {
            "batchInfoList": [{"batchID": i, "batchType": int(batch_type)} for i in ids],
        }
        if include_type_field:
            body["type"] = 1

        payload, trace = self.request("POST", PATH_V4_PROCESSING, body, dry_run=dry_run,
                                      idempotent=True)  # 查询：连接级失败可重试
        if dry_run:
            return [], trace
        batches = parse_v4_batches(payload)
        trace["batch_count"] = len(batches)
        trace["feed_count"] = sum(len(f) for _, f in batches)
        trace["requested_ids"] = len(ids)
        return batches, trace

    def fetch_processing(self, *, project_id: str = "0",
                         dry_run: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        """查"我有没有任务在跑"（`{processing, onProcessingImageNum, …}`）。

        协调器用它做**廉价的存在性判断**：`onProcessingImageNum == 0` 时
        可以跳过本轮 batch 查询（没有在途任务就别打上游）。
        """
        payload, trace = self.request("POST", PATH_PROCESSING,
                                      {"projectID": project_id}, dry_run=dry_run,
                                      idempotent=True)  # 查询：连接级失败可重试
        if dry_run:
            return {}, trace
        return payload.get("data") or {}, trace

    # ------------------------------------------------------------------ 辅助

    def probe_token(self) -> tuple[bool, str]:
        """轻量凭据自检：查一次 `processing`。**零计费**（不建任务）。

        返回 `(ok, 说明)`。用于 `/readyz` 与启动自检 ——
        凭据坏了要在启动时就发现，而不是等第一个真实任务。
        """
        try:
            data, _ = self.fetch_processing()
        except AdapterError as e:
            return False, f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        return True, f"processing={data.get('processing')} " \
                     f"in_flight_image={data.get('onProcessingImageNum')}"


def _retry_after(resp: httpx.Response) -> float | None:
    """`Retry-After` **只在真的给了的时候**返回 —— 编一个数字等于伪造事实。"""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


__all__ = [
    "BATCH_TYPE_IMAGE",
    "BATCH_TYPE_VIDEO",
    "FAILED",
    "FEED_TYPE_IMAGE",
    "Feed",
    "HailuoClient",
    "IN_PROGRESS",
    "PATH_BATCH",
    "PATH_CREATE_IMAGE",
    "PATH_PROCESSING",
    "PATH_V4_PROCESSING",
    "SUCCEEDED",
    "parse_batches",
    "parse_feed",
    "parse_v4_batches",
    "status_name",
]
