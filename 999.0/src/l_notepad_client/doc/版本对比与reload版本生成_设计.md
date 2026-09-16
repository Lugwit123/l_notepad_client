# 版本对比 + reload 版本生成 设计文档

- 程序：`l_notepad_client`（`rez-package-source/l_notepad_client/999.0/src/l_notepad_client`）
- 相关文件：`ui.py`、`history_store.py`、新增 `version_diff_dialog.py`
- 状态：设计稿（待评审），未开始编码

---

## 1. 背景与目标

### 1.1 背景

右侧面板已有「版本」下拉框（`combo_version`），数据来自本地 SQLite 版本库
（`history_store.py`，表 `versions`）。但当前只能**切换到**某个历史版本，无法
**看清两个版本之间改了什么**；同时也缺少一条从「磁盘上被外部改动的文件」回写版本
历史的路径。

### 1.2 目标

| 编号 | 目标 | 类型 |
| --- | --- | --- |
| A | 版本下拉框右侧新增「对比基准」下拉框、「对比目标」下拉框、「对比」按钮；默认 = 最新版 vs 上一版；点击后并排展示差异 | 新功能 |
| B | 修复：外部/本地文件被外部修改后点「重载」，编辑器内容更新了，但版本历史没有生成新版本 | Bug 修复 |

非目标（本期不做，见 §7）：

- 版本回滚到磁盘/服务端的一键提交（现在仍需「重载版本 → 保存」两步）
- 行内（字符级）差异高亮、三方合并、跨文件对比

---

## 2. 现状分析

### 2.1 版本数据模型（`history_store.py`）

- 表：`versions(id, kind, ref, title, content, saved_at)`，索引 `(kind, ref, id DESC)`
- 每个 `(kind, ref)` 最多 `MAX_VERSIONS_PER_REF = 100` 条，超出自动删最旧（`history_store.py:21,78-87`）
- `add_version(kind, ref, title, content) -> bool`：内容与上一版本完全相同则跳过（`:59-89`）
- `list_versions(kind, ref)`：新→旧，仅 `id/title/saved_at/length/preview`，**不含全文**（`:110-134`）
- `get_version(version_id)`：单条全文（`:137-145`）
- `migrate_versions(kind, old_ref, new_ref)`：笔记改名后迁移（`:92-107`）

内容来源标识 `(kind, ref, title)` 由 `ui.py:2571 _current_version_context()` 给出：

| kind | ref | 场景 |
| --- | --- | --- |
| `note` | 笔记 id 字符串 | 普通笔记 |
| `log` | 服务器日志 posix 路径 | 服务器日志 |
| `external` | 文件绝对路径 | 外部文件 / IPC 推入文件 |

（IPC 文件经 `_set_external_file_editor` 统一落成 `_current_external_file`，见 `ui.py:3244`，
因此与 `external` 共用 ref。）

### 2.2 版本写入时机（现有 5 处，全部来自「保存/打开」）

| 位置 | 时机 | kind |
| --- | --- | --- |
| `ui.py:3270` | 打开外部/IPC 文件时落基线版本 | `external` |
| `ui.py:2563` | 保存笔记 | `note` |
| `ui.py:2857` | 自动保存笔记 | `note` |
| `ui.py:3766` | 保存服务器日志 | `log` |
| `ui.py:3821` | 保存外部文件 | `external` |

**缺口：`_reload_local_file_from_disk`（`ui.py:5004`）不在上表内** —— 即需求 B。

### 2.3 现有版本 UI

`ui.py:639-658`（`RightPanel.__init__`，行布局挂在 `root: QVBoxLayout`）：

```python
version_row = QtWidgets.QHBoxLayout()
version_row.setSpacing(8)
self.label_version = QLabel("📜 版本")            # objectName=label_version
version_row.addWidget(self.label_version)
self.combo_version = _VersionComboBox(self)     # objectName=combo_version, minWidth=260
self.combo_version.setToolTip("查看并切换到该笔记/日志的历史保存版本")
self.combo_version.addItem("📜 切换版本", None)   # 占位项 data=None
version_row.addWidget(self.combo_version)
version_row.addStretch()                        # 插入点：stretch 之前
root.addLayout(version_row)
```

