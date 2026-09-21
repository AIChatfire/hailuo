#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台协调器 —— **推进任务的唯一执行者**。

## 为什么它不是"可选增强"

受理端刻意不碰上游（建任务是计费动作）。⇒ **没有协调器，任务永远停在 `queued`**
（连建任务都不会发生），超时看门狗也永不触发。它是链路的一环，不是优化。

```
每 tick：
  ① 续租（拿不到租约 ⇒ 静默空转，让持锁副本干活）
  ② 看门狗：把超过 TASK_TIMEOUT 的任务收成 failure
  ③ **一次** 上游 batch 查询推进全部在途任务（批量轮询）
  ④ 在并发与闸门允许的前提下，从 queued 里取任务建到上游
```

## 两个刻意的设计

1. **只打一次上游查询**。hailuo 的 `my/batch` 不带 id 参数（回"我最近的若干条"）
   ⇒ 一轮 tick 的上游查询次数与在途任务数**无关**。这是"把并发提上去"的前提：
   否则提并发等于把上游请求量一起乘 N，而那正是风控最敏感的维度。
2. **租约选主**。多 worker 下只有一个副本真正执行；崩溃的副本租约到期后自动被抢占
   —— 所以不会出现"主挂了任务就永远不动"。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .config import Settings
from .errors import AdapterError, RiskControlChallenge
from .observability import OBS
from .service import Service


@dataclass
class CoordinatorStats:
    ticks: int = 0
    submitted: int = 0
    submit_skipped: int = 0
    submit_failed: int = 0
    polled_batches: int = 0
    advanced: int = 0
    expired: int = 0
    leader_skips: int = 0
    last_tick_at: float = 0.0
    last_error: str = ""
    errors: int = 0


