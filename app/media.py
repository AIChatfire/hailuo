#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入图处理：嗅探 / 下载 / 归一化。

## 为什么要有这一层

上游 `fileList[]` 要的是**已经躺在 hailuo OSS 上的素材**，不是任意 URL。
所以 `image: ["https://…"]` 必须走：下载 → 嗅探真格式 → （必要时）归一化 → 上传。

## 三条纪律

1. **格式看魔数，不看扩展名也不看 `Content-Type`。** 上游按真字节判定；
   拿 `.png` 的扩展名骗它，失败点会落在很远的地方（生成阶段的模糊报错）。
2. **归一化必须留痕。** 缩放/转码会改变调用方给的东西 ⇒ 进 `degradations`。
   静默改图 = 让人按 A 的预期为 B 的结果付钱。
3. **大小上限是「本地防线」不是「上游契约」。** 上游没给体积上限
   ⇒ 只做保守压缩（`normalize_*`），不设"超了就拒"的硬阈值。
"""
from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from .errors import InvalidParameterError, UpstreamError

# ---------------------------------------------------------------------------
# 魔数嗅探
# ---------------------------------------------------------------------------

_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"BM", "image/bmp", "bmp"),
)

#: 上游 `fileList[].frameType` / 上游能吃的格式。别扩它 —— 没实测过的一律不加。
SUPPORTED_MIME: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})


def sniff_mime(data: bytes) -> tuple[str, str] | None:
    """返回 `(mime, ext)`；认不出返回 `None`（**不猜**）。"""
    for magic, mime, ext in _MAGIC:
        if data.startswith(magic):
            return mime, ext
    # RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------


@dataclass
class ImageBlob:
    """一张可上传的输入图。"""

    data: bytes
    mime: str
    ext: str
    name: str
    origin: str = ""
    width: int | None = None
    height: int | None = None

    @property
    def size(self) -> int:
        return len(self.data)


def _classify_ip(addr: str) -> bool | None:
    """`True` = 内网/不可达；`False` = 公网；`None` = 不是合法 IP。"""
    try:
        ip = ipaddress.ip_address(addr.strip("[]"))
    except ValueError:
        return None
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _is_private_host(host: str) -> bool:
    """SSRF 防线：拒绝指向内网/回环/链路本地的 URL。

    调用方能给 `image: ["http://…"]` ⇒ 这是一个**用户可控的出站目标**。
    不做这层，本服务就成了访问调用方内网的工具。

    ⚠️ **先按字面量判，再解析**：
      · 主机本身就是 IP（`127.0.0.1` / `10.0.0.5` / `169.254.1.1` …）⇒
        **直接用 `ipaddress` 判定，不做任何解析**。这是最常见的一类攻击目标，
        而纯字符串判定是零成本、零副作用的；
      · 真域名 ⇒ 才需要解析。

    🔴 **已知边界（不粉饰）**：解析**失败**时不在这里拦。
    理由：解析不出来的主机**根本连不上**，它不构成 SSRF 通道；
    而"解析失败即拒绝"会把上游 CDN 的一次瞬时 DNS 抖动
    变成一条误导人的 `400 SSRF`。真正的残余风险是 **DNS rebinding**
    （校验时解析到公网、连接时解析到内网）—— 那需要"按解析出的 IP 直连"
    才能根治，本服务**没做**。见 `docs/UPSTREAM.md` 的诚实边界一节。
    """
    literal = _classify_ip(host)
    if literal is not None:
        return literal

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        #: 解析失败 ⇒ 不拦（它连不上，不是 SSRF 通道）
        return False
    for info in infos:
        if _classify_ip(info[4][0]) is True:
            return True
    return False


def download_image(
    url: str, *, timeout: float = 20.0, max_bytes: int = 32 * 1024 * 1024,
    transport: httpx.BaseTransport | None = None,
) -> ImageBlob:
    """下载 + 嗅探。任何一步不明确就抛错（**不静默退回默认格式**）。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise InvalidParameterError(
            f"image 只接受 http(s) URL，实得 {parsed.scheme or '（无 scheme）'!r}：{url}",
            param="image")
    if not parsed.hostname:
        raise InvalidParameterError(f"image URL 缺少主机名：{url}", param="image")
    if _is_private_host(parsed.hostname):
        raise InvalidParameterError(
            f"image URL 指向内网/回环地址，已拒绝（SSRF 防线）：{parsed.hostname}",
            param="image")

    #: 🔴 **连接级失败要重试**（2026-09-21 实测：并发场景下 CDN 与上游都会偶发
    #: 掐掉新建连接 —— `ConnectTimeout` / `Server disconnected`）。下载是幂等的，
    #: 重试零副作用；同一 Client 复用连接，退避取小值（这不是限流，是建连抖动）。
    resp: httpx.Response | None = None
    last_err: Exception | None = None
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      transport=transport) as c:
        for attempt in range(1, 4):
            try:
                resp = c.get(url, headers={"Accept": "image/*"})
                break
            except httpx.HTTPError as e:
                last_err = e
                if attempt >= 3:
                    raise UpstreamError(
                        f"下载输入图失败（已重试 3 次）：{url}"
                        f"（{type(e).__name__}: {e}）") from e
                time.sleep(0.2 * attempt)
    if resp is None:  # pragma: no cover —— 循环要么 break 要么 raise
        raise UpstreamError(f"下载输入图失败：{url}（{last_err}）")

    if resp.status_code != 200:
        raise InvalidParameterError(
            f"输入图下载失败（HTTP {resp.status_code}）：{url}", param="image")
    data = resp.content
    if len(data) > max_bytes:
        raise InvalidParameterError(
            f"输入图过大（{len(data)}B > 上限 {max_bytes}B）：{url}", param="image")
    found = sniff_mime(data)
    if not found:
        raise InvalidParameterError(
            f"输入图不是可识别的图片格式（只支持 JPEG/PNG/WebP）：{url}", param="image")
    mime, ext = found
    name = (parsed.path.rsplit("/", 1)[-1] or f"input.{ext}").split("?")[0]
    return ImageBlob(data=data, mime=mime, ext=ext, name=name, origin=url)