- item 的 `data` = 版本 id（`ui.py:2633`）；label 形如 `v3 · 2026-09-15T10:20:11 · 812字（最新） | 首行预览`
- 展开前刷新：`_VersionComboBox.aboutToShowPopup`（`ui.py:530-565`）→ `_populate_version_combo`（`ui.py:2595`）
- 选中切换：`activated` → `_on_version_combo_activated`（`ui.py:2646`）→ `_apply_version`（`ui.py:2677`，写编辑器 + `dirty=True`）
- 内容切换时重同步：`_sync_version_combo_on_open`（`ui.py:2658`，5 处调用：`3275/3615/3741/4733/5052`）
- 信号连接：`ui.py:3096-3097`

### 2.4 缺失能力

全包无任何 diff 实现（无 `difflib` / `SequenceMatcher` / `unified_diff` 使用），需新写。

---

## 3. 需求 A：版本对比

### 3.1 控件布局（`version_row` 最终顺序）

```
[📜 版本] [combo_version]    ⇄ 差异  [combo_diff_left: 旧版]  →  [combo_diff_right: 新版]  [对比]
```

新增（插在 `combo_version` 之后、`addStretch()` 之前）：

| 控件 | 类型 | objectName | 宽度 | 默认 | ToolTip |
| --- | --- | --- | --- | --- | --- |
| `label_diff` | QLabel | `label_diff` | - | `⇄ 差异` | - |
| `combo_diff_left` | QComboBox | `combo_diff_left` | min 180 | 上一版 | 选择「旧」版本（对比基准） |
| `label_diff_arrow` | QLabel | `label_diff_arrow` | - | `→` | - |
| `combo_diff_right` | QComboBox | `combo_diff_right` | min 180 | 最新版 | 选择「新」版本 |
| `btn_diff` | QPushButton | `btn_diff` | - | `对比` | 并排对比两个版本的内容 |

（标签文案用「⇄ 差异」而非「对比」，避免与右侧按钮重名。）

- 字号/内边距复用 `combo_version` 的内联样式，抽成模块级常量
  `_VERSION_COMBO_QSS`（`ui.py:649-654` 现为内联字符串），三个下拉框共用，避免样式漂移。
- 新增控件全部在 `RightPanel.__init__` 创建，并加入 `bind_to()` 的 `mapping`（`ui.py:783-806`），
  使 `mw.combo_diff_left` 等可用（与现有 `label_version/combo_version` 一致）。
- 标签与控件在 `_set_right_panel_mode(...)`（`ui.py` 中控制 `combo_version` 可见性，约 `3225-3227`）
  同一分支里一起显示/隐藏，保证「问AI 面板」不出现对比控件。

### 3.2 两个下拉框的内容与默认值

- 数据源：与 `combo_version` **复用同一次** `history_store.list_versions(kind, ref)` 结果，
  在 `_populate_version_combo` 内一并填充，避免重复查库。
- 排序：按 `id` **升序**（旧 → 新）填充，label 与 `combo_version` 同格式
  （便于对照，如 `v2 · 2026-09-15T09:10:03 · 640字`），`data` 同为版本 id。
- 默认选中：
  - `combo_diff_right` = 最新（`versions[0]`，即 `id` 最大）
  - `combo_diff_left` = 上一版（`versions[1]`）
- 版本数不足时：

| 情况 | 行为 |
| --- | --- |
| 无版本 / 内容不支持历史 | 两个下拉显示占位项并 `setEnabled(False)`，`btn_diff` 禁用 |
| 只有 1 个版本 | `left` 仅一项 `（无上一版）`（data=None），`btn_diff` 禁用并提示「至少需要两个版本才能对比」 |
| 用户手动切了 `combo_version` | 不改动对比下拉（两套选择互相独立），但 `_current_version_id` 变化只影响编辑器 |
| 切换到其它笔记/日志/文件 | `_sync_version_combo_on_open` 时重置为默认（最新 vs 上一版） |

### 3.3 对比执行（核心算法）

