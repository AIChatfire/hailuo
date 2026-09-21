#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调用方凭据（**透传模式**：入口 JWT 即上游凭证）。

## 为什么不落库

本仓铁律：**明文凭据永不落库**（库里只存指纹）。透传模式下 token 需要在
「受理 → 提交 → 轮询」之间存活 ⇒ 只能放**进程内存**：

- 进程重启 ⇒ 池空 ⇒ 在途任务在下一轮 tick 以**明确错误**失败
  （不静默卡死、也绝不拿别人的 token 顶替 —— 见 `CredentialUnavailable`）；
- TTL/容量双上限，避免把进程内存当仓库用。

## 池的过期判据：**过期即弃**（不留宽限期）

`exp` 一过，上游必拒；留着一个死 token 只会让请求多失败一次、把原因搅浑。
（入口 `parse_jwt` 另留 60s 时钟偏差容忍 —— 那是给"刚过期就发出的请求"的余地，
两者语义不同、刻意分开。）

## 校验到什么程度（如实标注）

服务**不验签**（没有签名密钥，也无从获得）：只解码 `exp` 与结构。
真正的校验由**上游**做 —— 伪造/过期 token 会在上游拿到 401。
本层的作用是"早失败 + 少一次往返"，不是安全边界。

## device_id 的取值（未取证的取舍）