def from_bytes(data: bytes, *, name: str = "") -> ImageBlob:
    """本地字节 → `ImageBlob`（`data:` URL 与内联 base64 走这里）。"""
    found = sniff_mime(data)
    if not found:
        raise InvalidParameterError("输入图不是可识别的图片格式", param="image")
    mime, ext = found
    return ImageBlob(data=data, mime=mime, ext=ext, name=name or f"input.{ext}")


def normalize(
    blob: ImageBlob, *, max_side: int = 4096, max_bytes: int = 4 * 1024 * 1024,
    enabled: bool = True,
) -> tuple[ImageBlob, list[str]]:
    """保守归一化：超长边缩放、超大体积转 JPEG。返回 `(新 blob, 降级说明)`。

    ⚠️ **只做大不改小**：尺寸与体积都在阈值内 ⇒ **一个字节都不动**
    （重编码会损失画质，而调用方给的就是他要的）。
    """
    notes: list[str] = []
    if not enabled:
        return blob, notes
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        logger.debug("未安装 Pillow，跳过输入图归一化")
        return blob, notes

    try:
        img = Image.open(io.BytesIO(blob.data))
        img.load()
    except Exception as e:  # noqa: BLE001
        # 嗅探通过但解不开 ⇒ 交给上游去判，**不在这里拦**（生成失败会上游报错）
        logger.debug(f"Pillow 打不开输入图，跳过归一化：{type(e).__name__}: {e}")
        return blob, notes

    width, height = img.size
    need_resize = max(width, height) > max_side
    need_shrink = blob.size > max_bytes
    if not need_resize and not need_shrink:
        return ImageBlob(**{**blob.__dict__, "width": width, "height": height}), notes

    out = img
    if need_resize:
        ratio = max_side / float(max(width, height))
        new_size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
        out = img.resize(new_size, Image.LANCZOS)
        notes.append(
            f"输入图 {width}×{height} 长边超过 {max_side} ⇒ 已缩放到 "
            f"{new_size[0]}×{new_size[1]}（守恒比例）。")

    buf = io.BytesIO()
    if out.mode not in ("RGB", "L"):
        out = out.convert("RGB")
    fmt, mime, ext = "JPEG", "image/jpeg", "jpeg"
    quality = 88
    while True:
        buf.seek(0)
        buf.truncate()
        out.save(buf, format=fmt, quality=quality, optimize=True)
        if buf.tell() <= max_bytes or quality <= 40:
            break
        quality -= 12
    data = buf.getvalue()
    resized = out.size != (width, height)

    # 🔴 **重编码不许把图变大。**
    # 实测踩到：一张 786B 的 120×120 PNG 转成 JPEG q=88 反而变成 4227B（5 倍）。
    # PNG 对合成图/线条图极高效，而 JPEG 对它们很不划算 ⇒
    # 无条件"转 JPEG 以缩小体积"会把一张本来就小的图弄大，
    # 既浪费上传带宽，也可能顶破上游的体积限制。
    if need_shrink and not resized and len(data) >= blob.size:
        notes.append(
            f"输入图 {blob.size}B 超过目标 {max_bytes}B，但重编码为 JPEG 反而更大"
            f"（{len(data)}B：本图是 PNG 更擅长的合成/线条类图像）⇒ **保持原图上传**。"
            f"要真正压小只能改尺寸（调低 NORMALIZE_MAX_SIDE）。")
        return ImageBlob(**{**blob.__dict__, "width": width, "height": height}), notes

    if need_shrink and not resized:
        if len(data) <= max_bytes:
            notes.append(
                f"输入图 {blob.size}B 超过 {max_bytes}B ⇒ 已重编码为 JPEG(q={quality}) "
                f"压到 {len(data)}B。")
        else:
            # 🔴 **达不到目标必须说出来。** 悄悄返回一个仍然超标的文件，
            # 会让调用方以为"已经压到目标了" —— 而它会在上游以别的方式失败。
            notes.append(
                f"输入图 {blob.size}B 超过目标 {max_bytes}B，但即便降到 JPEG q=40 仍为 "
                f"{len(data)}B（画面复杂度太高，无法在不改尺寸的前提下压到目标）。"
                f"**已按能压到的最大程度上传**；若要更小请自行缩小尺寸"
                f"（或调低 NORMALIZE_MAX_BYTES 并同时调低 NORMALIZE_MAX_SIDE）。")
    elif resized and blob.size != len(data):
        notes.append(f"输入图已重编码为 JPEG(q={quality})：{len(data)}B。")

    return ImageBlob(data=data, mime=mime, ext=ext, name=_rebase_name(blob.name, ext),
                     origin=blob.origin, width=out.size[0], height=out.size[1]), notes