新模块 `version_diff_dialog.py`：

```python
@dataclass(frozen=True)
class DiffLine:
    kind: str          # "equal" | "replace" | "delete" | "insert"
    left_no: int | None    # 左侧行号（None=占位空行）
    right_no: int | None
    left_text: str
    right_text: str

def build_side_by_side(a_text: str, b_text: str) -> list[DiffLine]:
    """行级对齐：用 SequenceMatcher opcodes 生成左右等长的行数组。"""
    import difflib
    a = a_text.splitlines()
    b = b_text.splitlines()
    rows: list[DiffLine] = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        ...
    return rows

def diff_stats(rows: list[DiffLine]) -> tuple[int, int, int]:
    """返回 (新增行, 删除行, 修改行)。"""
```

- 使用 `SequenceMatcher` 而非 `unified_diff`：需要左右行号与逐行配对，`unified_diff`
  的 hunk 形式不适合双栏。
- 对齐规则：`equal` 逐行配对；`delete` 右侧补空行；`insert` 左侧补空行；
  `replace` 按行数多的那侧配平（多出的记为 `insert`/`delete` 语义但仍显示为 `replace` 行，
  以保证左右行号对齐）。**左右两个列表长度必须相等**，这是同步滚动与高亮的前提。
- 行内容一律按「原始行」比较（不做 strip），避免伪装成修改；「忽略空白差异」作为
  后续增强（见 §7）。

### 3.4 展示：双栏并排对话框

`VersionDiffDialog(QtWidgets.QDialog)`（新文件 `version_diff_dialog.py`，参照
`account_favorites_widget.py:107 CustomFieldManageDialog` 的对话框写法）：

```
┌──────────────────────────────────────────────────────────────┐
│ 对比：note.md   v2 (2026-09-15 09:10) → v3 (2026-09-15 10:20) │
│ 新增 12 行 / 删除 3 行 / 修改 5 行     [提示：对比不含未保存修改]│
├──────────────────────────────┬───────────────────────────────┤
│ CodeEditorWidget(readOnly)   │ CodeEditorWidget(readOnly)     │
│ 旧版 v2                      │ 新版 v3                        │
│ 行背景：删除=红 修改=黄       │ 行背景：新增=绿 修改=黄         │
├──────────────────────────────┴───────────────────────────────┤
│                                    [关闭]  [复制差异]（分期）  │
└──────────────────────────────────────────────────────────────┘
```

- 两栏均为 `CodeEditorWidget`（`l_qt_wgt_lib.smart_widget`，`ui.py:40-44` 已导入）：
  `setReadOnly(True)` + `set_mode(...)`，与主编辑器同一套高亮。
- **默认必须是「源码态」模式，不能用 `markdown_preview` 作默认**：预览态会把多个源码行渲染成
  富文本块（图片/表格），行数与源码不再 1:1，行号与逐行背景会错位。因此新增小映射
  `_diff_mode_from_filename(name)`（`ui.py:1204 _mode_from_filename` 的变体）：

  | 扩展名 | 编辑器模式 | 说明 |
  | --- | --- | --- |
  | `.md` / `.mdc` / `.markdown` | `markdown` | Markdown 源码高亮（不做块渲染） |
  | `.log` | `log` | 日志高亮 |
  | `.py` | `python` | Python 高亮 |
  | 其它 | `text` | 纯文本 |

  > 后续演进（见 `openspec/changes/add-version-diff-preview-mode/`）：预览态与源码态**并列存在**。
  > 默认仍是源码态（本节契约不变），工具条可切到「预览」——预览态改为按 **Markdown 块**对齐，
  > 逐块着色、按比例同步滚动、隐藏行号；源码态的补位对齐/行号/行内字符级高亮/值镜像滚动全部保留。
  > 两种模式的取舍不同：源码态给精确核对，预览态给可读性。改动细节与决策见该 change 的 `design.md`。

- 副作用：Markdown 源码模式下超长图片 URL 会被折叠隐藏（`code_editor.py` 的
  `MarkdownCodeHighlighter`），差异里这类行只显示 URL 前 100 字符，属可接受行为。
