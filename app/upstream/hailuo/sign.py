#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hailuo `yy` 请求签名 —— 从 `3340-*.js` 的 axios 请求拦截器逐字还原。

## 上游原码（webpack 打包后，module 62001 + 拦截器）

```js
W.interceptors.request.use(e => {
  e.headers.token = getLocalStorageToken();
  let i = Date.parse(new Date().toString());        // 毫秒，已截断到秒
  let l = mC(i);                                    // 公共参数
  e.params = {...P6(e.params), ...l};
  let u = wQ(e.url, e.params);                      // path?<合并后的查询串>
  let {encrypt: s, bodyString: c} = b({time: i, body: e.data,
        hasSearchParamsPath: u, method: e.method,
        bodyToYY: e.headers.yy || null});
  e.headers.yy = s; e.data = c; e.url = u; e.params = {};
});
```

其中 `b`：

```js
b = ({hasSearchParamsPath: t, bodyToYY: o, method: n, time: r, body: a}) => {
  let i = {};
  if (n && (n.toLowerCase() === "post" || n.toLowerCase() === "delete")) i = a || {};
  let l = i = JSON.stringify(i);                    // ← bodyString：GET 恒为 "{}"
  if (o) i = o;                                     // ← 已有 yy 时用 yy 顶替 body 段
  let d = encodeURIComponent(t) + "_" + i + MD5(r.toString()) + "ooui";
  return {encrypt: MD5(d), bodyString: l};
};
```

⇒ 算法（本模块 `sign_yy`）：

```
time      = floor(now_ms / 1000) * 1000            # `Date.parse(new Date().toString())`
body_json = JSON.stringify(body)  (POST/DELETE 且 body 非空)  否则 "{}"
yy        = md5( urlencode(full_path_with_query) + "_" + body_json + md5(str(time)) + "ooui" )
```

## 两个容易写错的点

1. **`encodeURIComponent` 不是 `quote`** —— 它不转义 `-_.!~*'()`，其余（含 `/` `?` `&` `=` `:`）
   全部百分号编码。用 `quote(safe="")` 会多转义那 7 个字符 ⇒ 签名必然错。
   本模块用 `_encode_uri_component` 精确复刻。
2. **`body_json` 必须是「紧凑 + 原文键序」** —— 上游是 `JSON.stringify(e.data)`，
   而 `e.data` 就是调用方给的原始对象。所以构造 body 时**不能重排键**、
   不能加空格。本模块的 `sign_yy` 接收**已经序列化好的字符串**，把责任显式化。

## 自证

`VECTORS` 是 5 条**真实抓包**（创建/轮询/上传/图生图），
`tests/test_sign.py` 逐条断言。**签名错一个字，这里就红** —— 这是本仓最强的回归网。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote_plus

# ---------------------------------------------------------------------------
# 常量（来自 webpack module 62001 / 99273 / 68444）
# ---------------------------------------------------------------------------

APP_ID: Final = 3001
VERSION_CODE: Final = 22203
BIZ_ID: Final = 0
DEVICE_PLATFORM: Final = "web"

#: `encodeURIComponent` 不转义的字符集（与 `urllib.parse.quote(safe=...)` 语义不同）
_URI_UNRESERVED: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()"

#: 签名尾缀。上游硬编码字面量 `"ooui"`。
SIGN_SUFFIX: Final = "ooui"


