# -*- coding: utf-8 -*-
"""收藏条目内置变量（占位符）展开。

在收藏（文件夹/网址/命令/账号）的名称与值中写入 ``{y}{m}{d}`` 之类的
占位符，显示、执行、复制时实时展开为当前值；磁盘与云端仍保存原始模板
文本，因此旧数据与云同步格式完全不受影响。

支持语法：
  ``{name}``          变量名（英文或中文，见下表）
  ``{date:%Y%m%d}``   日期/时间类变量可跟 ``:`` 加 strftime 格式
  ``{{`` / ``}}``     转义为字面花括号
未知变量原样保留（不破坏路径/命令行语法）。

变量一览：
  日期      {y} {m} {d}            2026 / 09 / 09   （{yy}=26）
            {year} {month} {day}   同 {y} {m} {d}
            {年} {月} {日}          同 {y} {m} {d}
            {date}                 2026-09-09（可 {date:%Y%m%d}）
  星期      {wd} {weekday} {星期}  周三 / 星期三
            {wenum}                Wednesday
  时间      {hh} {mi} {ss}         13 / 05 / 09
            {hour} {minute} {second} 同 {hh} {mi} {ss}
            {时} {分} {秒}          同 {hh} {mi} {ss}
            {time}                 13:05:09（可 {time:%H%M}）
            {datetime} {now}       2026-09-09 13:05:09
  计算机    {pc} {computer} {host} Wuzu-Client
            {电脑名} {主机名}       同 {pc}
            {os} {系统名}           Windows
  用户      {user} {username} {用户名}
            {home}                  用户主目录

示例：收藏名写成 ``冯青青 {y}{m}{d}：测试{pc}`` → 显示/执行为
``冯青青 20260909：测试Wuzu-Client``。
"""

from __future__ import annotations

import functools
import getpass
import os
import platform
import re
from datetime import datetime

__all__ = ["expand", "has_vars", "preview_text", "VARIABLE_HELP"]

_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([^{}]{1,48})\}")

_WEEKDAYS_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


@functools.lru_cache(maxsize=1)
def _computer_name() -> str:
    return (
        os.environ.get("COMPUTERNAME")
        or os.environ.get("HOSTNAME")
        or platform.node()
        or "PC"
    )


@functools.lru_cache(maxsize=1)
def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - getpass 在个别环境会抛异常
        return os.environ.get("USERNAME") or os.environ.get("USER") or "user"


def _strftime(default_fmt: str):
    def fn(now: datetime, fmt: str | None) -> str:
        return now.strftime(fmt or default_fmt)

    return fn


def _weekday_cn(now: datetime, fmt: str | None) -> str:
    return _WEEKDAYS_CN[now.weekday()]


def _weekday_short(now: datetime, fmt: str | None) -> str:
    return "周" + _WEEKDAYS_CN[now.weekday()][2:]


def _weekday_en(now: datetime, fmt: str | None) -> str:
    return now.strftime("%A")


def _computer(now: datetime, fmt: str | None) -> str:
    return _computer_name()


def _os_name(now: datetime, fmt: str | None) -> str:
    return platform.system()


def _user(now: datetime, fmt: str | None) -> str:
    return _os_user()


def _home(now: datetime, fmt: str | None) -> str:
    return str(os.path.expanduser("~"))


#: 变量名 → 取值函数（名字统一按小写查表，中文名不受 lower 影响）
VARIANTS = {
    # 日期
    "y": _strftime("%Y"), "yy": _strftime("%y"),
    "m": _strftime("%m"), "d": _strftime("%d"),
    "year": _strftime("%Y"), "month": _strftime("%m"), "day": _strftime("%d"),
    "年": _strftime("%Y"), "月": _strftime("%m"), "日": _strftime("%d"),
    "date": _strftime("%Y-%m-%d"),
    # 星期
    "wd": _weekday_short, "weekday": _weekday_cn, "星期": _weekday_cn,
    "wenum": _weekday_en,
    # 时间
    "hh": _strftime("%H"), "mi": _strftime("%M"), "ss": _strftime("%S"),
    "hour": _strftime("%H"), "minute": _strftime("%M"), "second": _strftime("%S"),
    "时": _strftime("%H"), "分": _strftime("%M"), "秒": _strftime("%S"),
    "time": _strftime("%H:%M:%S"),
    "datetime": _strftime("%Y-%m-%d %H:%M:%S"), "now": _strftime("%Y-%m-%d %H:%M:%S"),
    # 计算机 / 系统
    "pc": _computer, "computer": _computer, "host": _computer,
    "hostname": _computer, "电脑名": _computer, "主机名": _computer,
    "os": _os_name, "系统名": _os_name,
    # 用户
    "user": _user, "username": _user, "用户名": _user, "home": _home,
}

VARIABLE_HELP = (
    "内置变量：{y}{m}{d} 年月日 ｜ {wd}/{星期} ｜ {hh}{mi}{ss} 时分秒 ｜ "
    "{date} {time} {datetime}（可 {date:%Y%m%d}）｜ {pc} 电脑名 ｜ "
    "{os} 系统名 ｜ {user} 用户名 ｜ {{ }} 转义花括号"
)


def expand(text: str) -> str:
    """把 *text* 中的 ``{变量}`` 替换为当前值；未知变量与转义按原样处理。"""
    if not text or "{" not in text:
        return text
    now = datetime.now()

    def _sub(m: re.Match) -> str:
        token = m.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name, _, fmt = m.group(1).partition(":")
        fn = VARIANTS.get(name.strip().lower())
        if fn is None:
            return token
        try:
            return fn(now, fmt.strip() or None)
        except Exception:  # noqa: BLE001 - 坏格式串不应影响使用
            return token

    return _TOKEN_RE.sub(_sub, text)


def has_vars(text: str) -> bool:
    """*text* 中是否含可识别的变量占位符。"""
    return bool(text) and "{" in text and expand(text) != text


def preview_text(pairs: list[tuple[str, str]]) -> str:
    """生成编辑对话框里的实时预览文案。

    *pairs* 为 ``(标签, 原始模板)`` 列表；全部为空时返回变量用法提示。
    """
    parts = []
    for label, raw in pairs:
        raw = (raw or "").strip()
        if raw:
            parts.append(f"{label}: {expand(raw)}")
    if not parts:
        return f"变量预览：{VARIABLE_HELP}"
    return "实时预览 →  " + "　".join(parts)
