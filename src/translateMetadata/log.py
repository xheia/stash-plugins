# -*- coding: utf-8 -*-
"""向 Stash 回传日志与进度。

Stash 读取插件 stderr 的约定：每行以 SOH(0x01) + 级别字符 + STX(0x02) 开头。
级别字符：t=trace, d=debug, i=info, w=warning, e=error, p=progress。
没有前缀的行，Stash 会按插件 yml 里 errLog 指定的级别整体处理。

进度只在作为「任务」运行时有意义：任务的 stderr 会被接到进度通道上，
而 Hook 触发时没有进度通道，所以默认关闭，避免写坏 stderr。
"""
from __future__ import annotations

import sys

_SOH = "\x01"
_STX = "\x02"

_progress_enabled = False


def enable_progress(enabled: bool = True) -> None:
    """只有任务模式才开启进度上报。"""
    global _progress_enabled
    _progress_enabled = bool(enabled)


def _emit(level_char: str, message: str) -> None:
    try:
        sys.stderr.write("%s%s%s%s\n" % (_SOH, level_char, _STX, message))
        sys.stderr.flush()
    except Exception:
        # 日志本身绝不能让插件崩掉
        pass


def trace(message) -> None:
    _emit("t", str(message))


def debug(message) -> None:
    _emit("d", str(message))


def info(message) -> None:
    _emit("i", str(message))


def warning(message) -> None:
    _emit("w", str(message))


def error(message) -> None:
    _emit("e", str(message))


def progress(value) -> None:
    """上报 0.0 ~ 1.0 的进度，Stash 任务页会显示进度条。"""
    if not _progress_enabled:
        return
    try:
        v = float(value)
    except (TypeError, ValueError):
        return
    v = min(max(v, 0.0), 1.0)
    _emit("p", repr(v))


def progress_step(done: int, total: int) -> None:
    """按已完成 / 总数上报进度。"""
    if total and total > 0:
        progress(float(done) / float(total))