def _rebase_name(name: str, ext: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem or 'input'}.{ext}"


def accept_form(value: Any) -> list[str]:
    """把 `image` 字段规范成**输入源**列表（http(s) URL 或 `data:` base64 URL）。

    🔴 **只接受数组**（与 jimeng 同一纪律）：`"image": "https://…"` 是很常见的
    写法，但把它当单元素数组处理会让人以为"我传了 3 个只用了 1 个"的镜像问题
    —— 不如直接告诉他正确写法。

    **本地图片**走 `data:image/png;base64,…`（HTTP 接口不收裸文件）——
    调用方 base64 编码即可，无需先传到公网。
    """
    if value is None:
        return []
    if isinstance(value, str):
        raise InvalidParameterError(
            'image 必须是**数组**，即使只有一张也要写成 ["…"]。'
            f"实得字符串 {value[:60]!r}。", param="image")
    if not isinstance(value, (list, tuple)):
        raise InvalidParameterError(
            f"image 必须是数组，实得 {type(value).__name__}。", param="image")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidParameterError(
                "image 的每一项都必须是非空字符串（http(s) URL 或 data: base64 URL），"
                f"实得 {item!r}。", param="image")
        out.append(item.strip())
    return out


def blob_from_source(
    src: str, *, timeout: float = 20.0, max_bytes: int = 32 * 1024 * 1024,
    transport: httpx.BaseTransport | None = None,
) -> ImageBlob:
    """输入图**统一入口**：`data:` base64 URL 或 http(s) URL。

    本地图片只能以 base64 形态进来（HTTP 接口不收裸文件）——
    解码后走与下载**完全相同**的校验链（体积上限 / 魔数嗅探），
    不做任何"信任调用方"的捷径。
    """
    if src.startswith("data:"):
        return _blob_from_data_url(src, max_bytes=max_bytes)
    return download_image(src, timeout=timeout, max_bytes=max_bytes, transport=transport)


def _blob_from_data_url(src: str, *, max_bytes: int) -> ImageBlob:
    """`data:image/png;base64,…` → `ImageBlob`。

    ⚠️ **声明的 mime 与魔数不符 ⇒ 响亮报错**（不静默按魔数改判）——
    调用方若把 JPEG 标成 png，我们按哪个走都"看起来对"，但计费与调试都会走偏。
    """
    head, _, payload = src.partition(",")
    if not payload:
        raise InvalidParameterError(
            "data URL 缺少 `,` 分隔的 base64 数据（形如 data:image/png;base64,…）",
            param="image")
    if ";base64" not in head.lower():
        raise InvalidParameterError(
            "data URL 必须是 `data:image/*;base64,…` 形态（不支持 URL-encoded 明文）",
            param="image")
    #: 先按 base64 长度粗筛，避免为一个超大 payload 先解码再拒绝
    approx = len(payload) * 3 // 4
    if approx > max_bytes:
        raise InvalidParameterError(
            f"输入图过大（≈{approx}B > 上限 {max_bytes}B）", param="image")
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception as e:  # noqa: BLE001
        raise InvalidParameterError(f"data URL 的 base64 解码失败：{e}", param="image") from e
    if len(data) > max_bytes:
        raise InvalidParameterError(
            f"输入图过大（{len(data)}B > 上限 {max_bytes}B）", param="image")

    declared = head[len("data:"):].split(";", 1)[0].strip().lower()
    found = sniff_mime(data)  # 🔴 按**魔数**判定，不信任声明
    if not found:
        raise InvalidParameterError(
            "输入图不是可识别的图片格式（只支持 JPEG/PNG/WebP）", param="image")
    mime, ext = found
    if declared and declared not in (mime, "image/jpg" if mime == "image/jpeg" else mime):
        raise InvalidParameterError(
            f"data URL 声明 {declared!r}，但字节的魔数是 {mime!r} —— "
            f"请按真实格式编码（不静默改判）。", param="image")
    name = f"inline-{hashlib.md5(data).hexdigest()[:8]}.{ext}"
    return ImageBlob(data=data, mime=mime, ext=ext, name=name,
                     origin="<inline base64>")


__all__ = [
    "SUPPORTED_MIME",
    "ImageBlob",
    "accept_form",
    "blob_from_source",
    "download_image",
    "from_bytes",
    "normalize",
    "sniff_mime",
]
