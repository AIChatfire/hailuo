#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`yy` 签名 —— **5 条真实抓包，一条不符就红**。

这是本仓最强的回归网：签名错一个字符，所有端点在线上都会回 `code:2 请求异常`
（一个只告诉你"参数有问题"的笼统错误），而在离线测试里它**立刻**暴露。
"""
from __future__ import annotations

import hashlib

import pytest

from app.upstream.hailuo import sign


# ---------------------------------------------------------------------------
# 抓包向量
# ---------------------------------------------------------------------------


def test_all_capture_vectors_reproduce() -> None:
    """5 条抓包（建任务前的列表查询 / processing×3 / 上传回调）全部复算成功。"""
    failures = []
    for v in sign.VECTORS:
        got = sign.sign_yy(path_with_query=v.path_with_query, body_json=v.body_json,
                           time_ms=v.unix_ms, method=v.method)
        if got != v.expect_yy:
            failures.append(f"{v.name}: expect={v.expect_yy} got={got}")
    assert not failures, "签名向量复算失败：\n" + "\n".join(failures)


@pytest.mark.parametrize("vector", sign.VECTORS, ids=lambda v: v.name)
def test_vector_individually(vector: sign.Vector) -> None:
    """逐条单独跑 —— 失败时能直接看出是哪条抓包漂了。"""
    assert sign.sign_yy(
        path_with_query=vector.path_with_query, body_json=vector.body_json,
        time_ms=vector.unix_ms, method=vector.method) == vector.expect_yy


def test_vectors_are_actually_distinct() -> None:
    """守卫夹具本身：5 条向量必须给出 5 个**不同**的期望值。

    否则"全 PASS"可能只是因为我复制粘贴时把同一个值填了 5 遍。
    """
    assert len({v.expect_yy for v in sign.VECTORS}) == len(sign.VECTORS)
    assert len({v.unix_ms for v in sign.VECTORS}) == len(sign.VECTORS)


# ---------------------------------------------------------------------------
# 算法细节（每一条都对着一个真实踩坑点）
# ---------------------------------------------------------------------------


def test_encode_uri_component_matches_js_not_quote() -> None:
    """`encodeURIComponent` **不**转义 `-_.!~*'()` —— 用 `quote(safe="")` 会错。

    这 7 个字符在路径里极常见（`my/processing` 没有，但 `!~*'()` 可能出现在
    query 的任意值里，且 OSS 路径常带 `_`）。一旦多转义，签名必然不匹配。
    """
    for ch in "-_.!~*'()":
        assert sign._encode_uri_component(ch) == ch, f"{ch!r} 不该被转义"
    # 这些**必须**转义（与 quote(safe="") 一致的部分）
    assert sign._encode_uri_component("/") == "%2F"
    assert sign._encode_uri_component("?") == "%3F"
    assert sign._encode_uri_component("&") == "%26"
    assert sign._encode_uri_component("=") == "%3D"
    assert sign._encode_uri_component(":") == "%3A"
    # 中文走 UTF-8 逐字节百分号编码
    assert sign._encode_uri_component("猫") == "%E7%8C%AB"


def test_get_method_uses_empty_object_as_body_segment() -> None:
    """GET 的 body 段恒为 `"{}"`（上游 `JSON.stringify({})`），**不是空串**。

    写成空串是最容易犯的错：它让签名在 POST 上全对、在 GET 上全错，
    而 GET 端点在本服务里恰恰是上传策略那一步。
    """
    common = dict(path_with_query="/x?a=1", time_ms=1789922801000)
    as_get = sign.sign_yy(body_json='{"a":1}', method="GET", **common)
    as_empty = sign.sign_yy(body_json="{}", method="GET", **common)
    assert as_get == as_empty
    # 而 POST 会把 body 计入
    assert sign.sign_yy(body_json='{"a":1}', method="POST", **common) != as_get


def test_body_order_and_compactness_matter() -> None:
    """body 的**键序**与**紧凑格式**都是签名输入的一部分。

    上游是 `JSON.stringify(e.data)` —— 它保留键序、不加空格。
    所以"先 normalize 成排序键的 json.dumps(indent=2)"会让签名全错。
    """
    base = dict(path_with_query="/x", time_ms=1789922801000, method="POST")
    ordered = sign.sign_yy(body_json='{"a":1,"b":2}', **base)
    reordered = sign.sign_yy(body_json='{"b":2,"a":1}', **base)
    spaced = sign.sign_yy(body_json='{"a": 1, "b": 2}', **base)
    assert len({ordered, reordered, spaced}) == 3


def test_compact_json_preserves_key_order_and_non_ascii() -> None:
    """`compact_json` 必须等价于 `JSON.stringify`：键序保留、中文不转义。"""
    assert sign.compact_json({"b": 1, "a": 2}) == '{"b":1,"a":2}'
    assert sign.compact_json({"p": "一只猫"}) == '{"p":"一只猫"}'
    assert "\\u" not in sign.compact_json({"p": "一只猫"})


def test_now_ms_is_truncated_to_seconds() -> None:
    """`unix` 必须是**整秒**的毫秒值（`Date.parse(new Date().toString())`）。

    它同时出现在：查询参数 `unix`、签名里的 `md5(str(time))`。
    两者必须是**同一个数** —— 拿 `time.time()*1000` 取整会莫名其妙地差几十毫秒。
    """
    value = sign.now_ms_truncated()
    assert value % 1000 == 0


def test_existing_yy_replaces_body_segment() -> None:
    """上游 `bodyToYY` 分支：已有 yy 时它**顶替** body 段（保留以防上游行为变化）。"""
    base = dict(path_with_query="/x", time_ms=1789922801000, method="POST")
    normal = sign.sign_yy(body_json='{"a":1}', **base)
    replaced = sign.sign_yy(body_json='{"a":1}', existing_yy="deadbeef", **base)
    assert replaced != normal
    assert replaced == sign.sign_yy(body_json="deadbeef", **base)


def test_signature_is_md5_of_four_parts() -> None:
    """结构自证：`md5(encodeURIComponent(path) + "_" + body + md5(time) + "ooui")`。

    这条断言把**算法结构**钉死 —— 即使将来所有向量都被替换，
    只要结构变了它就会红。
    """
    path, body, ts = "/a/b?c=1", '{"x":1}', 1789922801000
    expected_input = (sign._encode_uri_component(path) + "_" + body
                      + hashlib.md5(str(ts).encode()).hexdigest() + "ooui")
    assert sign.sign_yy(path_with_query=path, body_json=body, time_ms=ts,
                        method="POST") == hashlib.md5(expected_input.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 公共参数
# ---------------------------------------------------------------------------


def test_public_params_carry_app_id_and_device() -> None:
    params = sign.public_params(unix=1789922801000, **sign.CAPTURE_DEVICE)
    assert params["device_platform"] == "web"
    assert params["app_id"] == sign.APP_ID == 3001
    assert params["version_code"] == sign.VERSION_CODE
    assert params["biz_id"] == 0
    assert params["unix"] == 1789922801000
    assert params["uuid"] == sign.CAPTURE_DEVICE["uuid"]
    assert params["device_id"] == sign.CAPTURE_DEVICE["device_id"]
    assert params["lang"] == "zh-Intl"
    # 这些来自"浏览器环境"，服务端必须显式注入
    for key in ("os_name", "browser_name", "device_memory", "cpu_core_num",
                "browser_language", "browser_platform", "screen_width", "screen_height"):
        assert key in params, f"公共参数缺 {key}（它参与签名）"


def test_public_params_key_order_is_stable() -> None:
    """键序即签名输入的一部分 ⇒ 同一入参必须给出同一 key 顺序。"""
    a = sign.public_params(unix=1, **sign.CAPTURE_DEVICE)
    b = sign.public_params(unix=1, **sign.CAPTURE_DEVICE)
    assert list(a) == list(b)


def test_build_query_skips_none() -> None:
    assert sign.build_query({"a": 1, "b": None, "c": "x"}) == "a=1&c=x"


def test_build_query_stringifies_ints() -> None:
    """上游 `String(value)` —— `app_id` 必须是 `"3001"` 而不是 `"3001.0"`。"""
    assert sign.build_query({"app_id": 3001, "unix": 1789922801000}) == \
        "app_id=3001&unix=1789922801000"
