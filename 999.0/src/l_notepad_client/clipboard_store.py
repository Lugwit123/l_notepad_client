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
from datetime import datetime
from pathlib import Path
from typing import Optional

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
             QtCore.Qt.ToolTipRole],
        )

    def upsert_top(self, item: dict, max_stored: int = CLIPBOARD_MAX_STORED) -> bool:
        """把条目放到最前：不存在则插入；已存在则移到最前并刷新内容/时间。

        返回 True 表示新增一行；False 表示既有行被置顶覆盖。
        """
        key = dedupe_key(item)
        row = self.row_of_key(key)
        if row is None:
            self.beginInsertRows(QtCore.QModelIndex(), 0, 0)
            self._items.insert(0, item)
            self._key_set.add(key)
            self.endInsertRows()
            self._trim(max_stored)
            return True
        if row == 0:
            self._items[0] = item
            self._emit_row_changed(0)
            return False
        self.beginMoveRows(QtCore.QModelIndex(), row, row, QtCore.QModelIndex(), 0)
        self._items.pop(row)
        self._items.insert(0, item)
        self.endMoveRows()
        self._emit_row_changed(0)
        return False

    def prepend(self, item: dict, max_stored: int = CLIPBOARD_MAX_STORED) -> None:
        """插入到最前（已存在同 key 则忽略）；超出上限时裁剪最旧的若干条。"""
        if not self.upsert_top(item, max_stored):
            return

    def _trim(self, max_stored: int) -> None:
        if len(self._items) <= max_stored:
            return
        start, end = max_stored, len(self._items) - 1
        self.beginRemoveRows(QtCore.QModelIndex(), start, end)
        for dropped in self._items[max_stored:]:
            self._key_set.discard(dedupe_key(dropped))
        del self._items[max_stored:]
        self.endRemoveRows()

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
        self._items = list(items)
        self._key_set = {dedupe_key(it) for it in self._items}
        self.endResetModel()

    def clear(self) -> None:
        self.reset_items([])

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

        返回 True 表示已更新；False 表示因冲突删除了该行。
        """
        if not (0 <= row < len(self._items)):
            return False
        old_key = dedupe_key(self._items[row])
        new_key = dedupe_key(item)
        if new_key != old_key and new_key in self._key_set:
            self.remove_key(old_key)
            return False
        if new_key != old_key:
            self._key_set.discard(old_key)
            self._key_set.add(new_key)
        self._items[row] = item
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

    # ── 历史加载（后台线程 + 主线程入模型）──
    def _load_history_async(self) -> None:
        def _work() -> None:
            items = self._read_history()
            self._post(_StoreEvent(_LOADED_EVENT_TYPE, items))

        threading.Thread(target=_work, name="clipboard-loader", daemon=True).start()

    def _read_history(self) -> list[dict]:
        """读取历史文件（纯 IO+JSON，可在后台线程调用）。"""
        path = history_file()
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [it for it in data if isinstance(it, dict)]
        except Exception as e:
            lprint(f"加载剪贴板历史失败: {e}")
        return []

    def _on_history_loaded(self, items: list) -> None:
        if not isinstance(items, list):
            return
        existing = self.model.items()
        if existing:
            # 加载期间可能已有新条目入模型（概率极低），新条目置前
            items = list(existing) + list(items)
        self.model.reset_items(items)
        self._bump("loaded", len(items))
        lprint(f"剪贴板历史已加载 {len(items)} 条")
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
            self._on_history_loaded(getattr(ev, "payload", []) or [])
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
        """删除指定条目，并清理不再被引用的图片文件。"""
        removed = self.model.remove_key(dedupe_key(item))
        if removed is None:
            return False
        self.cleanup_orphan_images()
        self.request_save()
        self.historyChanged.emit()
        return True

    def update_item_at(self, row: int, item: dict) -> bool:
        """更新指定行（编辑保存）。返回 False 表示因冲突删除了该行。"""
        ok = self.model.update_item_at(row, item)
        self.request_save()
        self.historyChanged.emit()
        return ok

    def dedupe(self) -> int:
        """清理重复：同内容只保留最新一条。返回移除条数。"""
        removed = self.model.dedupe()
        if removed > 0:
            self.cleanup_orphan_images()
            self.request_save()
            self.historyChanged.emit()
        return removed

    def clear(self) -> None:
        """清空历史（连同图片缓存文件）。"""
        self.model.clear()
        self.clear_images()
        self.request_save()
        self.historyChanged.emit()

    def cleanup_orphan_images(self) -> None:
        """删除不再被任何历史条目引用的图片缓存文件（写盘线程执行）。"""
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

    def _write_history(self) -> None:
        """把当前模型序列化到历史文件（临时文件 + 原子替换）。"""
        try:
            payload = json.dumps(
                self.model.items(), ensure_ascii=False, indent=2
            ).encode("utf-8")
            _atomic_write_bytes(history_file(), payload)
        except Exception as e:
            self._bump("persist_errors", log=True)
            lprint(f"保存剪贴板历史失败: {e}")