`device_id` 在签名 `yy` 里被使用。透传 token 的 `user.deviceID` 声明是
**该 token 自己的设备** ⇒ 取声明值（与真实客户端行为一致）；
`uuid` 声明 token 里没有 ⇒ 沿用服务端 profile。
⚠️ 本机首个真实用例中两者恰好一致（同一台设备），**跨设备 token 未实测**。
"""
from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from .errors import AuthError

#: 池容量（按凭据指纹计）。超出时淘汰"最久未使用"的条目。
#: 128 远大于正常并发用户数，纯粹是内存兜底。
POOL_MAX_ENTRIES = 128


@dataclass(frozen=True)
class JwtClaims:
    """从 JWT payload 里取出的、我们真正用得上的字段。"""

    exp: float | None
    device_id: str | None
    subject: str


def parse_jwt(token: str) -> JwtClaims:
    """解码 JWT（**不验签**）并做本地校验。不合法 ⇒ `AuthError`（401）。

    判据（每条都对应一种真实的坏输入）：
      1. 必须是三段式（`header.payload.signature`）；
      2. payload 必须能 base64url 解码且是 JSON 对象；
      3. `exp` 若存在则必须**未过期**（留 60s 时钟偏差余量）。
    """
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise AuthError(
            "Authorization 必须是 hailuo 的 JWT（三段式 header.payload.signature）—— "
            "形如 eyJhbGciOi….<payload>.<sig>。")
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise AuthError(f"Bearer token 不是合法的 JWT（payload 解码失败：{e}）") from e
    if not isinstance(payload, dict):
        raise AuthError("Bearer token 的 JWT payload 不是 JSON 对象。")

    exp_raw = payload.get("exp")
    exp: float | None = None
    if isinstance(exp_raw, (int, float)):
        exp = float(exp_raw)
        if exp <= time.time() - 60:
            raise AuthError(
                "Bearer token 已过期（JWT 的 exp 早于当前时间）—— 请重新登录取新 token。")
    user = payload.get("user") or {}
    device_id = str(user.get("deviceID") or "") or None
    subject = str(user.get("name") or user.get("id") or "unknown")
    return JwtClaims(exp=exp, device_id=device_id, subject=subject)


@dataclass
class _Entry:
    token: str
    exp: float | None
    subject: str
    device_id: str | None
    last_used: float = field(default_factory=time.time)
    client: Any = None      # HailuoClient（惰性）
    uploader: Any = None    # Uploader（惰性）


class CredentialPool:
    """`指纹 → 凭据(+惰性客户端)`。线程安全（API 线程 + 协调器线程共用）。"""

    def __init__(self, *, max_entries: int = POOL_MAX_ENTRIES,
                 transport: Any = None) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self.max_entries = max_entries
        #: 池内客户端共用的 httpx transport —— 测试/嵌入方注入后**零出网**，
        #: 与服务其它部分的 `http_transport` 语义保持一致。
        self._transport = transport

    # ------------------------------------------------------------------ 写

    def put(self, fingerprint: str, token: str, claims: JwtClaims) -> None:
        with self._lock:
            self._entries[fingerprint] = _Entry(
                token=token, exp=claims.exp, subject=claims.subject,
                device_id=claims.device_id)
            self._evict_locked()

    def _evict_locked(self) -> None:
        """过期先清；仍超限 ⇒ 淘汰最久未使用（**可能在途** —— 见类文档的语义）。"""
        now = time.time()
        for fp in [k for k, e in self._entries.items() if e.exp and e.exp <= now]:
            self._entries.pop(fp, None)
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k].last_used)
            logger.warning(f"凭据池超限，淘汰最久未用条目：{oldest[:12]}…")
            self._entries.pop(oldest, None)

    # ------------------------------------------------------------------ 读

    def get(self, fingerprint: str) -> _Entry | None:
        with self._lock:
            entry = self._entries.get(fingerprint)
            if entry is None:
                return None
            if entry.exp and entry.exp <= time.time():
                self._entries.pop(fingerprint, None)
                return None
            entry.last_used = time.time()
            return entry

    def forget(self, fingerprint: str) -> bool:
        with self._lock:
            return self._entries.pop(fingerprint, None) is not None

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            alive = sum(1 for e in self._entries.values()
                        if not e.exp or e.exp > now)
            return {"entries": len(self._entries), "alive": alive,
                    "max_entries": self.max_entries}

    def clients_for(self, fingerprint: str, settings: Any) -> tuple[Any, Any] | None:
        """取该凭据的 `(HailuoClient, Uploader)`；凭据不在池里 ⇒ `None`。

        客户端惰性创建**在锁内**完成（构造只建 httpx 连接池、**零网络 I/O**）——
        放锁外会让并发首访各建一份客户端（连接池泄漏）。
        """
        with self._lock:
            entry = self._entries.get(fingerprint)
            if entry is None:
                return None
            if entry.exp and entry.exp <= time.time():
                self._entries.pop(fingerprint, None)
                return None
            entry.last_used = time.time()
            if entry.client is None or entry.uploader is None:
                entry.client, entry.uploader = _make_client(
                    entry=entry, settings=settings, transport=self._transport)
            return entry.client, entry.uploader

    def close_all(self) -> None:
        with self._lock:
            for entry in self._entries.values():
                for obj in (entry.uploader, entry.client):
                    if obj is not None:
                        try:
                            obj.close()
                        except Exception as e:  # noqa: BLE001
                            logger.debug(f"凭据客户端关闭失败（已忽略）：{e}")
            self._entries.clear()


def _make_client(*, entry: _Entry, settings: Any,
                 transport: Any = None) -> tuple[Any, Any]:
    """为一条凭据惰性建 `(HailuoClient, Uploader)` —— 设备档按 token 声明覆盖。"""
    import httpx  # noqa: PLC0415
    from .upstream.hailuo import upload as up_mod  # noqa: PLC0415
    from .upstream.hailuo.client import HailuoClient  # noqa: PLC0415

    device = settings.device_profile()
    if entry.device_id:
        #: 透传 token 的设备声明优先（签名 yy 用的是"那台设备"的 id）
        device = {**device, "device_id": entry.device_id}
    client = HailuoClient(token=entry.token, base_url=settings.hailuo_base_url,
                          device=device, transport=transport)
    uploader = up_mod.Uploader(
        client=client, cache_ttl=settings.upload_cache_ttl,
        oss_client=httpx.Client(transport=transport, timeout=60.0,
                                follow_redirects=True) if transport else None)
    return client, uploader


__all__ = [
    "POOL_MAX_ENTRIES",
    "CredentialPool",
    "JwtClaims",
    "parse_jwt",
]