def md5_hex(text: str) -> str:
    """MD5 → 32 位小写十六进制。上游用 `md5` 的默认 hex 输出。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _encode_uri_component(text: str) -> str:
    """复刻 JS `encodeURIComponent`。

    ⚠️ 不要用 `urllib.parse.quote(text, safe="")` 替代：它会把 `-_.!~*'()`
    这 7 个字符也编码掉，而 JS 不编码它们 ⇒ 签名不一致。
    """
    out: list[str] = []
    for ch in text:
        if ch in _URI_UNRESERVED:
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def now_ms_truncated() -> int:
    """`Date.parse(new Date().toString())` —— 毫秒时间戳**截断到秒**。

    `new Date().toString()` 的字符串不含毫秒 ⇒ 解析回来必然是整秒。
    这个值既是 `unix` 查询参数，也是签名里 `md5(time)` 的输入，
    两者必须**是同一个数**。
    """
    return int(time.time()) * 1000


def public_params(
    *,
    unix: int,
    uuid: str,
    device_id: str,
    lang: str = "zh-Intl",
    os_name: str = "Mac",
    browser_name: str = "chrome",
    device_memory: int = 32,
    cpu_core_num: int = 10,
    browser_language: str = "zh-CN",
    browser_platform: str = "MacIntel",
    screen_width: int = 2560,
    screen_height: int = 1440,
    activity_tag: str = "",
) -> dict[str, Any]:
    """上游 `mC()` 生成的公共查询参数。

    上游这些值全部来自浏览器环境（`navigator.deviceMemory` / `screen.width` / locale）。
    服务端跑没有 `navigator`，所以**必须显式注入**并保持一致 —— 它们参与签名，
    但服务端**不校验其真实性**（抓包实测：改 screen_* 仍 200）。
    取值来自抓包环境，作为默认值冻结。

    ⚠️ 键序即**签名输入的一部分**：`sign_yy` 会用同一个 dict 拼查询串，
    所以只要「构造 → 签名 → 发送」用同一个 dict，键序自洽。
    但 `tests/test_sign.py` 里的抓包向量要求**与抓包完全同序**才能复算成功，
    这正是在锁死"键序不能乱"。
    """
    params: dict[str, Any] = {
        "device_platform": DEVICE_PLATFORM,
        "app_id": APP_ID,
        "version_code": VERSION_CODE,
        "biz_id": BIZ_ID,
        "unix": unix,
        "lang": lang,
    }
    if uuid:
        params["uuid"] = uuid
    if device_id:
        params["device_id"] = device_id
    params["os_name"] = os_name
    params["browser_name"] = browser_name
    if device_memory:
        params["device_memory"] = device_memory
    if cpu_core_num:
        params["cpu_core_num"] = cpu_core_num
    if browser_language:
        params["browser_language"] = browser_language
    if browser_platform:
        params["browser_platform"] = browser_platform
    if activity_tag:
        params["activityTag"] = activity_tag
    params["screen_width"] = screen_width
    params["screen_height"] = screen_height
    return params


def build_query(params: dict[str, Any]) -> str:
    """上游 `wQ()` 的查询串拼装：`key=String(value)`，`null`/`undefined` 跳过。

    ⚠️ 上游用的是 `URLSearchParams.append(t, String(o))`，它按**表单编码**规则转义
    （空格→`+`，`!~*'()` 也被转义，这点与 `encodeURIComponent` 不同）。
    但公共参数全是 ASCII 字母数字与 `-`，两种编码结果一致 ⇒ 用 `quote_plus` 忠实复刻。
    """
    parts = []
    for key, value in params.items():
        if value is None:
            continue
        parts.append(f"{key}={quote_plus(str(value))}")
    return "&".join(parts)


def sign_yy(
    *,
    path_with_query: str,
    body_json: str,
    time_ms: int,
    method: str = "POST",
    existing_yy: str | None = None,
) -> str:
    """算出 `yy` 请求头。

    Args:
        path_with_query: **含查询串的完整 path**（如 `/api/feed/creation/my/batch?device_platform=web&…`）。
            上游传的是拦截器拼好的 `u` —— 也就是**真正发出去的那个 URL 的 path 部分**。
        body_json: 请求体的 JSON 字符串。GET 传 `"{}"`。
            上游 `JSON.stringify(e.data)`，**紧凑、无空格、原键序**。
        time_ms: 截断到秒的毫秒时间戳，**必须与查询参数里的 `unix` 完全一致**。
        method: 上游只有 POST/DELETE 才把 body 计入签名；其余一律用 `"{}"`。
        existing_yy: 上游 `bodyToYY` —— 若已显式给了 yy，它**顶替** body 段。
            本服务不用这个分支（保持单一真相），保留参数只为忠实还原与测试。
    """
    upper = (method or "").upper()
    if upper in ("POST", "DELETE"):
        payload = body_json or "{}"
    else:
        payload = "{}"
    if existing_yy:
        payload = existing_yy

    raw = (
        _encode_uri_component(path_with_query)
        + "_"
        + payload
        + md5_hex(str(time_ms))
        + SIGN_SUFFIX
    )
    return md5_hex(raw)


# ---------------------------------------------------------------------------
# 抓包向量（5 条。任何一条不符 = 签名实现已漂移）
#
# ⚠️ **已脱敏**（2026-09-21，公开仓发布前）：原始抓包里的**真实设备指纹**
# （uuid / device_id）已替换为**合成值**（见 CAPTURE_DEVICE），因此
# `expect_yy` 是按合成指纹用本实现**重算**的 —— 它仍然锁住"签名实现漂移"
# 与算法结构（见 tests/test_sign.py 的结构自证），但**不再**具备
# "与真实客户端逐字节一致"的外部取证意义（脱敏前，真实向量已验证过实现）。
# ---------------------------------------------------------------------------

#: 抓包环境的公共参数（全部向量共用）。键序 = 抓包里 query 的实际顺序。
CAPTURE_DEVICE: Final[dict[str, Any]] = {
    "uuid": "00000000-0000-4000-8000-000000000000",
    "device_id": "100000000000000000",
}


@dataclass(frozen=True)
class Vector:
    """一条抓包：给 `sign_yy` 的输入 + 上游实际返回的 `yy`。"""

    name: str
    method: str
    path: str
    unix_ms: int
    body_json: str
    expect_yy: str
    #: 该票的 query 串（抓包里 unix 之后的固定部分）
    query_tail: str = (
        "&lang=zh-Intl&uuid=00000000-0000-4000-8000-000000000000"
        "&device_id=100000000000000000&os_name=Mac&browser_name=chrome"
        "&device_memory=32&cpu_core_num=10&browser_language=zh-CN"
        "&browser_platform=MacIntel&screen_width=2560&screen_height=1440"
    )

    @property
    def path_with_query(self) -> str:
        head = (
            f"/{self.path.lstrip('/')}?device_platform=web&app_id={APP_ID}"
            f"&version_code={VERSION_CODE}&biz_id={BIZ_ID}&unix={self.unix_ms}"
        )
        return head + self.query_tail


VECTORS: Final[tuple[Vector, ...]] = (
    Vector(
        name="batch（获取任务列表：cursor/limit/feedTypes）",
        method="POST",
        path="/api/feed/creation/my/batch",
        unix_ms=1789922801000,
        body_json=(
            '{"cursor":"","limit":30,"type":"next","scene":"create",'
            '"projectID":"0","feedTypes":[0,1,2,3,4,5,6]}'
        ),
        expect_yy="a3e160cb587e0728397f0edecc6221e2",
    ),
    Vector(
        name="processing #1（未带图片的项目态查询）",
        method="POST",
        path="/api/feed/creation/my/processing",
        unix_ms=1789922767000,
        body_json='{"projectID":"0"}',
        expect_yy="d769e03f5e9a659697e0447a38dc4148",
    ),
    Vector(
        name="processing #2",
        method="POST",
        path="/api/feed/creation/my/processing",
        unix_ms=1789922800000,
        body_json='{"projectID":"0"}',
        expect_yy="8c20dcf4c0bc728c42567a08556ae2ca",
    ),
    Vector(
        name="processing #3（图生图那次）",
        method="POST",
        path="/api/feed/creation/my/processing",
        unix_ms=1789922883000,
        body_json='{"projectID":"0"}',
        expect_yy="0e5927602668747b1782a727d275c3ab",
    ),
    Vector(
        name="policy_callback（图片上传确认）",
        method="POST",
        path="/v1/api/files/policy_callback",
        unix_ms=1789922854000,
        body_json=(
            '{"fileName":"4591a45f-0dfe-48cc-84ad-b32f28322f0b.jpeg",'
            '"originFileName":"021789133119513402c3547e09b7409e300241a04980e221786e7.jpeg",'
            '"dir":"moss/prod/2026-09-21-00/user/multi_chat_file",'
            '"endpoint":"oss-us-east-1.aliyuncs.com","bucketName":"hailuo-video",'
            '"size":"454426","mimeType":"jpeg",'
            '"fileMd5":"a34b5d98fbbbeb2f25da935f928ea2f8",'
            '"fileScene":10,"durationMs":0,"assetFileType":1}'
        ),
        expect_yy="b9a76cdcf77170736998ddb738c0ca00",
    ),
)


def compact_json(payload: Any) -> str:
    """`JSON.stringify` 的等价物：紧凑分隔符、不转义非 ASCII、**保留键序**。

    ⚠️ `ensure_ascii=False` 不是可选项：prompt 含中文时，
    上游签名用的是**未转义的 UTF-8 原文**，写成 `\\uXXXX` 必然签名不符。
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "APP_ID",
    "VERSION_CODE",
    "BIZ_ID",
    "SIGN_SUFFIX",
    "CAPTURE_DEVICE",
    "Vector",
    "VECTORS",
    "build_query",
    "compact_json",
    "md5_hex",
    "now_ms_truncated",
    "public_params",
    "sign_yy",
]
