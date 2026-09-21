#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可观测性 —— logfire(OTel) + loguru 的**唯一收拢点**。

## 一条纪律 + 一条反纪律

**纪律：密钥不上报，靠实现约束而不是过滤器。**

1. `token` 只在 `HailuoClient` 里，**从不作为属性传入**；
2. 上游埋点的 `http_path` **只给 pathname、丢掉整个 query**
   （`client.request` 已经这么做）；
3. 上游明细上报的是**业务报文**，天然不含凭据。

由 `tests/test_observability.py` 守着：断言上报事件里**没有 token**、
且 `http_path` 不含 `?`。

**反纪律（刻意）：不做事后脱敏。** `scrubbing=False` 必须保持 ——
SDK 自带 scrubber 按**值子串**命中 `credential`/`token`/`auth`，
而 hailuo 的产物是 OSS 直链（可能带签名参数）⇒ 打开它会把每一条结果 URL
打成 `[Scrubbed due to 'Credential']`，面板直接不可读。
**事后"顺手脱敏"会改掉上游的实际字段名**，让人对着面板排查一个不存在的字段。

## 探活路径

`/healthz` 每 30s 被容器打一次。它**既不上报 span、也不留日志** ——
两条通道都从 `PROBE_PATHS` 一张表派生。只摘 span 会留下一半噪音。
"""
from __future__ import annotations

import sys
from typing import Any

from loguru import logger

#: 探活路径。**单一真相** —— span 与日志两条通道都从这里派生。
PROBE_PATHS: tuple[str, ...] = ("/healthz", "/readyz")


def is_probe_path(path: str) -> bool:
    return path in PROBE_PATHS


def should_log_path(path: str) -> bool:
    """请求日志开关。默认全打，只有探活静音。"""
    return not is_probe_path(path)


def excluded_urls(paths: tuple[str, ...]) -> str:
    """把路径表机械地变成 logfire 的 `excluded_urls` 正则。

    🔴 **别手写这个正则**：logfire 用的是 `re.search`（**子串匹配**），
    写 `"/"` 会命中每一个 URL ⇒ **全站追踪静默关闭**，而且没有任何报错。
    所以这里把每个路径 `re.escape` 后精确锚定。
    """
    import re  # noqa: PLC0415

    return "|".join(rf"^{re.escape(p)}$" for p in paths)


def setup_logging(level: str = "INFO", *, obs: "Observability | None" = None) -> None:
    """配置 loguru 输出到 stderr（容器友好）。"""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <7}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> "
            "- <level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )
    if obs is not None and obs.sdk_configured:
        logger.debug("loguru 已接入 logfire（logfire.loguru 由 OBS.init 挂载）")


class Observability:
    """logfire 的薄封装。**失败绝不影响服务启动。**"""

    def __init__(self) -> None:
        self.sdk_configured = False
        self.write_token: str = ""
        self.service_name: str = ""
        self.environment: str = ""
        self.capture_upstream: bool = True
        self._logfire: Any = None
        self._events: int = 0

    # ------------------------------------------------------------------ 装配

    def init(self, settings: Any) -> "Observability":
        """尝试配置 logfire。没有 token ⇒ `sdk_configured=False`（只在本地留 span）。"""
        self.write_token = settings.otel_token
        self.service_name = settings.otel_service_name
        self.environment = settings.otel_environment
        self.capture_upstream = settings.otel_capture_upstream

        if not self.write_token:
            print("[observability] 未配置 LOGFIRE_TOKEN ⇒ 只在本地留日志，不上报 span。")
            return self

        try:
            import logfire  # noqa: PLC0415

            logfire.configure(
                token=self.write_token,
                service_name=self.service_name,
                environment=self.environment or None,
                #: 🔴 保持 False：见模块 docstring 的"反纪律"一节。
                scrubbing=False if not settings.otel_scrubbing else True,
            )
            try:
                from logfire import loguru as logfire_loguru  # noqa: PLC0415

                logfire_loguru.LoguruIntegration().install()
            except Exception:  # noqa: BLE001, S110
                # loguru 集成失败不影响主通道（span 照发），刻意静默。
                pass
            self._logfire = logfire
            self.sdk_configured = True
        except Exception as e:  # noqa: BLE001
            print(f"[observability] logfire 配置失败，已忽略：{type(e).__name__}: {e}")
            self.sdk_configured = False
        return self

    def flush(self) -> None:
        """**短命进程必须显式 flush**，否则退出时最后一批 span 直接丢。"""
        if self._logfire is None:
            return
        try:
            self._logfire.force_flush()
        except Exception:  # noqa: BLE001, S110
            # flush 失败无处可报（报它也要走同一条通道）⇒ 刻意的静默。
            # 代价已经由 `OBS.sdk_configured` 与 `/stats` 的 events 计数兜住。
            pass

    # ------------------------------------------------------------------ 上报

    def event(self, name: str, **attrs: Any) -> None:
        """记一条事件。

        ⚠️ **调用方负责不把凭据放进 `attrs`**。本方法不做任何过滤/改写 ——
        这是刻意的：过滤器会静默吞掉你以为已经上报了的信息。
        """
        self._events += 1
        if self._logfire is None:
            return
        try:
            self._logfire.info(name, **attrs)
        except Exception:  # noqa: BLE001, S110
            # 🔴 上报失败**绝不能**让业务请求失败 —— 观测是横切关注点，
            # 它坏了不该带走功能。也不能在这里 log（会与自身递归）。
            pass

    def upstream(self, event: str, **attrs: Any) -> None:
        """上游埋点。`capture_upstream=False` 时只记阶段名与状态码。

        ⚠️ 第一个形参叫 `event` 而不是 `stage`：调用方常写
        `OBS.upstream("upload.x", **trace)`，而 `trace` 里本身就有 `stage` 键
        —— 形参同名会直接 `TypeError: got multiple values for argument`。
        """
        if not self.capture_upstream:
            keep = {k: v for k, v in attrs.items()
                    if k in ("http_method", "http_path", "http_status", "dry_run")}
            attrs = keep
        self.event(f"upstream.{event}", **attrs)

    # ------------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        return {
            "sdk_configured": self.sdk_configured,
            "service_name": self.service_name,
            "environment": self.environment,
            "capture_upstream": self.capture_upstream,
            "events": self._events,
            "probe_paths": list(PROBE_PATHS),
        }


#: 进程级单例 —— 与 jimeng 同一形态（service/coordinator/client 都拿这一个）。
OBS = Observability()


__all__ = [
    "OBS",
    "PROBE_PATHS",
    "Observability",
    "excluded_urls",
    "is_probe_path",
    "setup_logging",
    "should_log_path",
]
