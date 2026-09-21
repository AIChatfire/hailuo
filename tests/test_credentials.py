#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""透传凭据：JWT 解析边界 + 池行为。

这些用例守的是**入口鉴权**（本服务唯一的安全边界）：
校验松一格，任何人都能让服务拿别人/服务端的账号去花额度。
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from app.credentials import CredentialPool, parse_jwt
from app.errors import AuthError

from .conftest import FAKE_JWT, FAKE_JWT_B


def _jwt(payload: dict, *, signature: str = "sig") -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.{signature}"


# --------------------------------------------------------------- 解析边界


def test_parses_valid_jwt_and_extracts_claims() -> None:
    claims = parse_jwt(FAKE_JWT)
    assert claims.device_id == "100000000000000000"
    assert claims.subject == "test-user"
    assert claims.exp and claims.exp > time.time()


def test_rejects_non_jwt_strings() -> None:
    """不是三段式 ⇒ 401，且信息要说明**该传什么**（不是泛泛的"无效"）。"""
    for bad in ("sk-whatever", "a.b", "a.b.c.d", "", "eyJx.eyJy"):
        with pytest.raises(AuthError) as ei:
            parse_jwt(bad)
        assert "JWT" in str(ei.value)


def test_rejects_unparseable_payload() -> None:
    with pytest.raises(AuthError) as ei:
        parse_jwt("eyJhbGciOiJIUzI1NiJ9.!!!not-base64!!!.sig")
    assert "payload" in str(ei.value)


def test_rejects_expired_jwt() -> None:
    """`exp` 过期 ⇒ 401，且提示"重新登录取新 token"（可执行）。"""
    past = _jwt({"exp": int(time.time()) - 3600, "user": {"id": "1"}})
    with pytest.raises(AuthError) as ei:
        parse_jwt(past)
    assert "过期" in str(ei.value)


def test_accepts_jwt_without_exp() -> None:
    """没有 `exp` 声明的 token：本地无从判断 ⇒ 放行（由上游兜底否掉）。"""
    claims = parse_jwt(_jwt({"user": {"id": "1", "name": "n"}}))
    assert claims.exp is None and claims.subject == "n"


def test_clock_skew_tolerance_sixty_seconds() -> None:
    """刚过期 30s 的 token 仍放行（时钟偏差），过期 5 分钟则拒。"""
    assert parse_jwt(_jwt({"exp": int(time.time()) - 30})).exp is not None
    with pytest.raises(AuthError):
        parse_jwt(_jwt({"exp": int(time.time()) - 300}))


# --------------------------------------------------------------- 池行为


def test_pool_put_get_and_forget() -> None:
    pool = CredentialPool()
    claims = parse_jwt(FAKE_JWT)
    pool.put("fp-a", FAKE_JWT, claims)
    assert pool.stats()["entries"] == 1
    got = pool.clients_for("fp-a", _settings())
    assert got is not None, "登记过的凭据必须能取到客户端"
    assert pool.clients_for("fp-unknown", _settings()) is None
    assert pool.forget("fp-a") is True
    assert pool.clients_for("fp-a", _settings()) is None


def test_pool_returns_same_client_pair_for_same_credential() -> None:
    """同一凭据重复取 ⇒ **同一个**客户端实例（否则每次请求新建连接池会泄漏）。"""
    pool = CredentialPool()
    pool.put("fp", FAKE_JWT, parse_jwt(FAKE_JWT))
    first = pool.clients_for("fp", _settings())
    again = pool.clients_for("fp", _settings())
    assert first is not None and first[0] is again[0] and first[1] is again[1]
    pool.close_all()


def test_pool_evicts_lru_beyond_capacity() -> None:
    """容量上限：淘汰**最久未使用**的条目（内存兜底，不能当仓库用）。"""
    pool = CredentialPool(max_entries=2)
    for i, tok in enumerate((FAKE_JWT, FAKE_JWT_B)):
        pool.put(f"fp-{i}", tok, parse_jwt(tok))
    _ = pool.clients_for("fp-0", _settings())      # fp-0 变"最近使用"
    pool.put("fp-2", FAKE_JWT, parse_jwt(FAKE_JWT))
    assert pool.stats()["entries"] == 2
    assert pool.clients_for("fp-0", _settings()) is not None, "最近用过的应保留"
    assert pool.clients_for("fp-1", _settings()) is None, "最久未用的应被淘汰"
    pool.close_all()


def test_pool_drops_expired_entries_on_read() -> None:
    """**过期即弃**：exp 一过就取不到（上游必拒，留着只会多失败一次）。"""
    pool = CredentialPool()
    alive = _jwt({"exp": int(time.time()) + 30, "user": {"id": "1"}})
    pool.put("fp-alive", alive, parse_jwt(alive))
    assert pool.clients_for("fp-alive", _settings()) is not None, "未过期必须可用"

    gone = _jwt({"exp": int(time.time()) - 10, "user": {"id": "2"}})
    pool.put("fp-gone", gone, parse_jwt(gone))
    assert pool.clients_for("fp-gone", _settings()) is None
    assert "fp-gone" not in [k for k in ("fp-alive", "fp-gone")
                             if pool.clients_for(k, _settings()) is not None]
    pool.close_all()


def test_two_credentials_get_distinct_clients() -> None:
    """两个凭据 ⇒ 两套客户端（token/设备档都独立）—— 透传路由的基础。"""
    pool = CredentialPool()
    pool.put("fp-a", FAKE_JWT, parse_jwt(FAKE_JWT))
    pool.put("fp-b", FAKE_JWT_B, parse_jwt(FAKE_JWT_B))
    a = pool.clients_for("fp-a", _settings())
    b = pool.clients_for("fp-b", _settings())
    assert a is not None and b is not None and a[0] is not b[0]
    #: 设备档按 token 声明覆盖 ⇒ 两个凭据的 device_id 必须不同
    assert a[0].device["device_id"] != b[0].device["device_id"]
    pool.close_all()


def _settings():
    from app.config import Settings

    return Settings(task_db="sqlite+pysqlite:///:memory:")
