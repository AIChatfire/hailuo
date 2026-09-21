#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入图处理：嗅探 / 下载 / 归一化 / 表单校验。

图生图是本项目重点能力 ⇒ 这一层错了，**上游会在很远的地方以模糊的方式失败**。
"""
from __future__ import annotations

import httpx
import pytest

from app import media
from app.errors import InvalidParameterError, UpstreamError

from .conftest import PNG_1PX, _make_png

@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """🔴 **测试绝不碰真实 DNS。**

    两个理由，都不是洁癖：
      1. `_is_private_host` 对真域名会解析 —— 那是一次真实网络调用，
         在受限环境里会被网络守卫直接杀掉整个进程（实测：整文件跑到一半 SIGTERM）；
      2. 依赖真实 DNS 的用例是**不确定**的（解析结果随网络变化）。

    默认把任何主机名解析到一个公网 IP；需要"解析到内网"的用例自行覆盖。
    """
    import socket as _socket

    def _resolve(host, port, *a, **k):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", _resolve)


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16


# ---------------------------------------------------------------------------
# 嗅探（看魔数，不看扩展名）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("data", "mime", "ext"), [
    (PNG_1PX, "image/png", "png"),
    (JPEG, "image/jpeg", "jpeg"),
    (GIF, "image/gif", "gif"),
    (WEBP, "image/webp", "webp"),
])
def test_sniff_recognises_real_formats(data, mime, ext) -> None:
    assert media.sniff_mime(data) == (mime, ext)


@pytest.mark.parametrize("data", [b"", b"not an image", b"<html>", b"\x00" * 32])
def test_sniff_returns_none_for_non_images(data) -> None:
    """认不出就返回 `None`（**不猜**）—— 猜错会让失败点落到生成阶段。"""
    assert media.sniff_mime(data) is None


def test_from_bytes_rejects_non_image() -> None:
    with pytest.raises(InvalidParameterError):
        media.from_bytes(b"definitely not an image")


def test_from_bytes_derives_name() -> None:
    blob = media.from_bytes(PNG_1PX)
    assert blob.mime == "image/png" and blob.name.endswith(".png")
    assert blob.size == len(PNG_1PX)


# ---------------------------------------------------------------------------
# image 字段形态
# ---------------------------------------------------------------------------


def test_accept_form_none_is_empty() -> None:
    assert media.accept_form(None) == []


def test_accept_form_rejects_bare_string() -> None:
    """🔴 `"image": "https://…"` 必须**明确拒绝**并给出正确写法。

    偷偷当单元素数组处理，就会长出"我传了 N 张却只用了 1 张"这类不可预测行为。
    """
    with pytest.raises(InvalidParameterError) as ei:
        media.accept_form("https://x/a.png")
    assert ei.value.param == "image"
    assert '["' in str(ei.value)


@pytest.mark.parametrize("bad", [123, {"url": "x"}, [None], [""], [1], ["  "]])
def test_accept_form_rejects_bad_items(bad) -> None:
    with pytest.raises(InvalidParameterError):
        media.accept_form(bad)


def test_accept_form_trims_and_preserves_order() -> None:
    got = media.accept_form([" a ", "b", "c"])
    assert got == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------


def _transport(data: bytes = PNG_1PX, status: int = 200,
               content_type: str = "image/png") -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda r: httpx.Response(status, content=data,
                                 headers={"Content-Type": content_type}))


def test_download_ok() -> None:
    blob = media.download_image("https://cdn.example.com/a.png",
                                transport=_transport())
    assert blob.mime == "image/png" and blob.data == PNG_1PX
    assert blob.origin == "https://cdn.example.com/a.png"


# ---------------------------------------------------------------------------
# 内联 base64（data URL）—— 本地图片唯一可行的入口
# ---------------------------------------------------------------------------


def _data_url(data: bytes = PNG_1PX, mime: str = "image/png") -> str:
    import base64

    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def test_blob_from_source_dispatches_http_to_download() -> None:
    blob = media.blob_from_source("https://cdn.example.com/a.png",
                                  transport=_transport())
    assert blob.data == PNG_1PX and blob.origin == "https://cdn.example.com/a.png"


def test_data_url_base64_ok() -> None:
    """本地图片 → base64 data URL ⇒ 与下载走**同一套**校验（魔数嗅探）。"""
    blob = media.blob_from_source(_data_url())
    assert blob.data == PNG_1PX and blob.mime == "image/png"
    assert blob.name.startswith("inline-") and blob.name.endswith(".png")
    assert blob.origin == "<inline base64>"


def test_data_url_rejects_mime_mismatch() -> None:
    """声明 `image/jpeg` 但字节是 PNG ⇒ **响亮报错**（不静默按魔数改判）。"""
    with pytest.raises(InvalidParameterError) as ei:
        media.blob_from_source(_data_url(mime="image/jpeg"))
    assert "魔数" in str(ei.value)


def test_data_url_rejects_non_image_bytes() -> None:
    with pytest.raises(InvalidParameterError) as ei:
        media.blob_from_source(_data_url(data=b"not an image"))
    assert "可识别" in str(ei.value)


def test_data_url_rejects_bad_base64_and_missing_payload() -> None:
    with pytest.raises(InvalidParameterError):
        media.blob_from_source("data:image/png;base64,!!!!")
    with pytest.raises(InvalidParameterError):
        media.blob_from_source("data:image/png;base64")


def test_data_url_rejects_oversized_before_decoding() -> None:
    """先按 base64 长度粗筛 —— 不为超大 payload 白解一遍。"""
    big = "data:image/png;base64," + "A" * 4000
    with pytest.raises(InvalidParameterError) as ei:
        media.blob_from_source(big, max_bytes=1024)
    assert "过大" in str(ei.value)


def test_download_retries_connection_failure() -> None:
    """🔴 下载是幂等的 ⇒ **连接级失败要重试**（2026-09-21 实测：并发场景下
    CDN/上游都会偶发掐掉新建连接，重试一次即可越过）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("handshake operation timed out")
        return httpx.Response(200, content=PNG_1PX,
                              headers={"Content-Type": "image/png"})

    blob = media.download_image("https://cdn.example.com/a.png",
                                transport=httpx.MockTransport(handler))
    assert calls["n"] == 2 and blob.data == PNG_1PX


