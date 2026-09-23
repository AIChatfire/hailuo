#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入图上传 —— 阿里云 OSS（STS 临时凭据）三段式。

## 抓包还原的完整链路

```
① GET  /v1/api/files/request_policy          → accessKeyId / accessKeySecret /
                                               securityToken / expiration /
                                               dir / endpoint / bucketName
② PUT  https://{bucket}.{endpoint}/{dir}/{fileName}   ← 图片字节直接传 OSS
        Authorization: OSS {ak}:{签名}
        x-oss-security-token: {securityToken}
③ POST /v1/api/files/policy_callback         → {fileName, originFileName, dir,
                                               endpoint, bucketName, size, mimeType,
                                               fileMd5, fileScene, durationMs, assetFileType}
```

第 ③ 步的请求体**逐字段**来自抓包（见 `sign.VECTORS` 里那条 `policy_callback` 向量）。

## 四个刻意的取舍

1. **上传结果按 `fileMd5` 缓存**（`UPLOAD_CACHE_TTL`，默认 6h）。
   上游是否回收未引用的素材**未知** ⇒ 取保守值。缓存命中的话，
   连 `request_policy` 都不用打。
   🔴 缓存读写受锁保护 —— 多张输入图**并行 ingest** 时（`INGEST_PARALLELISM`），
   上传本身在锁外并发，只有缓存读判定与写回在锁内（避免同 md5 重复上传）。
2. **`fileName` 用 UUID**（`{uuid}.{ext}`），并**原样带回 `originFileName`** ——
   抓包里就是这个形态（`fileName` 是 UUID、`originFileName` 是原名）。
3. **`fileScene=10` / `assetFileType=1`（图片）** —— 抓包实值。音频 2、视频 3
   （前端 `cG()` 按 mimeType 推导），本服务只做图片，故恒为 1。

## 不做什么

- **不转存产物**（产物是 `cdn.hailuoai.video` 直链，转存是另一个决定）；
- **不猜体积上限** —— 服务端没给，所以只做保守压缩（`media.py`），不设硬阈值。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from ...errors import UpstreamError

PATH_REQUEST_POLICY = "/v1/api/files/request_policy"
PATH_POLICY_CALLBACK = "/v1/api/files/policy_callback"

#: 抓包实值：图片场景。前端 `cG()` 里音频=2 / 视频=3，本服务只做图片。
FILE_SCENE_IMAGE = 10
ASSET_FILE_TYPE_IMAGE = 1

#: 上传用的 `Referer`（与建任务一致）。
REFERER = "https://hailuoai.video/zh-Intl/create-image-generation"

#: STS 凭据的本地缓存 TTL（秒）。凭据本身有效期 ~45min，TTL 取 30s 足够覆盖
#: 一次并行 ingest（秒级），又远小于跨小时窗口 ⇒ 不担心 `dir` 漂移。
POLICY_CACHE_TTL = 30.0


@dataclass
class UploadedFile:
    """上传完成后的素材引用 —— 正好是建任务 `fileList[]` 需要的那几个字段。"""

    file_id: str
    url: str
    name: str
    file_type: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    from_cache: bool = False

    def to_video_frame_entry(self, frame_type: int) -> dict[str, Any]:
        """转成**视频** `parameter.fileList[]` 的一项（`frameType` 决定是首帧还是尾帧）。

        🔴 形态来自**真实抓包**（参考实现 `openai_videos.py` 里的 `sora2-i2v`
        请求实录，五键）：

        ```json
        {"id": "469914687591321600", "url": "https://…/xxx.jpeg",
         "name": "cropped_1768892531810.jpeg", "type": "jpeg", "frameType": 0}
        ```

        `frameType`：**0 = 首帧，1 = 尾帧**（`start-end-frames` 模式）。

        ⚠️ 刻意**不用**图片链路那 12 键的全形态（多了 `characterID` /
        `referenceType` / `assetFileType` / `videoID` …）—— 那些是**图片**侧抓包的字段，
        搬到视频端点等于往上发一堆没证据的键；而视频端点出错时回的是与图片同一个
        `code:2 请求异常`，不带到底是哪个键有问题 ⇒ 少发比多发好排障。
        """
        return {
            "id": self.file_id,
            "url": self.url,
            "name": self.name,
            "type": self.file_type,
            "frameType": int(frame_type),
        }

    def to_file_list_entry(self) -> dict[str, Any]:
        """转成上游**图片** `parameter.fileList[]` 的一项。

        字段对照抓包（feed 的 `modelParameter.imageParameter.fileList[0]`）：
        `id` 是 **fileID**（不是本地 id），`frameType: 3`、`referenceType: 0`、
        `assetFileType: 1`、`duration: 0`、`characterID/characterUrl/videoID` 空串。
        这些常量在抓包里是固定形态，缺失会导致上游不接受参考图。
        """
        return {
            "id": self.file_id,
            "name": self.name,
            "type": self.file_type,
            "url": self.url,
            "characterID": "",
            "coverUrl": self.url,
            "frameType": 3,
            "referenceType": 0,
            "characterUrl": "",
            "duration": 0,
            "assetFileType": ASSET_FILE_TYPE_IMAGE,
            "videoID": "",
            "durationMs": 0,
        }