- 行背景着色：用 `QTextEdit.ExtraSelection` + `FullWidthSelection` 给每行加 `QTextCharFormat` 背景
  （与 `code_editor.py` 当前行高亮同一手法）。配色沿用深色主题：

| 类型 | 背景 | 说明 |
| --- | --- | --- |
| 删除（仅左栏） | `#3b1f24` | 红 |
| 新增（仅右栏） | `#1f3b2b` | 绿 |
| 修改（两栏） | `#3b3520` | 黄 |
| 空占位行 | `#12161c` | 压暗 |

- 同步滚动：两栏 `verticalScrollBar().valueChanged` 互相同步，用一个 `_syncing` 布尔量
  防止递归；滚动条最大值不同时按比例同步。
- 行号：`CodeEditorWidget` 自带行号栏（固定行号）；左右两侧分别是各自文件的行号，
  占位行显示为空。差异行号的一致性靠 §3.3 的对齐保证。
- 对话框尺寸：`resize(1280, 800)`，用 `QSplitter` 让用户可拖动分配左右宽度；
  位置居中于主窗口（`move(main.center() - rect().center())`）。
- 只读、不修改任何状态：对话框不做「应用/回滚」，避免误操作。

### 3.5 触发与生命周期

- `btn_diff.clicked` → `MainWindow._on_diff_clicked()`
- `_on_diff_clicked` 逻辑：

```python
def _on_diff_clicked(self) -> None:
    left_id = self.combo_diff_left.itemData(self.combo_diff_left.currentIndex())
    right_id = self.combo_diff_right.itemData(self.combo_diff_right.currentIndex())
    if not left_id or not right_id or left_id == right_id:
        self.status.showMessage("请选择两个不同的版本再对比", 3000)
        return
    left_ver = history_store.get_version(int(left_id))
    right_ver = history_store.get_version(int(right_id))
    if left_ver is None or right_ver is None:  # 版本已被 100 条上限修剪
        self.status.showMessage("所选版本已不存在，已刷新版本列表", 4000)
        self._populate_version_combo()
        return
    name = left_ver["title"] or right_ver["title"] or "当前内容"
    # 两栏标题直接用下拉框的 item 文本（已含 vN / 时间 / 字数），避免 id 与「第几版」混淆
    dlg = VersionDiffDialog(
        name=name,
        left_label=self.combo_diff_left.currentText(),
        right_label=self.combo_diff_right.currentText(),
        left_text=left_ver["content"],
        right_text=right_ver["content"],
        mode=self._diff_mode_from_filename(name),
        dirty_hint=bool(self.state.dirty),
        parent=self,
    )
    dlg.exec()
```

- 对话框按需创建、`exec()` 模态；不缓存（版本内容小，打开即算），避免内存与失效问题。

### 3.6 边界与异常

| 场景 | 处理 |
| --- | --- |
| 两侧选同一条（同 id） | 按钮禁用 / 提示「两侧版本相同」 |
| 内容完全一致 | 对话框正常打开，统计全 0，顶部提示「两个版本内容一致」 |
| `get_version` 返回 None（被 100 条上限修剪） | 提示 + 刷新下拉 |
| 超长内容 | 单侧 > 20000 行或 > 2MB：只对比前 20000 行并在顶部提示「内容过长，仅显示前 20000 行差异」 |
| 当前编辑器有未保存修改 | 对话框顶部固定提示「对比的是历史版本，不含当前未保存修改」；不参与对比 |
| 版本 id 为 None（"（无上一版）" 占位） | 按钮禁用 |

### 3.7 验收标准

1. 打开有 ≥3 个版本的笔记：对比下拉默认 = 最新 vs 上一版，点「对比」弹出双栏，左栏为上一版、右栏为最新，新增行绿底、删除行红底。
2. 手动把左侧换成 v1、右侧换成最新，统计数字随之变化。
3. 仅 1 个版本时：「对比」按钮禁用，提示明确。
4. 切到另一条笔记：两个下拉重置为「最新 vs 上一版」。
5. 对照切换版本（`combo_version`）与对比下拉互不影响。
6. 长日志（> 1 万行）打开不卡死（< 1.5s）。

