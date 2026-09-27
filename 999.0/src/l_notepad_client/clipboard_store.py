# -*- coding: utf-8 -*-
"""剪贴板历史的单例存储。

全应用唯一的历史模型 + 唯一写盘线程：

- 捕获引擎在捕获线程调用 ``submit_item()`` 提交条目，经事件队列回主线程入模型；
- 去重语义为「同键置顶并刷新时间」，重复复制同一内容不再被丢弃；
- 图片落盘、缩略图、JSON 序列化全部在写盘线程完成，GUI 线程不做磁盘 IO；
- 落盘用临时文件 + ``os.replace`` 原子替换，``flush()`` 供退出前等待落盘完成。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from PySide6 import QtCore, QtGui

from . import paths
from pytracemp import lprint

# 剪贴板历史最多保存条数（超出丢弃最旧的）
CLIPBOARD_MAX_STORED = 2000

# 剪贴板图片子目录（PNG 原图落盘，历史条目只存引用，避免 JSON 膨胀）
CLIPBOARD_IMAGES_DIR_NAME = "clipboard_images"

# 图片保存前的最长边上限（超过则等比缩放，防止超大截图撑爆磁盘）
CLIPBOARD_IMAGE_MAX_SIDE = 4096

# 缩略图边长（渲染缩略图时避免读 4096px 大图）
THUMB_SIZE = 46

# 写盘防抖：变更后合并到一次写入，避免频繁同步写盘
SAVE_DEBOUNCE_SEC = 0.8

# 诊断计数日志的节流间隔（每 N 次打印一次）
_LOG_EVERY = 20

_HISTORY_FILENAME = "clipboard_history.json"


def now_iso() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def images_dir() -> Path:
    """剪贴板图片缓存目录（PNG 原图 + ``{md5}_t.png`` 缩略图）。"""
    return paths.favorites_dir() / CLIPBOARD_IMAGES_DIR_NAME


def history_file() -> Path:
    """剪贴板历史 JSON 文件。"""
    return paths.favorites_dir() / _HISTORY_FILENAME


def dedupe_key(item: dict) -> tuple:
    """剪贴板条目去重键（按 kind 区分）。"""
    kind = item.get("kind", "text") if isinstance(item, dict) else "text"
    if kind == "image":
        return ("image", str(item.get("md5", "") or ""))
    if kind == "file":
        files = sorted(str(f) for f in (item.get("files", []) or []))
        return ("file", "|".join(files))
    return ("text", str(item.get("text", "") or ""))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """临时文件 + 原子替换写字节串（中断时磁盘上仍是上一份完整内容）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def cleanup_stale_temp_files() -> int:
    """清理上次异常退出残留的 ``*.tmp``（启动时调用）。"""
    removed = 0
    try:
        for f in paths.favorites_dir().iterdir():
            if f.is_file() and f.name.endswith(".tmp"):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def history_lock_path() -> Path:
    """跨进程写盘互斥锁文件（与历史文件同目录）。"""
    p = history_file()
    return p.with_name(p.name + ".lock")