def oss_signature(
    *, access_key_secret: str, method: str, content_md5: str, content_type: str,
    date: str, security_token: str, bucket: str, object_key: str,
) -> str:
    """阿里云 OSS **V1** 签名（`Authorization: OSS {ak}:{sig}`）。

    ```
    StringToSign = VERB + "\\n" + Content-MD5 + "\\n" + Content-Type + "\\n"
                 + Date + "\\n" + CanonicalizedOSSHeaders + CanonicalizedResource
    ```

    ⚠️ `x-oss-security-token` **必须**进 `CanonicalizedOSSHeaders`
    （按 `key:value\\n` 拼接、键全小写、按字典序）。漏了它签名必错，
    而 OSS 的错误文案是笼统的 `SignatureDoesNotMatch` —— 很难反查。
    """
    oss_headers = f"x-oss-security-token:{security_token}\n"
    resource = f"/{bucket}/{object_key}"
    to_sign = "\n".join([method.upper(), content_md5, content_type, date,
                         oss_headers + resource])
    digest = hmac.new(access_key_secret.encode("utf-8"),
                      to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _uuid_filename(ext: str) -> str:
    suffix = f".{ext.lstrip('.')}" if ext else ".bin"
    return f"{uuid.uuid4()}{suffix}"


def _guess_ext(mime: str) -> str:
    return {
        "image/jpeg": "jpeg", "image/jpg": "jpeg", "image/png": "png",
        "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp",
    }.get(mime.lower(), "jpeg")


class Uploader:
    """把本地图片字节送到 hailuo，拿回 `fileID` + `url`。

    持有 `HailuoClient` 的两个能力：签名请求（取 policy / 回调）与 OSS 直传。
    **线程安全**：`upload_bytes` 可被并行调用（并行 ingest）；缓存由锁保护，
    真正的网络上传在锁外 —— 并发的是上传，串行的只是 md5 判定与写回。
    """

    def __init__(
        self,
        *,
        client: Any,
        oss_client: httpx.Client | None = None,
        cache_ttl: float = 6 * 3600.0,
    ) -> None:
        self.client = client
        self.cache_ttl = cache_ttl
        self._oss = oss_client or httpx.Client(timeout=60.0, follow_redirects=True)
        #: `md5 -> (UploadedFile, 到期时间戳)`
        self._cache: dict[str, tuple[UploadedFile, float]] = {}
        self._lock = threading.Lock()
        #: STS 凭据缓存（`(policy, 到期时间戳)`）—— 见 `request_policy` 的说明
        self._policy_cache: tuple[dict[str, Any], float] | None = None
        self._policy_lock = threading.Lock()

    def close(self) -> None:
        self._oss.close()

    # ------------------------------------------------------------------ ①

    def request_policy(self, *, dry_run: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        """① 取 STS 临时凭据。**零计费、免建任务。**

        🔴 **短 TTL 缓存 + 单飞**（2026-09-21 实测驱动，并发 ingest 的关键）：

        实测 8 张图并发 ingest 时，8 个 `request_policy` 同时新建连接会被网络掐
        （1/4 轮出现 `RemoteProtocolError`，**三次重试全失败**）。而凭据是
        **账号级**的 —— `accessKeyId/securityToken/dir/endpoint/bucketName`
        与具体文件无关（callback 里 per-file 的字段是我们按文件填的）
        ⇒ **一次取、多图共用完全等价**，只是把 N 次并发请求压成 1 次。

        `idempotent=True`：这是 GET，连接级失败（冷池并发建连被掐）可安全重试。
        ⚠️ `dry_run` **不读写缓存**（dry-run 的空结果绝不能污染真实路径）。
        """
        if dry_run:
            payload, trace = self.client.request(
                "GET", PATH_REQUEST_POLICY, None, dry_run=True, idempotent=True)
            return {}, trace

        with self._policy_lock:
            cached = self._policy_cache
        if cached and cached[1] > time.time():
            return cached[0], {"stage": "policy_cache_hit",
                               "policy_expiration": str(cached[0].get("expiration") or "")}

        with self._policy_lock:
            #: 双检 —— 等锁期间可能已被别的线程填上（**单飞**：只有第一个线程真取）
            cached = self._policy_cache
            if cached and cached[1] > time.time():
                return cached[0], {"stage": "policy_cache_hit"}
            payload, trace = self.client.request(
                "GET", PATH_REQUEST_POLICY, None, dry_run=False, idempotent=True)
            data = payload.get("data") or {}
            for key in ("accessKeyId", "accessKeySecret", "securityToken",
                        "dir", "endpoint", "bucketName"):
                if not data.get(key):
                    raise UpstreamError(f"上传凭据响应缺少 {key}：{data}")
            self._policy_cache = (data, time.time() + POLICY_CACHE_TTL)
            return data, trace

    # ------------------------------------------------------------------ ②

    def put_to_oss(
        self, *, policy: dict[str, Any], object_key: str, content: bytes, mime: str,
        dry_run: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """② 直传 OSS。返回 `(object_url, trace)`。"""
        endpoint = str(policy["endpoint"])
        bucket = str(policy["bucketName"])
        url = f"https://{bucket}.{endpoint}/{quote(object_key)}"
        content_md5 = base64.b64encode(hashlib.md5(content).digest()).decode("ascii")
        date = format_datetime(datetime.now(timezone.utc), usegmt=True)
        auth = oss_signature(
            access_key_secret=str(policy["accessKeySecret"]), method="PUT",
            content_md5=content_md5, content_type=mime, date=date,
            security_token=str(policy["securityToken"]), bucket=bucket,
            object_key=object_key,
        )
        trace: dict[str, Any] = {
            "oss_url": url, "oss_object_key": object_key,
            "content_md5": content_md5, "bytes": len(content), "dry_run": dry_run,
        }
        if dry_run:
            trace["sent"] = False
            return url, trace

        #: 🔴 OSS PUT **幂等**（同 object_key + 同内容 ⇒ 覆盖写，不产生副作用）
        #: ⇒ 连接级失败允许重试。实测（2026-09-21）：冷池下并发建连会被掐，
        #: 重试一次即可越过该窗口。
        last_err: Exception | None = None
        resp: httpx.Response | None = None
        for attempt in range(1, 4):
            try:
                resp = self._oss.put(url, content=content, headers={
                    "Content-Type": mime,
                    "Content-MD5": content_md5,
                    "Date": date,
                    "x-oss-security-token": str(policy["securityToken"]),
                    "Authorization": f"OSS {policy['accessKeyId']}:{auth}",
                })
                break
            except httpx.HTTPError as e:
                last_err = e
                trace["oss_retry_attempts"] = attempt
                if attempt >= 3:
                    raise UpstreamError(
                        f"OSS 上传连接失败（已重试 3 次）："
                        f"{type(e).__name__}: {e}") from e
                time.sleep(0.2 * attempt)
        if resp is None:  # 循环要么 break 要么 raise
            raise UpstreamError(f"OSS 上传失败：{last_err}")
        trace["oss_status"] = resp.status_code
        if resp.status_code >= 300:
            raise UpstreamError(
                f"OSS 上传失败（HTTP {resp.status_code}）：{resp.text[:300]}")
        return url, trace

    # ------------------------------------------------------------------ ③

    def policy_callback(
        self, *, policy: dict[str, Any], file_name: str, origin_name: str,
        size: int, mime: str, file_md5: str, dry_run: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """③ 通知 hailuo"我传好了"。返回 `(data, trace)`。

        ⚠️ `assetFileType` 由这里恒为图片（1）—— 与抓包一致。
        `durationMs: 0` 是图片的实值（视频才有意义）。
        """
        body = {
            "fileName": file_name,
            "originFileName": origin_name,
            "dir": str(policy["dir"]),
            "endpoint": str(policy["endpoint"]),
            "bucketName": str(policy["bucketName"]),
            "size": str(size),
            "mimeType": mime.split("/")[-1],
            "fileMd5": file_md5,
            "fileScene": FILE_SCENE_IMAGE,
            "durationMs": 0,
            "assetFileType": ASSET_FILE_TYPE_IMAGE,
        }
        payload, trace = self.client.request(
            "POST", PATH_POLICY_CALLBACK, body, dry_run=dry_run)
        if dry_run:
            return {}, trace
        return payload.get("data") or {}, trace

    # ------------------------------------------------------------------ 编排

    def upload_bytes(
        self, *, content: bytes, mime: str, name: str, dry_run: bool = False,
    ) -> tuple[UploadedFile, list[dict[str, Any]]]:
        """完整三段式。返回 `(UploadedFile, [上游明细, …])`。

        缓存命中 ⇒ **零网络往返**（连 ① 都不打）。
        可并发调用：锁只包缓存判定与写回，上传本身在锁外。
        """
        file_md5 = hashlib.md5(content).hexdigest()
        with self._lock:
            cached = self._cache.get(file_md5)
        if cached and cached[1] > time.time():
            logger.debug(f"上传缓存命中 md5={file_md5} file_id={cached[0].file_id}")
            hit = cached[0]
            return (UploadedFile(**{**hit.__dict__, "from_cache": True}),
                    [{"stage": "upload_cache_hit", "file_md5": file_md5}])

        ext = _guess_ext(mime)
        #: 抓包形态：`fileName` 是 UUID 名，`originFileName` 是用户原始文件名。
        upstream_name = _uuid_filename(ext)

        policy, t1 = self.request_policy(dry_run=dry_run)
        traces = [{"stage": "request_policy", **t1}]

        if dry_run:
            object_key = f"<dir>/{upstream_name}"
            t2: dict[str, Any] = {"oss_object_key": object_key, "sent": False,
                                  "dry_run": True, "bytes": len(content)}
            t3: dict[str, Any] = {"sent": False, "dry_run": True}
            data: dict[str, Any] = {}
        else:
            object_key = f"{policy['dir']}/{upstream_name}"
            _, t2 = self.put_to_oss(policy=policy, object_key=object_key,
                                    content=content, mime=mime, dry_run=False)
            data, t3 = self.policy_callback(
                policy=policy, file_name=upstream_name, origin_name=name,
                size=len(content), mime=mime, file_md5=file_md5, dry_run=False)
        traces += [{"stage": "oss_put", **t2}, {"stage": "policy_callback", **t3}]

        # 回调返回的 fileID：抓包实测字段名是 `fileID`（feed 的 fileList 用的是同值）
        file_id = str(data.get("fileID") or data.get("id") or "")
        url = str(data.get("url") or "")
        if dry_run:
            file_id, url = "<dry-run-file-id>", "<dry-run-url>"
        elif not file_id:
            raise UpstreamError(
                f"上传回调成功但没拿到 fileID：{data} —— 没有它就无法垫图。")

        uploaded = UploadedFile(file_id=file_id, url=url or object_key,
                               name=name, file_type=ext, size=len(content))
        if not dry_run:
            with self._lock:
                self._cache[file_md5] = (uploaded, time.time() + self.cache_ttl)
            logger.bind(file_id=file_id, file_md5=file_md5).info(
                f"输入图已上传：{name} ({len(content)}B) -> fileID={file_id}")
        return uploaded, traces

    # ------------------------------------------------------------------ 维护

    def cache_stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            alive = sum(1 for _, exp in self._cache.values() if exp > now)
            entries = len(self._cache)
        return {"entries": entries, "alive": alive, "ttl_seconds": self.cache_ttl}

    def purge_expired(self) -> int:
        with self._lock:
            now = time.time()
            dead = [k for k, (_, exp) in self._cache.items() if exp <= now]
            for k in dead:
                self._cache.pop(k, None)
        return len(dead)


__all__ = [
    "ASSET_FILE_TYPE_IMAGE",
    "FILE_SCENE_IMAGE",
    "PATH_POLICY_CALLBACK",
    "PATH_REQUEST_POLICY",
    "UploadedFile",
    "Uploader",
    "oss_signature",
]