---

## 4. 需求 B：修复「重载不生成新版本」

### 4.1 复现路径（问题现象）

1. 打开一个外部 `.md` / `.log` 文件（或笔记 / IPC 文件）—— 打开时写入基线版本（`ui.py:3270`），
   版本下拉显示「最新」= 这个基线版本。
2. 用**别的程序**修改磁盘上的该文件。
3. 回到本程序：文件监视/轮询/窗口激活触发 `_check_local_file_changed`（`ui.py:4895`），
   弹出「文件『x.md』已在磁盘上被外部修改，是否重载？」
4. 点「是」→ `_reload_local_file_from_disk`（`ui.py:5004`）：读盘 → 刷新编辑器 → `dirty=False`
   → 记录快照 → `_sync_version_combo_on_open()`（`ui.py:5052`）。
5. **结果**：编辑器里是新内容，但**版本历史里没有这条新内容**，版本下拉的「最新」仍是旧内容；
   想对比「改动前 / 改动后」时，改动后的内容根本不在版本库里。
   点「重载」多次也不会生成任何新版本。

### 4.2 根因

`_reload_local_file_from_disk` 只做了「编辑器 + 磁盘快照」的更新
（`ui.py:5042-5054`：`state.dirty=False` / `_record_loaded_local_file` /
`_set_code_editor_status_file` / `_sync_version_combo_on_open`），
**从未调用 `_record_version`**；而 `_sync_version_combo_on_open`（`ui.py:2658-2675`）只是把
`_current_version_id` 指向库里已有的最新 id，本身不写库。

即：版本库的写入路径只挂在「保存 / 打开」，漏掉了「外部修改后重载」这条内容变更路径。

### 4.3 修复方案

在 `_reload_local_file_from_disk` 成功把磁盘内容落进编辑器之后、`_sync_version_combo_on_open()`
之前，补一次版本记录（`ui.py:5044` 附近）：

```python
self.state.dirty = False
self._invalidate_log_content_cache(str(path))
self._record_loaded_local_file(path)
self._set_code_editor_status_file(path)
...
self._update_title()
# 新增：重载后的磁盘内容成为版本历史的新版本（内容未变时 add_version 自动去重）
kind, ref, title = self._current_version_context()
if kind and ref:
    self._record_version(kind, ref, title, text)
self._sync_version_combo_on_open()
self._restore_editor_scroll_ratio(editor, scroll_ratio)
```

要点：

1. **记录的内容用局部变量 `text`（`ui.py:5019` 刚从磁盘读出的全文）**，不要用
   `_get_content_text()`（`ui.py:5289` 返回 `content_edit.toPlainText()`）——markdown 预览态下
   编辑器主体内容并非权威来源，`text` 才是这次重载的实际结果。
2. **必须放在 `_record_loaded_local_file` 之后**：万一 `add_version` 抛错（已 try/except 兜底，
   `ui.py:2588-2593`），也不影响磁盘快照，避免反复弹「外部修改」提示。
3. **必须放在 `_sync_version_combo_on_open()` 之前**：这样下拉框立刻能看到新版本并被选中为「最新」。
4. `kind/ref/title` 复用 `_current_version_context()`：笔记=`note`、外部/IPC 文件=`external`、
   服务器日志=`log`，与保存路径语义完全一致。
5. `add_version` 自带内容去重（`history_store.py:72-73`），因此：
   - 用户重复点重载、或重载后内容与上一版本相同时，**不会**产生重复版本；
   - 只有磁盘内容真的变了才会新增一条。
6. 用户点「否」（保留当前内容）时不写版本，保持现状（`ui.py:4956-4960`）。

### 4.4 副作用与风险

| 项 | 评估 |
| --- | --- |
| 版本数量增长 | 每次磁盘改动消耗 1 个槽位；上限 100 自动修剪最旧，可接受 |
| 与保存路径重复写 | `add_version` 去重，安全 |
| 「重载」语义变化 | 「重载」现在等于「把磁盘内容收进历史」，版本下拉会新增一条并选中，属于用户期望的修复方向 |
| 编码兜底 | 读取已用 `errors="replace"`（`ui.py:5019`），不会因编码异常中断 |
| 剪贴板/IPC 推入 | IPC 文件走同一路径，同样会被记录（kind=`external`） |