def test_download_gives_up_after_three_attempts() -> None:
    """重试**有界**（3 次）—— 一直失败就响亮抛错，不无限拖住任务。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection reset")

    with pytest.raises(Exception) as ei:
        media.download_image("https://cdn.example.com/a.png",
                             transport=httpx.MockTransport(handler))
    assert calls["n"] == 3
    assert "重试 3 次" in str(ei.value)


@pytest.mark.parametrize("url", [
    "ftp://cdn.example.com/a.png",
    "file:///etc/passwd",
    "https:///a.png",
])
def test_download_rejects_non_http_or_hostless(url) -> None:
    with pytest.raises(InvalidParameterError):
        media.download_image(url, transport=_transport())


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "192.168.1.1",
                                  "169.254.1.1", "172.16.3.4", "[::1]"])
def test_download_blocks_internal_targets(host) -> None:
    """🔴 **SSRF 防线**：`image` 是**用户可控的出站目标**。

    不做这层，本服务就成了"替我访问内网"的工具。

    ⚠️ 这里只用 **IP 字面量**：判据走 `ipaddress`，**不做 DNS 解析**。
    域名那一侧由下一条用例用替身覆盖 —— 让测试依赖真实 DNS
    既慢又不确定（而且在本机沙箱里会被网络守卫生成信号杀掉整进程）。
    """
    with pytest.raises(InvalidParameterError) as ei:
        media.download_image(f"http://{host}/a.png", transport=_transport())
    assert "SSRF" in str(ei.value) or "内网" in str(ei.value)


def test_download_blocks_hostname_resolving_to_private(monkeypatch) -> None:
    """域名解析到内网 IP ⇒ 同样拒绝（DNS 用替身，不出网）。"""
    import socket as _socket

    def fake_getaddrinfo(host, port, *a, **k):
        return [(2, 1, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(InvalidParameterError):
        media.download_image("http://internal.example.com/a.png",
                             transport=_transport())


def test_unresolvable_host_is_not_blocked_by_the_ssrf_precheck(monkeypatch) -> None:
    """解析失败 ⇒ **不在这里拦** —— 它本来就连不上，不构成 SSRF 通道。

    反过来做（解析失败即拒绝）会把上游 CDN 的一次瞬时 DNS 抖动
    变成一条误导人的 `400 SSRF`。**残余风险 DNS rebinding 是已知未处理的**，
    写在这里而不是假装它不存在。
    """
    import socket as _socket

    def boom(host, port, *a, **k):
        raise _socket.gaierror("no such host")

    monkeypatch.setattr(_socket, "getaddrinfo", boom)
    blob = media.download_image("http://nowhere.invalid/a.png", transport=_transport())
    assert blob.mime == "image/png"     # 预检放行；真正的失败由连接层给出


def test_private_host_precheck_needs_no_dns_resolution() -> None:
    """IP 字面量的判定**不许触发解析** —— 零成本、零副作用，且离线可测。"""
    import socket as _socket

    called: list[str] = []

    def spy(host, port, *a, **k):
        called.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    original = _socket.getaddrinfo
    _socket.getaddrinfo = spy
    try:
        assert media._is_private_host("10.1.2.3") is True
        assert media._is_private_host("8.8.8.8") is False
    finally:
        _socket.getaddrinfo = original
    assert called == [], "IP 字面量不该触发 DNS"


def test_download_404_is_invalid_parameter() -> None:
    with pytest.raises(InvalidParameterError) as ei:
        media.download_image("https://cdn.example.com/missing.png",
                             transport=_transport(status=404))
    assert ei.value.param == "image"


def test_download_oversize_is_rejected() -> None:
    big = PNG_1PX + b"\x00" * 5000
    with pytest.raises(InvalidParameterError) as ei:
        media.download_image("https://cdn.example.com/big.png",
                             transport=_transport(data=big), max_bytes=1000)
    assert "过大" in str(ei.value)


def test_download_non_image_is_rejected() -> None:
    """`Content-Type` 说是图片但字节不是 ⇒ **按字节判**，拒绝。"""
    with pytest.raises(InvalidParameterError) as ei:
        media.download_image("https://cdn.example.com/fake.png",
                             transport=_transport(data=b"<html>x</html>",
                                                  content_type="image/png"))
    assert "可识别" in str(ei.value)


def test_download_network_error_is_upstream_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(UpstreamError):
        media.download_image("https://cdn.example.com/a.png",
                             transport=httpx.MockTransport(boom))


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_normalize_is_noop_when_within_limits() -> None:
    """🔴 **只做大不改小**：都在阈值内 ⇒ 一个字节都不动。

    重编码会损失画质，而调用方给的就是他要的。
    """
    blob = media.from_bytes(PNG_1PX, name="a.png")
    out, notes = media.normalize(blob, max_side=4096, max_bytes=4 * 1024 * 1024)
    assert out.data == PNG_1PX
    assert notes == []


def test_normalize_resizes_oversized_side() -> None:
    big = _make_png(400, 200)
    blob = media.from_bytes(big, name="big.png")
    out, notes = media.normalize(blob, max_side=100, max_bytes=10 ** 9)
    assert notes and "长边超过" in notes[0]
    assert out.width == 100
    assert out.height == 50           # 比例守恒
    assert out.mime == "image/jpeg"   # 缩放后统一转 JPEG
    assert out.name.endswith(".jpeg")  # 名字跟着换扩展名


def test_normalize_shrinks_oversized_bytes() -> None:
    # 真噪声图 ⇒ PNG 压不动、体积确实超标（纯色图会小到测不出这条路径）
    blob = media.from_bytes(_make_png(200, 200, noise=True), name="big.png")
    assert blob.size > 50_000, "前提：这张图确实很大"
    out, notes = media.normalize(blob, max_side=10000, max_bytes=100_000)
    assert out.size < blob.size, "重编码应当把体积压下来"
    assert any("超过" in n and "压到" in n for n in notes)


def test_normalize_never_makes_the_file_bigger() -> None:
    """🔴 重编码**不许把图变大**。

    实测踩到：一张 786B 的合成图案 PNG 转 JPEG 后变成 4227B（5 倍）。
    PNG 对合成/线条类图像极高效，而 JPEG 对它们很不划算 ⇒
    无条件"转 JPEG 以缩小体积"会把小图弄大，白费带宽还可能顶破上游限制。
    """
    blob = media.from_bytes(_make_png(120, 120), name="flat.png")
    out, notes = media.normalize(blob, max_side=10000, max_bytes=1)
    assert out.size <= blob.size, "宁可保持原图，也不能变大"
    assert out.data == blob.data
    joined = " ".join(notes)
    assert "反而更大" in joined and "保持原图上传" in joined


def test_normalize_reports_when_target_is_unreachable() -> None:
    """🔴 **达不到目标必须说出来** —— 悄悄返回一个仍然超标的文件，
    会让调用方以为"已经压到目标了"，而它会在上游以别的方式失败。

    这张 120×120 噪声图即便降到 JPEG q=40 也压不到 300B —— 正是要覆盖的分支。
    """
    blob = media.from_bytes(_make_png(120, 120, noise=True), name="noisy.png")
    out, notes = media.normalize(blob, max_side=10000, max_bytes=300)
    assert out.size > 300, "前提：这个目标确实达不到"
    joined = " ".join(notes)
    assert "无法在不改尺寸的前提下压到目标" in joined
    assert "已按能压到的最大程度上传" in joined
    # 必须给出下一步该怎么做（否则这条降级说明是不可执行的）
    assert "NORMALIZE_MAX_SIDE" in joined


def test_normalize_respects_disable_switch() -> None:
    big = _make_png(300, 300)
    blob = media.from_bytes(big, name="b.png")
    out, notes = media.normalize(blob, max_side=100, enabled=False)
    assert out.data == blob.data and notes == []


def test_normalize_survives_undecodable_input() -> None:
    """魔数过了但 Pillow 解不开 ⇒ **交给上游去判**，不在这里拦。"""
    blob = media.ImageBlob(data=b"\x89PNG\r\n\x1a\n" + b"garbage", mime="image/png",
                           ext="png", name="x.png")
    out, notes = media.normalize(blob, max_side=10)
    assert out.data == blob.data and notes == []


def test_supported_mime_is_a_deliberate_short_list() -> None:
    """上游能吃什么格式**没逐一取证** ⇒ 只列确定的那几个，别扩。"""
    assert media.SUPPORTED_MIME == {"image/jpeg", "image/png", "image/webp"}
