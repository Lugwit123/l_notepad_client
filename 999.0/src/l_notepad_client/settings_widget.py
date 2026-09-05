# -*- coding: utf-8 -*-
"""
设置页面组件 - l_notepad 和 l_folder_favorites 的设置
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from pytracemp import lprint

from .paths import data_root, favorites_dir, notes_dir


class SettingsWidget(QtWidgets.QWidget):
    """设置页面组件"""

    indent_display_changed = QtCore.Signal()
    folder_hotkey_changed = QtCore.Signal(str)
    font_size_changed = QtCore.Signal(int)

    _INDENT_MODE_ITEMS = (
        ("不显示", "none"),
        ("色块", "blocks"),
        ("圆点", "dots"),
        ("色块 + 圆点", "blocks_and_dots"),
    )

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = QtCore.QSettings("Lugwit", "l_notepad_pc")
        self._setup_data()
        self._setup_ui()
        self._load_settings()

    def _setup_data(self) -> None:
        """初始化数据路径（统一存放于 ~/.Lugwit/l_notepad）"""
        self.data_root = data_root()
        self.folder_fav_dir = favorites_dir()
        self.favorites_file = self.folder_fav_dir / "favorites.json"
        self.clipboard_file = self.folder_fav_dir / "clipboard_history.json"
        self.config_file = self.folder_fav_dir / "config.json"
        self.notes_dir = notes_dir()
        self._load_folder_fav_settings()
        self._load_folder_fav_hotkey()

    def _load_folder_fav_hotkey(self) -> None:
        """加载文件夹收藏的快捷键设置"""
        self.hotkey_button = "middle"  # 默认 Ctrl + 中键
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.hotkey_button = data.get("hotkey_button", "middle")
            except Exception:
                pass

    def _load_folder_fav_settings(self) -> None:
        """加载文件夹收藏的设置"""
        self.clipboard_max_items = 3  # 默认值
        if self.clipboard_file.exists():
            try:
                with open(self.clipboard_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.clipboard_max_items = data.get("max_items", 3)
            except Exception:
                pass

    def _setup_ui(self) -> None:
        """初始化UI"""
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 内容容器（在 ScrollArea 中）
        content_widget = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content_widget)
        content_layout.setSpacing(20)

        # ===== 笔记本设置区域 =====
        notepad_group = self._create_group("📓 笔记本设置")
        notepad_layout = QtWidgets.QVBoxLayout()

        # 快捷键设置
        hotkey_layout = QtWidgets.QFormLayout()
        hotkey_layout.setSpacing(10)

        # 双击热键选择
        self.hotkey_combo = QtWidgets.QComboBox()
        self.hotkey_combo.addItems(["Ctrl", "Alt", "Shift"])
        self.hotkey_combo.currentTextChanged.connect(self._on_hotkey_changed)
        hotkey_layout.addRow("双击热键:", self.hotkey_combo)

        # 双击间隔
        self.hotkey_interval_spin = QtWidgets.QDoubleSpinBox()
        self.hotkey_interval_spin.setRange(0.05, 1.0)
        self.hotkey_interval_spin.setSingleStep(0.05)
        self.hotkey_interval_spin.setSuffix(" 秒")
        self.hotkey_interval_spin.setMinimumWidth(96)
        self.hotkey_interval_spin.valueChanged.connect(self._on_interval_changed)
        hotkey_layout.addRow("双击间隔:", self.hotkey_interval_spin)

        notepad_layout.addLayout(hotkey_layout)
        notepad_group.setLayout(notepad_layout)
        content_layout.addWidget(notepad_group)

        # ===== 编辑器 / Python 缩进显示 =====
        editor_group = self._create_group("📝 编辑器 · Python 缩进显示")
        editor_layout = QtWidgets.QFormLayout()
        editor_layout.setSpacing(10)

        self.indent_mode_combo = QtWidgets.QComboBox()
        for label, mode in self._INDENT_MODE_ITEMS:
            self.indent_mode_combo.addItem(label, mode)
        self.indent_mode_combo.currentIndexChanged.connect(self._on_indent_display_changed)
        editor_layout.addRow("显示方式:", self.indent_mode_combo)

        self.indent_opacity_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.indent_opacity_slider.setRange(0, 100)
        self.indent_opacity_slider.setValue(100)
        self.indent_opacity_label = QtWidgets.QLabel("100%")
        self.indent_opacity_label.setMinimumWidth(42)
        opacity_row = QtWidgets.QHBoxLayout()
        opacity_row.addWidget(self.indent_opacity_slider, 1)
        opacity_row.addWidget(self.indent_opacity_label)
        self.indent_opacity_slider.valueChanged.connect(self._on_indent_opacity_changed)
        editor_layout.addRow("颜色深浅:", opacity_row)

        self.indent_block_width_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.indent_block_width_slider.setRange(50, 100)
        self.indent_block_width_slider.setValue(100)
        self.indent_block_width_label = QtWidgets.QLabel("100%")
        self.indent_block_width_label.setMinimumWidth(42)
        width_row = QtWidgets.QHBoxLayout()
        width_row.addWidget(self.indent_block_width_slider, 1)
        width_row.addWidget(self.indent_block_width_label)
        self.indent_block_width_slider.valueChanged.connect(self._on_indent_block_width_changed)
        editor_layout.addRow("色块宽度:", width_row)

        indent_hint = QtWidgets.QLabel(
            "仅 Python 模式下行首缩进生效；切换后立即应用到所有编辑器。"
        )
        indent_hint.setStyleSheet("color: gray; font-size: 11px;")
        editor_layout.addRow("", indent_hint)

        editor_group.setLayout(editor_layout)
        content_layout.addWidget(editor_group)

        # ===== 文件夹收藏设置区域 =====
        folder_fav_group = self._create_group("📁 文件夹收藏设置")
        folder_fav_layout = QtWidgets.QVBoxLayout()

        # 快捷键设置
        hotkey_layout = QtWidgets.QHBoxLayout()
        hotkey_label = QtWidgets.QLabel("全局显示快捷键:")
        hotkey_layout.addWidget(hotkey_label)

        self.folder_hotkey_combo = QtWidgets.QComboBox()
        self.folder_hotkey_combo.addItem("Ctrl + 中键", "middle")
        self.folder_hotkey_combo.addItem("Ctrl + 左键", "left")
        # 加载已保存的设置
        idx = self.folder_hotkey_combo.findData(self.hotkey_button)
        if idx >= 0:
            self.folder_hotkey_combo.setCurrentIndex(idx)
        self.folder_hotkey_combo.currentIndexChanged.connect(self._on_folder_hotkey_changed)
        hotkey_layout.addWidget(self.folder_hotkey_combo)
        hotkey_layout.addStretch()
        folder_fav_layout.addLayout(hotkey_layout)

        # 快捷键提示
        hotkey_hint = QtWidgets.QLabel("提示: 切换后立即生效，无需重启")
        hotkey_hint.setStyleSheet("color: gray; font-size: 11px;")
        folder_fav_layout.addWidget(hotkey_hint)

        # 剪贴板历史条数
        clipboard_layout = QtWidgets.QHBoxLayout()
        clipboard_label = QtWidgets.QLabel("剪贴板历史显示条数:")
        clipboard_layout.addWidget(clipboard_label)

        self.clipboard_count_combo = QtWidgets.QComboBox()
        for i in range(1, 11):
            self.clipboard_count_combo.addItem(str(i), i)
        self.clipboard_count_combo.setCurrentText(str(self.clipboard_max_items))
        self.clipboard_count_combo.currentIndexChanged.connect(self._on_clipboard_count_changed)
        clipboard_layout.addWidget(self.clipboard_count_combo)
        clipboard_layout.addStretch()

        folder_fav_layout.addLayout(clipboard_layout)

        # 收藏夹数据管理
        data_mgmt_layout = QtWidgets.QHBoxLayout()
        data_mgmt_label = QtWidgets.QLabel("收藏夹数据:")
        data_mgmt_layout.addWidget(data_mgmt_label)

        self.open_data_btn = QtWidgets.QPushButton("打开数据文件夹")
        self.open_data_btn.clicked.connect(self._open_data_folder)
        data_mgmt_layout.addWidget(self.open_data_btn)

        self.clear_data_btn = QtWidgets.QPushButton("清空收藏夹")
        self.clear_data_btn.clicked.connect(self._clear_favorites)
        data_mgmt_layout.addWidget(self.clear_data_btn)

        data_mgmt_layout.addStretch()
        folder_fav_layout.addLayout(data_mgmt_layout)

        folder_fav_group.setLayout(folder_fav_layout)
        content_layout.addWidget(folder_fav_group)

        # ===== 离线数据存储位置 =====
        offline_group = self._create_group("💾 离线数据存储位置")
        offline_layout = QtWidgets.QVBoxLayout()

        # 笔记数据：~/.Lugwit/l_notepad/notepad_list（.md 文件离线保存）
        self._add_offline_path_row(offline_layout, "笔记数据:", self.notes_dir)
        # 收藏夹/剪贴板历史：~/.Lugwit/l_notepad/favorites
        self._add_offline_path_row(offline_layout, "收藏夹数据:", self.folder_fav_dir)
        # 根目录：账号收藏、登录令牌、版本历史等
        self._add_offline_path_row(offline_layout, "数据根目录:", self.data_root)

        offline_hint = QtWidgets.QLabel(
            "本地数据已统一存放在 ~/.Lugwit/l_notepad 下"
            "（笔记 / 收藏夹 / 账号 / 版本历史）；路径文字可选中复制。"
        )
        offline_hint.setStyleSheet("color: gray; font-size: 11px;")
        offline_hint.setWordWrap(True)
        offline_layout.addWidget(offline_hint)

        offline_group.setLayout(offline_layout)
        content_layout.addWidget(offline_group)

        # ===== 通用设置区域 =====
        general_group = self._create_group("⚙️ 通用设置")
        general_layout = QtWidgets.QVBoxLayout()

        # 主题设置
        theme_layout = QtWidgets.QHBoxLayout()
        theme_label = QtWidgets.QLabel("主题:")
        theme_layout.addWidget(theme_label)

        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.addItems(["深色", "浅色", "跟随系统"])
        theme_layout.addWidget(self.theme_combo)
        theme_layout.addStretch()
        general_layout.addLayout(theme_layout)

        # 字体大小
        font_layout = QtWidgets.QHBoxLayout()
        font_label = QtWidgets.QLabel("字体大小:")
        font_layout.addWidget(font_label)

        self.font_size_spin = QtWidgets.QSpinBox()
        self.font_size_spin.setObjectName("SettingsFontSizeSpin")
        self.font_size_spin.setRange(8, 28)
        self.font_size_spin.setSingleStep(1)
        self.font_size_spin.setSuffix(" pt")
        self.font_size_spin.setMinimumWidth(96)
        self.font_size_spin.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self.font_size_spin.valueChanged.connect(self._on_font_size_changed)
        font_layout.addWidget(self.font_size_spin)
        font_layout.addStretch()
        general_layout.addLayout(font_layout)

        general_group.setLayout(general_layout)
        content_layout.addWidget(general_group)

        # 添加弹性空间
        content_layout.addStretch()

        # 添加到滚动区域
        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(content_widget)
        scroll_area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        layout.addWidget(scroll_area)

    def _create_group(self, title: str) -> QtWidgets.QGroupBox:
        """创建分组框"""
        group = QtWidgets.QGroupBox(title)
        group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                border: 1px solid #555;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        return group

    def _load_settings(self) -> None:
        """加载设置"""
        # 笔记本快捷键设置
        hotkey_key = self._settings.value("hotkey/key", "Ctrl", type=str)
        index = self.hotkey_combo.findText(hotkey_key)
        if index >= 0:
            self.hotkey_combo.setCurrentIndex(index)

        hotkey_interval = self._settings.value("hotkey/interval", 0.15, type=float)
        self.hotkey_interval_spin.setValue(hotkey_interval)

        # 文件夹收藏设置
        self.clipboard_count_combo.setCurrentText(str(self.clipboard_max_items))

        # 通用设置
        theme = self._settings.value("general/theme", "深色", type=str)
        index = self.theme_combo.findText(theme)
        if index >= 0:
            self.theme_combo.setCurrentIndex(index)

        font_size = self._read_font_size_setting()
        self.font_size_spin.blockSignals(True)
        self.font_size_spin.setValue(font_size)
        self.font_size_spin.blockSignals(False)

        self._load_indent_display_settings()

    @staticmethod
    def _read_font_size_from_settings(settings: QtCore.QSettings) -> int:
        """与 MainWindow 一致：优先 ui/text_font_size，兼容旧键 general/font_size。"""
        raw = settings.value("ui/text_font_size")
        if raw is not None and str(raw).strip():
            try:
                return max(8, min(28, int(str(raw))))
            except ValueError:
                pass
        legacy = settings.value("general/font_size", 10, type=int)
        return max(8, min(28, int(legacy)))

    def _read_font_size_setting(self) -> int:
        return self._read_font_size_from_settings(self._settings)

    def _load_indent_display_settings(self) -> None:
        mode = str(self._settings.value("editor/indent_mode", "blocks"))
        self.indent_mode_combo.blockSignals(True)
        idx = self.indent_mode_combo.findData(mode)
        if idx < 0:
            idx = self.indent_mode_combo.findData("blocks")
        if idx >= 0:
            self.indent_mode_combo.setCurrentIndex(idx)
        self.indent_mode_combo.blockSignals(False)

        opacity = int(self._settings.value("editor/indent_opacity", 100))
        opacity = max(0, min(100, opacity))
        self.indent_opacity_slider.blockSignals(True)
        self.indent_opacity_slider.setValue(opacity)
        self.indent_opacity_slider.blockSignals(False)
        self.indent_opacity_label.setText(f"{opacity}%")

        block_width = int(self._settings.value("editor/indent_block_width", 100))
        block_width = max(50, min(100, block_width))
        self.indent_block_width_slider.blockSignals(True)
        self.indent_block_width_slider.setValue(block_width)
        self.indent_block_width_slider.blockSignals(False)
        self.indent_block_width_label.setText(f"{block_width}%")
        self._update_indent_width_controls_enabled()

    def _current_indent_mode(self) -> str:
        mode = self.indent_mode_combo.currentData()
        return str(mode) if mode else "blocks"

    def _update_indent_width_controls_enabled(self) -> None:
        mode = self._current_indent_mode()
        show_blocks = mode in ("blocks", "blocks_and_dots")
        show_dots = mode in ("dots", "blocks_and_dots")
        self.indent_block_width_slider.setEnabled(show_blocks)
        self.indent_block_width_label.setEnabled(show_blocks)
        self.indent_opacity_slider.setEnabled(mode != "none")
        self.indent_opacity_label.setEnabled(mode != "none")
        if not show_dots and not show_blocks:
            return
        if not show_blocks:
            self.indent_block_width_label.setText("-")

    def _save_indent_display_settings(self) -> None:
        mode = self._current_indent_mode()
        opacity = int(self.indent_opacity_slider.value())
        block_width = int(self.indent_block_width_slider.value())
        self._settings.setValue("editor/indent_mode", mode)
        self._settings.setValue("editor/indent_opacity", opacity)
        self._settings.setValue("editor/indent_block_width", block_width)
        self._settings.sync()
        self.indent_display_changed.emit()

    def _on_indent_display_changed(self, _index: int = -1) -> None:
        self._update_indent_width_controls_enabled()
        self._save_indent_display_settings()

    def _on_indent_opacity_changed(self, value: int) -> None:
        self.indent_opacity_label.setText(f"{int(value)}%")
        self._save_indent_display_settings()

    def _on_indent_block_width_changed(self, value: int) -> None:
        self.indent_block_width_label.setText(f"{int(value)}%")
        self._save_indent_display_settings()

    def _on_font_size_changed(self, size: int) -> None:
        size = max(8, min(28, int(size)))
        self._settings.setValue("ui/text_font_size", str(size))
        self._settings.sync()
        self.font_size_changed.emit(size)

    def _on_hotkey_changed(self, value: str) -> None:
        """热键改变"""
        self._settings.setValue("hotkey/key", value)
        # 通知主窗口更新热键
        if hasattr(self.parent(), "_hotkey_key_callback") and self.parent()._hotkey_key_callback:
            self.parent()._hotkey_key_callback(value)

    def _on_interval_changed(self, value: float) -> None:
        """间隔改变"""
        self._settings.setValue("hotkey/interval", value)
        # 通知主窗口更新间隔
        if hasattr(self.parent(), "_hotkey_interval_callback") and self.parent()._hotkey_interval_callback:
            self.parent()._hotkey_interval_callback(value)

    def _on_clipboard_count_changed(self, index: int) -> None:
        """剪贴板条数改变"""
        count = self.clipboard_count_combo.itemData(index)
        if count:
            self.clipboard_max_items = count
            self._save_clipboard_settings()
            # 通知文件夹收藏组件更新
            self._notify_clipboard_update()

    def _save_clipboard_settings(self) -> None:
        """保存剪贴板设置"""
        try:
            data = {"max_items": self.clipboard_max_items}
            with open(self.clipboard_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            lprint(f"保存剪贴板设置失败: {e}")

    def _notify_clipboard_update(self) -> None:
        """通知文件夹收藏组件更新"""
        # 可以通过信号机制通知，这里暂时打印日志
        lprint(f"剪贴板显示条数已更新为: {self.clipboard_max_items}")

    def _add_offline_path_row(
        self, layout: QtWidgets.QVBoxLayout, name: str, path: Path
    ) -> None:
        """添加一行「数据名称 + 路径(可选中复制) + 打开按钮」。"""
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(name))
        path_label = QtWidgets.QLabel(str(path))
        path_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlags.TextSelectableByMouse
        )
        path_label.setStyleSheet("color: gray; font-size: 11px;")
        row.addWidget(path_label, 1)
        open_btn = QtWidgets.QPushButton("打开")
        open_btn.setToolTip(str(path))
        open_btn.clicked.connect(lambda _=False, p=path: self._open_folder(p))
        row.addWidget(open_btn)
        layout.addLayout(row)

    def _open_folder(self, path: Path) -> None:
        """打开指定数据文件夹（不存在时先创建）。"""
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "错误", f"打开文件夹失败: {e}")

    def _open_data_folder(self) -> None:
        """打开数据文件夹"""
        self._open_folder(self.folder_fav_dir)

    def _clear_favorites(self) -> None:
        """清空收藏夹"""
        reply = QtWidgets.QMessageBox.question(
            self,
            "确认清空",
            "确定要清空所有收藏的文件夹和命令吗？此操作不可撤销。",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            try:
                # 清空收藏夹
                if self.favorites_file.exists():
                    self.favorites_file.write_text("[]", encoding="utf-8")

                # 清空剪贴板历史
                if self.clipboard_file.exists():
                    self.clipboard_file.write_text("[]", encoding="utf-8")

                QtWidgets.QMessageBox.information(self, "成功", "已清空所有收藏夹数据")

                # 通知主窗口刷新
                if hasattr(self.parent(), "refresh_notes"):
                    self.parent().refresh_notes()
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "错误", f"清空失败: {e}")

    def _on_folder_hotkey_changed(self, index: int) -> None:
        """文件夹收藏快捷键改变"""
        self.hotkey_button = self.folder_hotkey_combo.itemData(index)
        self._save_folder_fav_hotkey()

    def _save_folder_fav_hotkey(self) -> None:
        try:
            data = {"hotkey_button": self.hotkey_button}
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.folder_hotkey_changed.emit(self.hotkey_button)
        except Exception as e:
            lprint(f"保存文件夹收藏快捷键设置失败: {e}")