### 4.5 验收标准

1. 打开外部文件（生成 v1）→ 外部改文件 → 弹窗点「是」→ 版本下拉出现 v2（内容=磁盘新内容），
   且 `combo_version` 默认选中 v2。
2. 磁盘内容未再变化时重复点重载 → 不新增版本（去重生效）。
3. 弹窗点「否」→ 不新增版本，编辑器内容保持用户当前内容。
4. 笔记（`kind=note`）与外部文件（`kind=external`）重载后，版本记录分别落在自己的 ref 上，
   互不串味；打开另一条笔记看到的版本列表不受影响。
5. markdown 预览态下重载 → 版本内容 == 磁盘全文（与预览可见内容一致，不丢内容）。

---

## 5. 改动清单

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `ui.py` | 模块常量（`639` 前） | 新增 `_VERSION_COMBO_QSS`，`combo_version` 的内联样式改为引用它 |
| `ui.py` | `639-658` | `version_row` 新增 `label_diff` / `combo_diff_left` / `label_diff_arrow` / `combo_diff_right` / `btn_diff` |
| `ui.py` | `783-806` | `bind_to()` 的 `mapping` 增加 5 个新控件 |
| `ui.py` | 约 `3225-3227` | 面板可见性分支里同步处理对比控件 |
| `ui.py` | `2595-2644` | `_populate_version_combo` 内同步填充两个对比下拉并设默认值（复用同一次 `list_versions`） |
| `ui.py` | `2658-2675` | `_sync_version_combo_on_open` 末尾复位对比下拉为默认 |
| `ui.py` | 新增方法 | `_on_diff_clicked()`（触发对话框，见 §3.5） |
| `ui.py` | `1204` 附近 | 新增 `_diff_mode_from_filename()`：Markdown 用 `markdown` 源码态而非 `markdown_preview` |
| `ui.py` | `3096-3097` 附近 | `btn_diff.clicked.connect(self._on_diff_clicked)` |
| `ui.py` | `5044` 附近 | 修复 B：重载后 `_record_version(kind, ref, title, text)` |
| `version_diff_dialog.py` | 新增 | `DiffLine` / `build_side_by_side` / `diff_stats` / `VersionDiffDialog` |
| `doc/CHANGELOG.md` | 顶部 | 记录本次功能与修复 |

不改动：`history_store.py`（现有 API 足够）、服务端（版本仍是纯本地能力）。

---

## 6. 测试计划

- 手工用例：§3.7（6 条）+ §4.5（5 条）。
- 脚本用例（放 `l_notepad_client/999.0/test_*.py`，参照现有
  `test_note_context_and_external_change.py` 的做法，跳过真实 UI 交互）：
  1. `build_side_by_side` 纯函数：相同内容全 `equal`；纯新增/纯删除/替换/行数不等的对齐；
     断言左右行数相等、行号连续、`diff_stats` 数值正确。
  2. `history_store.add_version` 去重与 100 条修剪（已有逻辑，回归覆盖）。
  3. 重载写版本：模拟 `_reload_local_file_from_disk` 的最小依赖（临时文件 + 桩编辑器），
     断言版本库新增一条且内容 == 磁盘内容；再跑一次断言不新增。

---

## 7. 待确认与后续

1. 「复制差异」按钮（把差异文本写入剪贴板/收藏夹）是否本期需要？
2. 是否需要「用左版/右版覆盖编辑器」（回滚一键化）？当前设计不含（需二次确认弹窗）。
3. 是否需要「忽略空白/大小写」开关，以及行内字符级高亮（阶段 2）。
4. 对比基准下拉是否要提供「当前编辑内容（未保存）」这一虚拟项，用于「未保存修改 vs 最新版本」。
5. 版本上限 100 是否偏高（每条存全文）？如需调小/改为仅存差异，属于独立的存储优化议题。