class Coordinator:
    """后台线程。**`start()`/`stop()` 由 lifespan 调用。**"""

    def __init__(self, service: Service, settings: Settings) -> None:
        self.service = service
        self.settings = settings
        self.owner = f"coord-{uuid.uuid4().hex[:12]}"
        self.stats = CoordinatorStats()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: 受理路径用它叫醒协调器（纯优化：唤醒丢了只是慢一个 tick，
        #: "该派发谁"始终由库里的状态决定）
        self._wake = threading.Event()

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if not self.settings.coordinator_enabled:
            logger.warning("协调器已按配置关闭（COORDINATOR_ENABLED=0）—— "
                           "任务只会停在 queued，永远不会建到上游。")
            return
        if self._thread is not None:
            return
        #: 租约用 PostgreSQL/SQLite 表 ⇒ 多副本安全；单副本时首轮即拿到
        self.service.store.acquire_lease(owner=self.owner,
                                        ttl=self.settings.coordinator_lease)
        self._thread = threading.Thread(target=self._run, name="coordinator", daemon=True)
        self._thread.start()
        logger.info(f"协调器已启动（owner={self.owner}, tick={self.settings.coordinator_tick}s）")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        try:
            self.service.store.release_lease(self.owner)
        except Exception as e:  # noqa: BLE001
            # 释放租约失败 ⇒ 最坏情况是等租约自然过期（默认 30s）后被别的副本抢占。
            # 不影响正确性，但要让它在日志里可见 —— 否则"主选举频繁抖动"无从查起。
            logger.debug(f"释放协调器租约失败（将由租约过期兜底）：{type(e).__name__}: {e}")
        logger.info("协调器已停止")

    def wake(self) -> None:
        """受理后叫醒：不然这条任务要等到下一个 tick 才被发现。"""
        self._wake.set()

    # ------------------------------------------------------------------ 主循环

    def _run(self) -> None:
        tick = max(0.1, self.settings.coordinator_tick)
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.stats.errors += 1
                self.stats.last_error = f"{type(e).__name__}: {e}"
                logger.exception(f"协调器 tick 异常（已吞，循环继续）：{e}")
            elapsed = time.perf_counter() - started
            self._wake.wait(timeout=max(0.0, tick - elapsed))
            self._wake.clear()

    # ------------------------------------------------------------------ 单轮

    def tick(self) -> CoordinatorStats:
        """一轮。

        ⚠️ 测试里**直接调它**（不起线程）：`tests/test_coordinator.py` 靠这个
        做到"确定性推进"，而不是 sleep 等线程。
        """
        self.stats.ticks += 1
        self.stats.last_tick_at = time.time()

        # ① 续租 —— 拿不到就只做只读的事（不建任务）
        is_leader = self.service.store.acquire_lease(
            owner=self.owner, ttl=self.settings.coordinator_lease)
        if not is_leader:
            self.stats.leader_skips += 1
            return self.stats

        # ② 看门狗
        expired = self.service.store.expire_stale(timeout=self.settings.task_timeout)
        if expired:
            self.stats.expired += len(expired)
            logger.warning(f"看门狗收掉 {len(expired)} 个超时任务：{expired}")

        # ③ 批量轮询（**一次上游查询**）
        in_flight = self.service.store.in_flight()
        if in_flight:
            try:
                res = self.service.poll_many(in_flight)
                self.stats.polled_batches += 1
                self.stats.advanced += int(res.get("updated") or 0)
            except RiskControlChallenge as e:
                self.service.gate.enter_cooldown(str(e))
                logger.warning(f"轮询命中风控，已进入冷却：{e}")
            except AdapterError as e:
                self.stats.last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"轮询失败（不影响下一轮）：{e}")

        # ④ 建任务（受并发 + 闸门约束）
        self._submit_due()

        # ⑤ 顺手清过期上传缓存（每 600 tick ≈ 10 分钟一次）
        if self.stats.ticks % 600 == 0:
            purged = self.service.purge_upload_cache()
            if purged:
                logger.debug(f"清理 {purged} 条过期上传缓存")

        # ⑥ 顺手清理超出保留期的终态任务（`TASK_RETENTION_DAYS`）
        # 不做的话任务库会一直长 —— 而"库里有多少任务"直接影响 count_active 的查询成本。
        if self.stats.ticks % 6000 == 0:
            removed = self.service.store.purge_old(
                retention_days=self.settings.task_retention_days)
            if removed:
                logger.info(f"按保留期清理 {removed} 条历史任务")

        OBS.event("coordinator.tick", tick=self.stats.ticks,
                  submitted=self.stats.submitted, advanced=self.stats.advanced,
                  in_flight=len(in_flight))
        return self.stats

    def _submit_due(self) -> None:
        slots = self.settings.hl_concurrency - self.service.store.count_active()
        if slots <= 0:
            return
        for task in self.service.store.due_for_submit(limit=slots):
            decision = self.service.gate.check()
            if not decision.allowed:
                self.stats.submit_skipped += 1
                logger.debug(f"闸门未放行，本轮停止建任务：{decision.reason}")
                return
            try:
                res = self.service.submit(task["task_id"])
            except RiskControlChallenge as e:
                self.service.gate.enter_cooldown(str(e))
                self.stats.submit_failed += 1
                logger.warning(f"建任务命中风控，整体冷却：{e}")
                return
            except AdapterError as e:
                self.stats.submit_failed += 1
                self.stats.last_error = f"{type(e).__name__}: {e}"
                # 🔴 **建任务之前的失败**（输入图下载/上传、连接被掐）：
                # 上游**一定没有**建任务 ⇒ 重试不会重复计费。任务保持 `queued`，
                # 下一 tick 自然会被 `due_for_submit` 捞出重试（attempts 已由
                # `service.submit` 自增，到上限就判失败——不能让任务无限重试）。
                # 其余失败（已发出建任务请求等）**绝不重试**：无法排除上游已受理。
                attempts_done = int(task.get("attempts") or 0) + 1
                if (getattr(e, "pre_create", False)
                        and attempts_done < self.settings.submit_max_attempts):
                    logger.warning(
                        f"建任务在**建任务前**失败（未计费），"
                        f"第 {attempts_done}/{self.settings.submit_max_attempts} 次，"
                        f"下一 tick 重试：{e}")
                    continue
                self.service.store.update_task(
                    task["task_id"], status="failure", finished_at=time.time(),
                    error={"message": str(e), "type": e.error_type, "code": e.code})
                logger.warning(f"建任务失败（已判任务失败）：{e}")
                continue
            if res.get("submitted"):
                self.stats.submitted += 1
            else:
                self.stats.submit_skipped += 1

    # ------------------------------------------------------------------ 观测

    def stats_view(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.coordinator_enabled,
            "owner": self.owner,
            "running": bool(self._thread and self._thread.is_alive()),
            "ticks": self.stats.ticks,
            "submitted": self.stats.submitted,
            "submit_skipped": self.stats.submit_skipped,
            "submit_failed": self.stats.submit_failed,
            "polled_rounds": self.stats.polled_batches,
            "advanced": self.stats.advanced,
            "expired": self.stats.expired,
            "leader_skips": self.stats.leader_skips,
            "errors": self.stats.errors,
            "last_error": self.stats.last_error,
            "last_tick_at": self.stats.last_tick_at,
        }


def one_shot(service: Service, settings: Settings, *, dry_run: bool = False) -> dict[str, Any]:
    """跑**恰好一轮**然后返回 —— 给脚本/自检用，不起线程。

    `dry_run=True` 时**一个字节都不发上游**（建任务路径完全走签名与构造）。
    """
    coord = Coordinator(service, settings)
    if dry_run:
        queued = service.store.due_for_submit(limit=settings.hl_concurrency)
        out: list[dict[str, Any]] = []
        for task in queued:
            try:
                out.append(service.submit(task["task_id"], dry_run=True))
            except AdapterError as e:
                out.append({"task_id": task["task_id"], "error": str(e)})
        return {"dry_run": True, "would_submit": len(queued), "details": out}
    coord.tick()
    return {"dry_run": False, "stats": coord.stats_view()}


__all__ = ["Coordinator", "CoordinatorStats", "one_shot"]