def _file_sig(path: Path) -> tuple | None:
    """文件身份指纹 (mtime_ns, size)；不存在返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


@contextmanager
def _cross_process_lock(timeout: float = 5.0) -> Iterator[bool]:
    """拿一把跨进程写盘锁（Windows: ``msvcrt`` 对锁文件加字节锁）。

    把"重读磁盘 → 合并 → 原子替换"变成原子操作，避免两个实例读到半截再互相覆盖。
    拿不到锁（超时 / 非 Windows）也照常放行并返回 False：此时退化成"尽力而为"，
    调用方仍会重读磁盘合并，只是不再有原子性——不能因此把历史写不出去。
    """
    fd = -1
    path = history_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    except OSError:
        yield False
        return
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
        yield locked
    finally:
        if locked:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def merge_items(disk_items: list[dict], mem_items: list[dict],
                deleted: frozenset | set = frozenset(),
                max_stored: int = CLIPBOARD_MAX_STORED) -> list[dict]:
    """合并"磁盘上已有的历史"与"本进程内存里的历史"。

    - 内存条目**原样保留**：旧历史文件里可能存在同 key 的重复条目（加载刻意不去重），
      这里不能替它去重
    - 只把"内存里没有的 key"从磁盘补进来（磁盘是另一个实例写的），跳过本次已删除的键
    - 结果按「收藏在前、组内时间倒序」规范排序，并按 ``max_stored`` 只裁最旧的未收藏项

    多实例并存时谁先退出都不会把对方的数据覆盖掉。
    """
    merged = list(mem_items)
    seen = {dedupe_key(it) for it in merged}
    for it in disk_items:
        key = dedupe_key(it)
        if key in seen or key in deleted:
            continue
        seen.add(key)
        merged.append(it)
    merged.sort(
        key=lambda it: (bool(it.get("favorite")), str(it.get("time", ""))),
        reverse=True,
    )
    # 裁剪口径与 _trim 一致：只丢最旧的未收藏项，收藏项无条件保留
    i = len(merged) - 1
    while len(merged) > max_stored and i >= 0:
        if not merged[i].get("favorite"):
            merged.pop(i)
        i -= 1
    return merged


class ClipboardHistoryModel(QtCore.QAbstractListModel):
    """剪贴板历史数据模型（配合 QListView 虚拟化渲染）。

    QListView 只渲染可见行，因此无论历史多大都不会卡顿。去重用 set 维护，查找 O(1)。

    支持三种条目（kind）：
    - ``text``：纯文本 ``{kind, text, time}``
    - ``image``：图片 ``{kind, text, time, md5, image_path, width, height}``，
      PNG 原图落盘到 ``favorites_dir/clipboard_images/{md5}.png``，历史只存引用
    - ``file``：复制文件/文件夹 ``{kind, text, time, files, count}``
    """

    TextRole = QtCore.Qt.UserRole            # 主文本（text / 文件展示名 / "[图片]"）
    KindRole = QtCore.Qt.UserRole + 1        # kind: text / image / file
    ImagePathRole = QtCore.Qt.UserRole + 2   # image: PNG 绝对路径
    FilesRole = QtCore.Qt.UserRole + 3       # file: 文件路径列表
    TimeRole = QtCore.Qt.UserRole + 4        # 记录时间
    SearchRole = QtCore.Qt.UserRole + 5      # 搜索过滤用的文本
    ItemRole = QtCore.Qt.UserRole + 6        # 整条 dict（右键/双击用）
    FavoriteRole = QtCore.Qt.UserRole + 7    # 是否收藏（★ 置顶显示且免于清理）

    def __init__(self, items: list[dict] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._items: list[dict] = list(items) if items else []
        self._key_set: set[tuple] = {dedupe_key(it) for it in self._items}

    @staticmethod
    def _dedupe_key(item: dict) -> tuple:
        return dedupe_key(item)

    # ---- Qt model 接口 ----
    def items(self) -> list[dict]:
        """条目列表（直接引用，供纯 Python 快速统计，避免逐行走 Qt 接口）。"""
        return self._items

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index: QtCore.QModelIndex, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        item = self._items[index.row()]
        kind = item.get("kind", "text")
        text = str(item.get("text", "") or "")
        time_str = str(item.get("time", "") or "")
        if role == QtCore.Qt.DisplayRole:
            if kind == "image":
                w = int(item.get("width", 0) or 0)
                h = int(item.get("height", 0) or 0)
                t = text if (text and text != "[图片]") else ""
                if len(t) > 24:
                    t = t[:24] + "…"
                if t:
                    return f"[{time_str}] [图片] {w}×{h} {t}"
                return f"[{time_str}] [图片] {w}×{h}"
            if kind == "file":
                files = item.get("files", []) or []
                if len(files) == 1:
                    return f"[{time_str}] 📄 {os.path.basename(files[0])}"
                return f"[{time_str}] 📁 {len(files)} 个文件"
            return f"[{time_str}] {text}"
        if role == QtCore.Qt.ToolTipRole:
            if kind == "image":
                p = item.get("image_path", "")
                if text and text != "[图片]":
                    return f"[图片] {time_str}\n{p}\n{text}"
                return f"[图片] {time_str}\n{p}"
            if kind == "file":
                return "\n".join(item.get("files", []) or [])
            return text
        if role == self.TextRole:
            return text
        if role == self.KindRole:
            return kind
        if role == self.ImagePathRole:
            return item.get("image_path", "")
        if role == self.FilesRole:
            return list(item.get("files", []) or [])
        if role == self.TimeRole:
            return time_str
        if role == self.SearchRole:
            if kind == "image":
                if text and text != "[图片]":
                    return "[图片] " + text
                return "[图片]"
            if kind == "file":
                parts = [os.path.basename(f) for f in (item.get("files", []) or [])]
                parts.extend(item.get("files", []) or [])
                return " ".join(parts)
            return text
        if role == self.ItemRole:
            return item
        if role == self.FavoriteRole:
            return bool(item.get("favorite"))
        return None

    # ---- 业务接口 ----
    def contains_key(self, key: tuple) -> bool:
        return key in self._key_set

    def row_of_key(self, key: tuple) -> Optional[int]:
        """返回指定 key 的行号（无则 None）。"""
        for i, it in enumerate(self._items):
            if dedupe_key(it) == key:
                return i
        return None

    def _emit_row_changed(self, row: int) -> None:
        self.dataChanged.emit(
            self.index(row, 0), self.index(row, 0),
            [QtCore.Qt.DisplayRole, self.TextRole, self.SearchRole,
             QtCore.Qt.ToolTipRole, self.FavoriteRole],
        )

    def _group_top_row(self, favorite: bool) -> int:
        """收藏组恒在最前，非收藏组紧随其后；返回该组最前一行的行号。"""
        return 0 if favorite else self.favorite_count()

    def _move_to_group_top(self, row: int) -> int:
        """把该行移到所属分组（收藏 / 非收藏）的最前，返回移动后的行号。"""
        item = self._items[row]
        target = self._group_top_row(bool(item.get("favorite")))
        if row == target:
            return row
        # Qt 的 destinationChild 按「移动前的行号」解释：同 parent 往下移时落点 = dest - 1，
        # 且 dest 落在 [row, row + 1] 区间内会被判非法（beginMoveRows 返回 False）。
        # 取消收藏正是下移（target > row）→ 必须传 target + 1；照传 target 会拿到 False，
        # 而未成对的 endMoveRows() 会把进程直接打崩（见 l_notepad/crash_20260927.log）。
        dest = target + 1 if target > row else target
        self.beginMoveRows(QtCore.QModelIndex(), row, row,
                           QtCore.QModelIndex(), dest)
        self._items.pop(row)
        self._items.insert(target, item)
        self.endMoveRows()
        return target

    def upsert_top(self, item: dict, max_stored: int = CLIPBOARD_MAX_STORED) -> bool:
        """把条目放到所属分组（收藏 / 非收藏）的最前。

        不存在则插入；已存在则刷新内容/时间并置顶。已存在条目保留其收藏标记
        （重新复制同内容不应丢失收藏）。返回 True 表示新增一行。
        """
        key = dedupe_key(item)
        row = self.row_of_key(key)
        if row is None:
            target = self._group_top_row(bool(item.get("favorite")))
            self.beginInsertRows(QtCore.QModelIndex(), target, target)
            self._items.insert(target, item)
            self._key_set.add(key)
            self.endInsertRows()
            self._trim(max_stored)
            return True
        merged = dict(item)
        merged["favorite"] = bool(self._items[row].get("favorite"))
        self._items[row] = merged
        self._emit_row_changed(self._move_to_group_top(row))
        return False

    def prepend(self, item: dict, max_stored: int = CLIPBOARD_MAX_STORED) -> None:
        """插入到最前（已存在同 key 则忽略）；超出上限时裁剪最旧的若干条。"""
        if not self.upsert_top(item, max_stored):
            return

    def _trim(self, max_stored: int) -> None:
        """裁剪到上限：只丢最旧的「非收藏」条目，收藏项永不因超限被丢。"""
        excess = len(self._items) - max_stored
        if excess <= 0:
            return
        drop_rows: list[int] = []
        for i in range(len(self._items) - 1, -1, -1):
            if excess <= 0:
                break
            if not self._items[i].get("favorite"):
                drop_rows.append(i)
                excess -= 1
        # drop_rows 由末尾往前扫描得来，已是降序：从后往前删，前面的行号不失效
        for row in drop_rows:
            self.beginRemoveRows(QtCore.QModelIndex(), row, row)
            dropped = self._items.pop(row)
            self._key_set.discard(dedupe_key(dropped))
            self.endRemoveRows()

    def set_favorite(self, key: tuple, favorite: bool) -> bool:
        """设置指定条目的收藏标记，并置顶到所属分组。返回 True 表示有变化。"""
        row = self.row_of_key(key)
        if row is None:
            return False
        if bool(self._items[row].get("favorite")) == bool(favorite):
            return False
        item = dict(self._items[row])
        item["favorite"] = bool(favorite)
        self._items[row] = item
        self._emit_row_changed(self._move_to_group_top(row))
        return True

    def favorite_count(self) -> int:
        """当前收藏条目数。"""
        return sum(1 for it in self._items if it.get("favorite"))

    def remove_key(self, key: tuple) -> Optional[dict]:
        """删除指定 key 的条目，返回被删除的条目（无则 None）。"""
        for i, it in enumerate(self._items):
            if dedupe_key(it) == key:
                self.beginRemoveRows(QtCore.QModelIndex(), i, i)
                removed = self._items.pop(i)
                self._key_set.discard(key)
                self.endRemoveRows()
                return removed
        return None

    def reset_items(self, items: list[dict]) -> None:
        self.beginResetModel()
        # 收藏组恒在最前；组内保持传入顺序（稳定分区）
        self._items = [it for it in items if it.get("favorite")] + \
                      [it for it in items if not it.get("favorite")]
        self._key_set = {dedupe_key(it) for it in self._items}
        self.endResetModel()

    def clear(self, keep_favorites: bool = True) -> int:
        """清空历史。``keep_favorites=True`` 时保留已收藏条目。返回移除条数。"""
        if not keep_favorites:
            removed = len(self._items)
            self.reset_items([])
            return removed
        kept = [it for it in self._items if it.get("favorite")]
        removed = len(self._items) - len(kept)
        if removed <= 0:
            return 0
        self.reset_items(kept)
        return removed

    def dedupe(self) -> int:
        """同 key 只保留最新一条（保持时间倒序）。返回移除条数。"""
        seen: set[tuple] = set()
        deduped: list[dict] = []
        for it in self._items:
            key = dedupe_key(it)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(it)
        removed = len(self._items) - len(deduped)
        if removed > 0:
            self.reset_items(deduped)
        return removed

    def update_item_at(self, row: int, item: dict) -> bool:
        """更新指定行内容。新 key 与其它行冲突时删除当前行（去重）。

        收藏标记由模型持有：编辑载荷即便不带（或带错）``favorite``，也一律沿用
        该行现有状态，避免"改个文字把收藏弄丢"。返回 True 表示已更新；
        False 表示因冲突删除了该行。
        """
        if not (0 <= row < len(self._items)):
            return False
        merged = dict(item)
        merged["favorite"] = bool(self._items[row].get("favorite"))
        old_key = dedupe_key(self._items[row])
        new_key = dedupe_key(merged)
        if new_key != old_key and new_key in self._key_set:
            self.remove_key(old_key)
            return False
        if new_key != old_key:
            self._key_set.discard(old_key)
            self._key_set.add(new_key)
        self._items[row] = merged
        self._emit_row_changed(row)
        return True


_WAKE_EVENT_TYPE = QtCore.QEvent.registerEventType()
_LOADED_EVENT_TYPE = QtCore.QEvent.registerEventType()


class _StoreEvent(QtCore.QEvent):
    """通用唤醒事件；``payload`` 承载附带数据（如加载结果）。"""

    def __init__(self, event_type: int, payload=None) -> None:
        super().__init__(QtCore.QEvent.Type(event_type))
        self.payload = payload


class ClipboardHistoryStore(QtCore.QObject):
    """剪贴板历史单例存储：唯一模型 + 唯一写盘线程。"""

    # 历史内容发生变化（新增/置顶/删除/清空/编辑）后发出，供界面刷新计数
    historyChanged = QtCore.Signal()

    _instance: Optional["ClipboardHistoryStore"] = None

    @classmethod
    def instance(cls) -> "ClipboardHistoryStore":
        if cls._instance is None:
            cls._instance = ClipboardHistoryStore(QtCore.QCoreApplication.instance())
        return cls._instance

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.model = ClipboardHistoryModel([])
        self.counters: dict[str, int] = {
            "upserted": 0,
            "moved_to_top": 0,
            "dropped_duplicate": 0,
            "persist_errors": 0,
            "loaded": 0,
        }
        self._lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._wake = threading.Event()
        self._dirty = False
        self._save_at = 0.0
        self._shutdown_done = False
        # 历史是否已成功读取并入模型。**未确认为 True 之前一律不写盘**：
        # 进程在加载事件被处理前退出（快速退出/崩溃/第二实例）时模型还是空的，
        # 若照写就会把磁盘上完整的历史覆盖成 []。
        self._history_ready = False
        self._blocked_write_logged = False
        # 本进程显式删掉/清掉的去重键：落盘前合并时不再从磁盘把它们捡回来
        # （只在成功写盘前有效，见 _clear_deleted）。
        self._deleted_keys: set[tuple] = set()
        # 本进程最近一次写盘后的文件指纹 + 写下去的内容：指纹一致说明盘上没被别人
        # 改过，可直接用上次的 payload 参与合并（省掉每次落盘重读整份 JSON）。
        self._last_write_sig: tuple | None = None
        self._last_payload: Optional[list[dict]] = None
        self._writer = threading.Thread(
            target=self._writer_loop, name="clipboard-writer", daemon=True)
        self._writer.start()
        self._load_history_async()

    # ── 诊断 ──
    def diagnostics(self) -> dict:
        with self._counter_lock:
            return dict(self.counters)

    def _bump(self, name: str, n: int = 1, log: bool = False) -> None:
        with self._counter_lock:
            self.counters[name] = self.counters.get(name, 0) + n
            value = self.counters[name]
            snapshot = dict(self.counters)
        if log or value % _LOG_EVERY == 0:
            lprint(f"剪贴板历史统计: {snapshot}")

    # ── 已删除键（落盘合并用，见 merge_items）──
    def _remember_deleted(self, *keys) -> None:
        """记下本进程显式删掉的键，合并磁盘时不再捡回来。"""
        with self._lock:
            self._deleted_keys.update(k for k in keys if k is not None)

    def _snapshot_deleted(self) -> frozenset:
        with self._lock:
            return frozenset(self._deleted_keys)

    def _clear_deleted(self) -> None:
        with self._lock:
            self._deleted_keys.clear()

    # ── 历史加载（后台线程 + 主线程入模型）──
    def _load_history_async(self) -> None:
        def _work() -> None:
            ok, items = self._read_history()
            self._post(_StoreEvent(_LOADED_EVENT_TYPE, (ok, items)))

        threading.Thread(target=_work, name="clipboard-loader", daemon=True).start()

    def _read_history(self) -> tuple[bool, list[dict]]:
        """读取历史文件（纯 IO+JSON，可在后台线程调用）。

        返回 ``(是否可信, 条目)``：文件不存在 = 可信的空历史；**读取/解析失败
        返回 ``False``**——此时磁盘内容未知，绝不能当成"空历史"回写覆盖。
        """
        path = history_file()
        if not path.exists():
            return True, []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            lprint(f"加载剪贴板历史失败（保留磁盘原文件，不覆盖）: {e}")
            return False, []
        if not isinstance(data, list):
            lprint("加载剪贴板历史失败（顶层不是数组，保留磁盘原文件）")
            return False, []
        return True, [it for it in data if isinstance(it, dict)]

    def _on_history_loaded(self, payload) -> None:
        ok, items = payload if isinstance(payload, tuple) else (False, [])
        if not ok:
            return
        existing = self.model.items()
        merged = bool(existing)
        if merged:
            # 加载期间可能已有新条目入模型（概率极低），新条目置前
            items = list(existing) + list(items)
        self.model.reset_items(items)
        self._history_ready = True
        self._bump("loaded", len(items))
        lprint(f"剪贴板历史已加载 {len(items)} 条")
        if merged:
            # 合并结果比磁盘新，补一次落盘（此刻已允许写盘）
            self.request_save()
        self.historyChanged.emit()

    # ── 捕获侧入口（捕获线程调用）──
    def submit_item(self, item: dict, png_bytes: bytes | None = None) -> None:
        """提交一条捕获结果（线程安全，可在捕获线程调用）。"""
        self._queue.put((item, png_bytes))
        self._post(_StoreEvent(_WAKE_EVENT_TYPE))

    def _post(self, event: QtCore.QEvent) -> None:
        app = QtCore.QCoreApplication.instance()
        if app is None:
            self._drain_sync()
            return
        QtCore.QCoreApplication.postEvent(self, event)

    def event(self, ev: QtCore.QEvent) -> bool:
        if ev.type() == _WAKE_EVENT_TYPE:
            self._drain_queue()
            return True
        if ev.type() == _LOADED_EVENT_TYPE:
            self._on_history_loaded(getattr(ev, "payload", None))
            return True
        return super().event(ev)

    def _drain_queue(self) -> None:
        while True:
            try:
                item, png_bytes = self._queue.get_nowait()
            except queue.Empty:
                return
            self._apply_item(item, png_bytes)

    def _drain_sync(self) -> None:
        """无 QApplication（离线测试）时直接在主调用线程处理队列。"""
        self._drain_queue()

    def _apply_item(self, item: dict, png_bytes: bytes | None) -> None:
        item = dict(item)
        item["time"] = now_iso()
        if png_bytes and item.get("image_path"):
            # 图片落盘立刻入写盘队列（缩略图随后生成），JSON 走防抖
            self._jobs.put(("image", str(item["image_path"]), png_bytes))
        inserted = self.model.upsert_top(item, CLIPBOARD_MAX_STORED)
        self._bump("upserted" if inserted else "moved_to_top")
        self.request_save()
        self.historyChanged.emit()

    # ── 界面侧入口（主线程调用）──
    def remove_item(self, item: dict) -> bool:
        """删除指定条目，并清理不再被引用的图片文件。

        收藏条目受保护：需先取消收藏才能删除，此处直接拒绝。
        """
        if item.get("favorite"):
            return False
        removed = self.model.remove_key(dedupe_key(item))
        if removed is None:
            return False
        self._remember_deleted(dedupe_key(item))
        self.cleanup_orphan_images()
        self.request_save()
        self.historyChanged.emit()
        return True

    def set_favorite(self, item: dict, favorite: bool) -> Optional[bool]:
        """设置条目收藏态。返回设置后的状态；条目不存在时返回 None。"""
        key = dedupe_key(item)
        if self.model.row_of_key(key) is None:
            return None
        self.model.set_favorite(key, favorite)
        self.request_save()
        self.historyChanged.emit()
        return bool(favorite)

    def toggle_favorite(self, item: dict) -> Optional[bool]:
        """切换条目收藏态。返回切换后的状态；条目不存在时返回 None。"""
        return self.set_favorite(item, not bool(item.get("favorite")))

    def update_item_at(self, row: int, item: dict) -> bool:
        """更新指定行（编辑保存）。返回 False 表示因冲突删除了该行。"""
        old_key = None
        current = self.model.items()
        if 0 <= row < len(current):
            old_key = dedupe_key(current[row])
        ok = self.model.update_item_at(row, item)
        # 改文本会让去重键变化（等价于改名）：旧 key 在磁盘上的那一版必须作废，
        # 否则下次落盘合并会把它当"别的实例写的新条目"再捡回来。
        if old_key is not None and dedupe_key(item) != old_key:
            self._remember_deleted(old_key)
        self.request_save()
        self.historyChanged.emit()
        return ok

    def update_item(self, item: dict, new_item: dict) -> bool:
        """按去重键定位并更新条目（编辑保存用）。

        编辑对话框从打开到保存之间，历史可能因新复制或收藏置顶而重排，行号会失准；
        这里保存时按 key 重新定位。收藏标记由模型沿用该行当前状态。
        返回 False 表示条目已不存在（或新内容与其它条目冲突被合并）。
        """
        row = self.model.row_of_key(dedupe_key(item))
        if row is None:
            return False
        return self.update_item_at(row, dict(new_item))

    def dedupe(self) -> int:
        """清理重复：同内容只保留最新一条。返回移除条数。"""
        removed = self.model.dedupe()
        if removed > 0:
            self.cleanup_orphan_images()
            self.request_save()
            self.historyChanged.emit()
        return removed

    def clear(self, keep_favorites: bool = True) -> int:
        """清空历史。``keep_favorites=True``（默认）时保留已收藏条目。

        返回被移除的条目数。图片缓存只按「仍被引用」清理，收藏项的原图不会误删。
        """
        before = self.model.items()
        removed = self.model.clear(keep_favorites=keep_favorites)
        if removed <= 0:
            return 0
        self._remember_deleted(*[
            dedupe_key(it) for it in before
            if not (keep_favorites and it.get("favorite"))
        ])
        if keep_favorites:
            self.cleanup_orphan_images()
        else:
            self.clear_images()
        self.request_save()
        self.historyChanged.emit()
        return removed

    def cleanup_orphan_images(self) -> None:
        """删除不再被任何历史条目引用的图片缓存文件（写盘线程执行）。

        历史尚未加载完成时**不删**：此刻模型是空/不完整的，按它算引用集会把
        真实历史的图片全当孤儿删掉。
        """
        if not self._history_ready:
            return
        referenced = {
            str(it.get("image_path", ""))
            for it in self.model.items()
            if it.get("kind") == "image" and it.get("image_path")
        }
        self._jobs.put(("orphans", frozenset(referenced)))
        self._wake.set()

    def clear_images(self) -> None:
        """删除全部图片缓存文件（写盘线程执行）。"""
        self._jobs.put(("clear_images", None))
        self._wake.set()

    # ── 落盘 ──
    def request_save(self, immediate: bool = False) -> None:
        """请求一次写盘（防抖合并；immediate=True 表示尽快写但不阻塞调用方）。"""
        with self._lock:
            self._dirty = True
            self._save_at = 0.0 if immediate else time.monotonic() + SAVE_DEBOUNCE_SEC
        self._wake.set()

    def flush(self, timeout: float = 5.0) -> bool:
        """等待挂起的写入完成（退出前调用）。返回是否在超时前完成。"""
        if not self._writer.is_alive():
            # 写盘线程已停止：退出路径重复调用时不再空等
            return False
        done = threading.Event()
        self._jobs.put(("flush", done))
        self._wake.set()
        return bool(done.wait(timeout))

    def shutdown(self, timeout: float = 3.0) -> None:
        """停止写盘线程（先 flush 再停，避免丢最后一条）。可重复调用。"""
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        self.flush(timeout)
        self._jobs.put(("quit", None))
        self._wake.set()
        if self._writer.is_alive():
            self._writer.join(timeout)

    # ── 写盘线程 ──
    def _wait_timeout(self) -> Optional[float]:
        with self._lock:
            if not self._dirty:
                return None
            return max(0.0, self._save_at - time.monotonic())

    def _save_if_due(self) -> bool:
        with self._lock:
            if not self._dirty or time.monotonic() < self._save_at:
                return False
            self._dirty = False
        self._write_history()
        return True

    def _writer_loop(self) -> None:
        while True:
            quit_now = False
            while True:
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    break
                if job[0] == "quit":
                    quit_now = True
                    break
                self._run_job(job)
            if quit_now:
                return
            if self._save_if_due():
                continue
            self._wake.wait(self._wait_timeout())
            self._wake.clear()

    def _run_job(self, job: tuple) -> None:
        kind = job[0]
        try:
            if kind == "image":
                self._write_image(job[1], job[2])
            elif kind == "orphans":
                self._delete_orphan_images(job[1])
            elif kind == "clear_images":
                self._delete_all_images()
            elif kind == "flush":
                with self._lock:
                    self._dirty = False
                self._write_history()
                job[1].set()
        except Exception as e:
            self._bump("persist_errors", log=True)
            lprint(f"剪贴板历史落盘失败({kind}): {e}")

    def _write_image(self, image_path: str, png_bytes: bytes) -> None:
        """原图落盘 + 生成小缩略图（{md5}_t.png）。"""
        if not image_path:
            return
        path = Path(image_path)
        if not path.exists():
            _atomic_write_bytes(path, png_bytes)
        thumb_path = f"{os.path.splitext(image_path)[0]}_t.png"
        if os.path.exists(thumb_path):
            return
        image = QtGui.QImage.fromData(png_bytes, "PNG")
        if image.isNull():
            return
        if max(image.width(), image.height()) > THUMB_SIZE:
            image = image.scaled(
                THUMB_SIZE, THUMB_SIZE,
                QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        buffer = QtCore.QByteArray()
        qbuf = QtCore.QBuffer(buffer)
        qbuf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        try:
            if image.save(qbuf, "PNG"):
                _atomic_write_bytes(Path(thumb_path), bytes(buffer))
        finally:
            qbuf.close()

    def _delete_orphan_images(self, referenced: frozenset) -> None:
        directory = images_dir()
        if not directory.is_dir():
            return
        try:
            for f in directory.iterdir():
                if f.is_file() and str(f) not in referenced:
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def _delete_all_images(self) -> None:
        directory = images_dir()
        if not directory.is_dir():
            return
        try:
            for f in directory.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _collect_payload_items(self) -> Optional[list[dict]]:
        """算出"该写进文件"的条目；返回 None 表示本次放弃写盘（保命）。

        多实例并存时磁盘上可能有本进程没见过（或已被别的实例改过）的条目，
        直接写内存模型会把它们覆盖掉——所以要按去重键与磁盘内容合并。

        文件指纹与上次写盘一致说明"盘上就是我们上次写的东西"，直接拿上次的
        payload 当磁盘内容参与合并即可，省掉重读整份 JSON 的 IO。
        """
        mem_items = self.model.items()
        if _file_sig(history_file()) == self._last_write_sig:
            disk_items = self._last_payload
            disk_ok = True
            if disk_items is None:
                disk_ok, disk_items = self._read_history()
        else:
            disk_ok, disk_items = self._read_history()
        if not disk_ok:
            if not self._blocked_write_logged:
                self._blocked_write_logged = True
                lprint("剪贴板历史读取失败，本次不落盘（避免覆盖磁盘上的完整历史）")
            return None
        if not disk_items:
            return mem_items
        merged = merge_items(disk_items, mem_items, self._snapshot_deleted())
        extra = len(merged) - len(mem_items)
        if extra > 0:
            self._bump("merged_from_disk", extra)
            lprint(f"剪贴板历史落盘前合并：并入磁盘上多出的 {extra} 条（多实例并存）")
        return merged

    def _write_history(self) -> None:
        """把当前模型序列化到历史文件（跨进程锁 + 临时文件原子替换）。

        历史未加载完成前直接跳过：此时模型不代表磁盘内容，写下去就是数据丢失。
        """
        if not self._history_ready:
            if not self._blocked_write_logged:
                self._blocked_write_logged = True
                lprint("剪贴板历史未加载完成，暂不落盘（避免覆盖磁盘上的完整历史）")
            return
        try:
            with _cross_process_lock() as locked:
                if not locked:
                    self._bump("lock_misses", log=True)
                payload_items = self._collect_payload_items()
                if payload_items is None:
                    return
                payload = json.dumps(
                    payload_items, ensure_ascii=False, indent=2
                ).encode("utf-8")
                _atomic_write_bytes(history_file(), payload)
                self._last_write_sig = _file_sig(history_file())
                self._last_payload = payload_items
                self._clear_deleted()
        except Exception as e:
            self._bump("persist_errors", log=True)
            lprint(f"保存剪贴板历史失败: {e}")
