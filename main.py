"""DINGDING Minecraft Launcher — local versions and local instances."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QInputDialog, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

import launcher_settings
from launcher_auth import login_microsoft as authenticate_microsoft
from launcher_display import apply_borderless_when_ready, launcher_monitor_bounds, prepare_window_options
from launcher_core import (
    Account, JavaInfo, LauncherError, build_launch_plan, cleanup_natives,
    extract_natives, find_java, find_minecraft_versions, find_modpack_instances,
    find_local_modpack_versions, resolve_local_modpack, build_version_entries, detect_version_type, VERSION_TYPE_LABELS,
    get_library_jars, get_offline_uuid, get_required_java_version, inspect_java,
    load_config as read_config, load_version, prepare_legacy_assets, redact_command, resolve_instance,
    save_config as write_config, select_best_java, validate_player_name,
)

CONFIG_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "DINGDINGLauncher"
CONFIG_FILE = CONFIG_DIR / "config.json"


class TaskSignals(QObject):
    finished = Signal(str, object)
    failed = Signal(str, str)
    log = Signal(str)
    game_exited = Signal(int)
    game_window_ready = Signal(int, bool)


class LauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.config = read_config(CONFIG_FILE)
        default_dir = Path(os.getenv("APPDATA", str(Path.home()))) / ".minecraft"
        self.minecraft_dir = Path(self.config["minecraft_dir"] or default_dir).resolve()
        self.account_type = self.config["account_type"]
        self.microsoft_account = None
        self.java_infos = []
        self._preferred_java_path = self.config["java_path"]
        self.current_spec = None
        self.current_game_directory = self.minecraft_dir
        self.current_instance = None
        self._scanned_versions, self._scanned_instances = [], []
        self.game_process = None
        self._running_version = None
        self._game_thread = None
        self._game_log_lines = deque(maxlen=5000)
        self._game_log_lock = threading.Lock()
        self._jobs = {}
        self._selection_revision = 0
        self._closing = False
        self._initializing = True
        self.signals = TaskSignals(self)
        self.signals.finished.connect(self._job_finished)
        self.signals.failed.connect(self._job_failed)
        self.signals.log.connect(self.append_log)
        self.signals.game_exited.connect(self._game_exited)
        self.signals.game_window_ready.connect(self._game_window_ready)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(400)
        self.save_timer.timeout.connect(self.save_config)
        self.setWindowTitle("DINGDING Minecraft Launcher")
        self.resize(1000, 900)
        self.init_ui()
        self.log_timer = QTimer(self)
        self.log_timer.setInterval(100)
        self.log_timer.timeout.connect(self._flush_game_log)
        self.log_timer.start()
        self._initializing = False
        self.scan_java()
        self.scan_versions()

    def init_ui(self):
        layout = QVBoxLayout(self)
        title = QLabel("DINGDING Minecraft Launcher")
        title.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(title)

        layout.addWidget(QLabel("Java 环境"))
        self.java_status = QLabel("正在检测 Java…")
        layout.addWidget(self.java_status)
        java_row = QHBoxLayout()
        self.java_combo = QComboBox()
        self.java_combo.setMinimumWidth(300)
        self.java_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.java_combo.currentIndexChanged.connect(self.java_selection_changed)
        self.java_combo.activated.connect(self._manual_java_selected)
        java_row.addWidget(self.java_combo, 1)
        self.java_scan_button = QPushButton("扫描 Java")
        self.java_scan_button.clicked.connect(self.scan_java)
        java_row.addWidget(self.java_scan_button)
        self.java_auto_check = QCheckBox("自动匹配")
        self.java_auto_check.setChecked(self.config["java_auto"])
        self.java_auto_check.setToolTip("切换游戏版本时重新匹配 Java；手动选择会关闭自动匹配。")
        self.java_auto_check.toggled.connect(self._java_auto_changed)
        java_row.addWidget(self.java_auto_check)
        self.java_browse_button = QPushButton("手动选择")
        self.java_browse_button.clicked.connect(self.choose_java)
        java_row.addWidget(self.java_browse_button)
        layout.addLayout(java_row)

        layout.addWidget(QLabel("Minecraft 游戏目录"))
        directory_row = QHBoxLayout()
        self.minecraft_path_label = QLabel(str(self.minecraft_dir))
        self.minecraft_path_label.setWordWrap(True)
        directory_row.addWidget(self.minecraft_path_label, 1)
        self.directory_button = QPushButton("选择游戏目录")
        self.directory_button.clicked.connect(self.choose_minecraft_directory)
        directory_row.addWidget(self.directory_button)
        layout.addLayout(directory_row)

        layout.addWidget(QLabel("Minecraft 版本 / 本地整合包"))
        version_row = QHBoxLayout()
        self.version_combo = QComboBox()
        self.version_combo.currentIndexChanged.connect(self.version_selected)
        version_row.addWidget(self.version_combo, 1)
        self.version_scan_button = QPushButton("扫描版本")
        self.version_scan_button.clicked.connect(self.scan_versions)
        version_row.addWidget(self.version_scan_button)
        self.modpack_button = QPushButton("添加本地整合包")
        self.modpack_button.setToolTip("选择已解压且已安装游戏文件的整合包目录；直接使用原有 Mod、配置与存档。")
        self.modpack_button.clicked.connect(self.choose_modpack_directory)
        version_row.addWidget(self.modpack_button)
        layout.addLayout(version_row)

        account_row = QHBoxLayout()
        account_row.addWidget(QLabel("账户类型："))
        self.account_combo = QComboBox()
        self.account_combo.addItem("离线账户", "offline")
        self.account_combo.addItem("Microsoft 账户", "microsoft")
        self.account_combo.setCurrentIndex(self.account_combo.findData(self.account_type))
        account_row.addWidget(self.account_combo)
        account_row.addWidget(QLabel("游戏昵称："))
        self.player_name = QLineEdit(self.config["offline_name"])
        self.player_name.setMaxLength(16)
        self.player_name.textEdited.connect(self.offline_name_changed)
        account_row.addWidget(self.player_name, 1)
        self.login_button = QPushButton("登录 Microsoft")
        self.login_button.clicked.connect(self.login_microsoft)
        account_row.addWidget(self.login_button)
        layout.addLayout(account_row)
        self.account_combo.currentIndexChanged.connect(self.account_type_changed)

        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("最大内存："))
        self.memory_spin = QSpinBox()
        self.memory_spin.setRange(512, 32768)
        self.memory_spin.setSingleStep(512)
        self.memory_spin.setSuffix(" MB")
        self.memory_spin.setValue(self.config["memory"])
        self.memory_spin.valueChanged.connect(lambda value: self._setting_changed("memory", value))
        settings_row.addWidget(self.memory_spin)
        settings_row.addWidget(QLabel("窗口："))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(320, 7680)
        self.width_spin.setValue(self.config["game_width"])
        self.width_spin.valueChanged.connect(lambda value: self._setting_changed("game_width", value))
        settings_row.addWidget(self.width_spin)
        settings_row.addWidget(QLabel("×"))
        self.height_spin = QSpinBox()
        self.height_spin.setRange(240, 4320)
        self.height_spin.setValue(self.config["game_height"])
        self.height_spin.valueChanged.connect(lambda value: self._setting_changed("game_height", value))
        settings_row.addWidget(self.height_spin)
        self.fullscreen_check = QCheckBox("全屏")
        self.fullscreen_check.setToolTip("Windows 使用无边框全屏，不切换桌面分辨率和刷新率；其他系统使用游戏原生全屏。")
        self.fullscreen_check.setChecked(self.config["fullscreen"])
        self.fullscreen_check.toggled.connect(self._fullscreen_changed)
        settings_row.addWidget(self.fullscreen_check)
        layout.addLayout(settings_row)

        self.version_status = QLabel("正在扫描本地版本…")
        self.version_status.setWordWrap(True)
        layout.addWidget(self.version_status)
        self.launch_button = QPushButton("启动 Minecraft")
        self.launch_button.setStyleSheet("font-size: 18px; font-weight: bold; padding: 10px;")
        self.launch_button.clicked.connect(self.launch_minecraft)
        layout.addWidget(self.launch_button)
        layout.addWidget(QLabel("版本信息"))
        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        layout.addWidget(self.info_text, 1)
        layout.addWidget(QLabel("启动日志"))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setUndoRedoEnabled(False)
        self.log_text.document().setMaximumBlockCount(5000)
        layout.addWidget(self.log_text, 2)
        self.account_type_changed(self.account_combo.currentIndex())
        self._fullscreen_changed(self.config["fullscreen"])

    def _run_job(self, name, operation, on_success):
        if name in self._jobs or self._closing:
            return False
        self._jobs[name] = on_success
        self._update_actions()

        def run():
            try:
                result = operation()
            except Exception as exc:
                if not self._closing:
                    self.signals.failed.emit(name, str(exc))
            else:
                if not self._closing:
                    self.signals.finished.emit(name, result)

        threading.Thread(target=run, name=f"launcher-{name}", daemon=True).start()
        return True

    def _job_finished(self, name, result):
        callback = self._jobs.pop(name, None)
        if callback and not self._closing:
            try:
                callback(result)
            except Exception as exc:
                self.append_log(f"处理结果失败：{exc}")
                self.version_status.setText(str(exc))
        self._update_actions()

    def _job_failed(self, name, error):
        self._jobs.pop(name, None)
        self.append_log(error)
        if name.startswith("selection-"):
            if name == f"selection-{self._selection_revision}":
                self.version_status.setText(error)
        elif name == "java":
            self.java_status.setText("Java 扫描失败，可尝试手动选择。")
        elif name == "launch":
            QMessageBox.warning(self, "无法启动", error)
            self.version_status.setText("启动准备失败，请查看日志。")
        elif name == "login":
            QMessageBox.warning(self, "登录未完成", error)
        elif name == "modpack":
            QMessageBox.warning(self, "无法添加整合包", error)
        else:
            self.version_status.setText(error)
        self._update_actions()

    def _update_actions(self):
        if not hasattr(self, "launch_button"):
            return
        running = self.game_process is not None
        preparing = "launch" in self._jobs
        self.java_scan_button.setEnabled("java" not in self._jobs and not preparing)
        self.java_browse_button.setEnabled("java" not in self._jobs and not preparing)
        self.java_combo.setEnabled(not preparing)
        self.java_auto_check.setEnabled(not preparing)
        pack_busy = "modpack" in self._jobs
        self.version_scan_button.setEnabled("versions" not in self._jobs and not preparing and not pack_busy)
        self.directory_button.setEnabled(not preparing and "versions" not in self._jobs and "java" not in self._jobs and not pack_busy)
        self.version_combo.setEnabled("versions" not in self._jobs and not preparing and not pack_busy)
        self.modpack_button.setEnabled(not preparing and not running and not pack_busy and "versions" not in self._jobs)
        self.account_combo.setEnabled("login" not in self._jobs and not preparing)
        self.player_name.setEnabled(self.account_type == "offline" and not preparing)
        self.login_button.setEnabled(self.account_type == "microsoft" and "login" not in self._jobs and not preparing)
        self.launch_button.setEnabled(bool(self.current_spec) and self.java_combo.currentIndex() >= 0
                                      and not preparing and not running and "login" not in self._jobs
                                      and "java" not in self._jobs and "versions" not in self._jobs and not pack_busy)
        self.launch_button.setText("Minecraft 正在运行" if running else "正在准备启动…" if preparing else "启动 Minecraft")
        self.login_button.setText("正在登录…" if "login" in self._jobs else
                                  f"已登录：{self.microsoft_account.name}" if self.microsoft_account else "登录 Microsoft")

    def _schedule_save(self):
        if not self._initializing:
            self.save_timer.start()

    def save_config(self):
        self.save_timer.stop()
        try:
            write_config(self.config, CONFIG_FILE)
        except OSError as exc:
            self.append_log(f"配置保存失败：{exc}")
            return False
        return True

    def _setting_changed(self, key, value):
        self.config[key] = value
        if key == "memory" and not self._initializing:
            self.auto_select_java()
        self._schedule_save()

    def _fullscreen_changed(self, checked):
        self.width_spin.setEnabled(not checked)
        self.height_spin.setEnabled(not checked)
        self._setting_changed("fullscreen", checked)

    def java_selection_changed(self, index):
        info = self.java_combo.itemData(index)
        if not isinstance(info, JavaInfo):
            return
        self.config["java_path"] = str(info.path)
        self.java_status.setText(f"当前 Java {info.major}（{info.architecture or '架构未知'}）")
        self.java_combo.setToolTip(str(info.path))
        self._schedule_save()
        self._update_actions()

    def _manual_java_selected(self, index):
        info = self.java_combo.itemData(index)
        if isinstance(info, JavaInfo):
            self._preferred_java_path = str(info.path)
            self.java_auto_check.setChecked(False)

    def _java_auto_changed(self, enabled):
        self.config["java_auto"] = enabled
        self._schedule_save()
        if enabled:
            self.auto_select_java()

    def offline_name_changed(self, text):
        if self.account_type != "offline":
            return
        try:
            name = validate_player_name(text)
        except LauncherError as exc:
            self.player_name.setToolTip(str(exc))
            self.player_name.setStyleSheet("border: 1px solid #c33;")
            return
        self.player_name.setStyleSheet("")
        self.player_name.setToolTip(f"离线 UUID：{get_offline_uuid(name)}")
        self.config["offline_name"] = name
        self.config["selected_account"] = get_offline_uuid(name)
        self._schedule_save()

    def account_type_changed(self, index):
        self.account_type = self.account_combo.itemData(index) or "offline"
        self.config["account_type"] = self.account_type
        if self.account_type == "offline":
            self.player_name.setText(self.config["offline_name"])
            self.player_name.setStyleSheet("")
            self.config["selected_account"] = get_offline_uuid(self.config["offline_name"])
        else:
            self.player_name.setText(self.microsoft_account.name if self.microsoft_account else "尚未登录")
            self.player_name.setStyleSheet("")
            self.config["selected_account"] = self.microsoft_account.uuid if self.microsoft_account else ""
        self._schedule_save()
        self._update_actions()

    def append_log(self, text):
        if self._closing:
            return
        text = str(text)
        if self.microsoft_account:
            text = text.replace(self.microsoft_account.access_token, "<已隐藏>")
        scrollbar = self.log_text.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        cursor = QTextCursor(self.log_text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n")
        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def scan_java(self):
        saved, root = self.config["java_path"], self.minecraft_dir
        self.java_status.setText("正在扫描 Java，可继续操作…")
        self._run_job("java", lambda: find_java(root, saved), self._java_scanned)

    def _java_scanned(self, infos):
        self.java_infos = infos
        saved = self._preferred_java_path
        self.java_combo.blockSignals(True)
        self.java_combo.clear()
        for info in infos:
            self.java_combo.addItem(f"Java {info.major} / {info.architecture or '?'} | {info.path}", info)
        saved_index = next((i for i, info in enumerate(infos)
                            if os.path.normcase(str(info.path)) == os.path.normcase(saved)), -1)
        if saved_index >= 0:
            self.java_combo.setCurrentIndex(saved_index)
        self.java_combo.blockSignals(False)
        if infos:
            self.java_selection_changed(self.java_combo.currentIndex())
            self.auto_select_java()
        else:
            self.java_status.setText("没有找到可用 Java，请手动选择或先安装 Java。")
        self.append_log(f"Java 扫描完成：找到 {len(infos)} 个可用环境。")

    def choose_java(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Java 可执行文件", "", "Java (java.exe java);;所有文件 (*)")
        if path:
            self._run_job("java", lambda: inspect_java(Path(path)), self._java_added)

    def _java_added(self, info):
        infos = [item for item in self.java_infos if item.path != info.path] + [info]
        self.config["java_path"] = str(info.path)
        self._preferred_java_path = str(info.path)
        self.java_auto_check.setChecked(False)
        self._java_scanned(infos)

    def auto_select_java(self):
        if not self.current_spec:
            return
        required = get_required_java_version(self.current_spec.name, self.current_spec.data)
        current = self.java_combo.currentData()
        if not self.config["java_auto"]:
            if current and required and current.major != required:
                self.java_status.setText(f"手动选择 Java {current.major}；当前游戏建议 Java {required}。")
            return
        if required is None:
            self.java_status.setText("无法判断该游戏所需 Java，请取消自动匹配后手动选择。")
            self.java_combo.setCurrentIndex(-1)
            self._update_actions()
            return
        chosen = select_best_java(self.java_infos, required, self.config["memory"], self._preferred_java_path)
        if chosen:
            self.java_combo.setCurrentIndex(self.java_infos.index(chosen))
            suffix = "" if chosen.major == required else "；高于建议版本，如不兼容请安装匹配版本"
            self.java_status.setText(f"已自动选择 Java {chosen.major}{suffix}")
        elif required:
            self.java_combo.setCurrentIndex(-1)
            self.java_status.setText(f"未找到满足版本、架构与内存要求的 Java {required}+，请扫描或安装。")
            self._update_actions()

    def choose_minecraft_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "选择 Minecraft 游戏目录", str(self.minecraft_dir))
        if not directory:
            return
        self.minecraft_dir = Path(directory).resolve()
        self._selection_revision += 1
        self.current_spec = None
        self.minecraft_path_label.setText(str(self.minecraft_dir))
        self.config["minecraft_dir"] = str(self.minecraft_dir)
        self.config["selected_version"] = ""
        self.config["selected_modpack"] = ""
        self._schedule_save()
        self.scan_versions()
        self.scan_java()

    def scan_versions(self):
        if "versions" in self._jobs:
            return
        root = self.minecraft_dir
        self.current_spec = None
        self._selection_revision += 1
        self.version_status.setText("正在扫描本地版本…")
        packs, selected_pack = list(self.config["local_modpacks"]), self.config["selected_modpack"]

        def scan():
            versions, instances = find_minecraft_versions(root), find_modpack_instances(root)
            entries = build_version_entries(root, versions, instances, packs, selected_pack)
            return versions, instances, entries

        self._run_job("versions", scan, self._versions_scanned)

    def _versions_scanned(self, result):
        versions, instances, entries = result
        self._scanned_versions, self._scanned_instances = versions, instances
        saved = self.config["selected_version"]
        saved_pack = self.config["selected_modpack"]
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        previous_category = None
        for entry in entries:
            item = entry["data"]
            if previous_category is not None and item["category"] != previous_category:
                self.version_combo.insertSeparator(self.version_combo.count())
            previous_category = item["category"]
            self.version_combo.addItem(entry["label"], item)
            self.version_combo.setItemData(self.version_combo.count() - 1,
                f"类型：{VERSION_TYPE_LABELS[item['category']]}\n游戏目录：{entry['game_dir']}", Qt.ItemDataRole.ToolTipRole)
        index = next((i for i in range(self.version_combo.count())
                      if isinstance(self.version_combo.itemData(i), dict)
                      and self.version_combo.itemData(i).get("type") == "local_modpack"
                      and os.path.normcase(self.version_combo.itemData(i)["path"]) == os.path.normcase(saved_pack)), -1) if saved_pack else self.version_combo.findText(saved)
        if index < 0 and not saved_pack:
            migrated = next((entry for entry in entries if saved in entry["aliases"]), None)
            if migrated:
                index = self.version_combo.findText(migrated["label"])
        self.version_combo.setCurrentIndex(index if index >= 0 else 0 if self.version_combo.count() else -1)
        self.version_combo.blockSignals(False)
        if self.version_combo.currentIndex() >= 0:
            self.version_selected(self.version_combo.currentIndex())
        else:
            self.info_text.clear()
            self.version_status.setText("没有找到已安装版本，请选择包含 versions 的游戏目录。")

    def version_selected(self, index):
        item = self.version_combo.itemData(index)
        if not isinstance(item, dict):
            self.current_spec = None
            self._update_actions()
            return
        self.current_spec = None
        self._selection_revision += 1
        revision = self._selection_revision
        root, name = self.minecraft_dir, item["name"]
        self.current_instance = item.get("path", name) if item["type"] != "version" else None
        self.config["selected_version"] = self.version_combo.currentText()
        self.config["selected_modpack"] = item.get("path", "")
        self._schedule_save()
        self.version_status.setText("正在检查版本和依赖…")
        self.info_text.clear()

        def prepare():
            if item["type"] == "local_modpack":
                spec, directory = resolve_local_modpack(Path(item["path"]), root, item["version"])
            elif item["type"] == "modpack":
                spec, directory = resolve_instance(root, name)
            else:
                spec, directory = load_version(root, name), root
                isolated = spec.json_path.parent
                if any((isolated / marker).is_dir() for marker in ("mods", "config", "saves")):
                    # Existing installed packs also appear in the normal version
                    # list. Honor their isolation instead of using root/mods.
                    spec, directory = resolve_local_modpack(isolated, root, name)
            return spec, directory, get_library_jars(spec), detect_version_type(spec, directory, item["type"] != "version")

        def selected(result):
            if revision != self._selection_revision:
                return
            spec, directory, libraries, category = result
            self.current_spec, self.current_game_directory = spec, directory
            required = get_required_java_version(spec.name, spec.data)
            self.info_text.setPlainText(
                f"类型：{VERSION_TYPE_LABELS[category]}\n游戏版本：{spec.name}\n游戏目录：{directory}\n\nJSON：{spec.json_path}\n"
                f"游戏 JAR：{spec.jar_path}\n主类：{spec.data.get('mainClass', '未提供')}\n"
                f"建议 Java：{required or '无法判断，请手动选择'}\n\n"
                f"普通 JAR：{len(libraries['normal'])}  Native：{len(libraries['native'])}  "
                f"缺失依赖：{len(libraries['missing'])}")
            missing_jar = not spec.jar_path.is_file()
            self.version_status.setText("游戏文件不完整，启动时会显示缺失详情。" if missing_jar or libraries["missing"] else "版本已就绪。")
            self.auto_select_java()

        self._run_job(f"selection-{revision}", prepare, selected)

    def choose_modpack_directory(self):
        if self.game_process is not None or "launch" in self._jobs or "modpack" in self._jobs:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择已解压的整合包文件夹",
            self.config["selected_modpack"] or str(self.minecraft_dir))
        if not directory:
            return
        source, shared_root = Path(directory).resolve(), self.minecraft_dir

        def added(candidates):
            names = [spec.name for spec, _ in candidates]
            selected, accepted = QInputDialog.getItem(self, "确认整合包游戏版本",
                "请选择此整合包对应的已安装版本（需匹配 Minecraft 和 Mod 加载器）：",
                names, 0, False)
            if not accepted:
                return
            spec, _ = next(candidate for candidate in candidates if candidate[0].name == selected)
            path = str(source)
            self.config["local_modpacks"] = [pack for pack in self.config["local_modpacks"]
                if os.path.normcase(pack["path"]) != os.path.normcase(path)] + [{"path": path, "version": spec.name}]
            self.config["selected_modpack"] = path
            self.append_log(f"已添加本地整合包：{source}\n关联版本：{spec.name}（文件保留在原目录）")
            self._schedule_save()
            self.scan_versions()

        self._run_job("modpack", lambda: find_local_modpack_versions(source, shared_root), added)

    def login_microsoft(self):
        client_id = launcher_settings.MICROSOFT_CLIENT_ID.strip()
        if not client_id:
            QMessageBox.information(self, "Microsoft 登录暂不可用", "此启动器尚未配置 Microsoft 登录，请联系启动器开发者。")
            return
        self.microsoft_account = None
        self._run_job("login", lambda: authenticate_microsoft(client_id, self.signals.log.emit), self._logged_in)

    def _logged_in(self, account):
        self.microsoft_account = account
        self.config["selected_account"] = account.uuid
        self.account_combo.setCurrentIndex(self.account_combo.findData("microsoft"))
        self.account_type_changed(self.account_combo.currentIndex())
        self.append_log(f"Microsoft 登录成功：{account.name}")
        self._schedule_save()

    def launch_minecraft(self):
        if any(name in self._jobs for name in ("launch", "versions", "java", "login", "modpack")) or self.game_process is not None or not self.current_spec:
            return
        self.auto_select_java()
        java = self.java_combo.currentData()
        if not isinstance(java, JavaInfo):
            return
        try:
            if self.account_type == "offline":
                name = validate_player_name(self.player_name.text())
                account = Account(name, get_offline_uuid(name))
            else:
                account = self.microsoft_account
                if account is None:
                    raise LauncherError("请先登录 Microsoft。")
                if account.expires_at <= time.time() + 60:
                    self.microsoft_account = None
                    self._update_actions()
                    raise LauncherError("Microsoft 登录已过期，请重新登录。")
        except LauncherError as exc:
            QMessageBox.warning(self, "无法启动", str(exc))
            return
        config, spec, game_dir = self.config.copy(), self.current_spec, self.current_game_directory
        bounds = launcher_monitor_bounds(int(self.winId())) if config["fullscreen"] and os.name == "nt" else None
        self.save_config()
        self.append_log(f"\n正在准备 Minecraft {spec.name}…")

        def prepare():
            plan = build_launch_plan(spec, java, game_dir, config, account,
                                     client_id=launcher_settings.MICROSOFT_CLIENT_ID, display_bounds=bounds)
            options = None
            try:
                plan.game_directory.mkdir(parents=True, exist_ok=True)
                prepare_legacy_assets(plan.asset_files)
                extract_natives(plan.native_jars, plan.natives_directory)
                if plan.window_mode != "fullscreen":
                    options = prepare_window_options(plan.game_directory)
            except Exception:
                if options:
                    options.restore()
                cleanup_natives(plan.natives_directory, plan.game_directory)
                raise
            return plan, account, options

        self._run_job("launch", prepare, self._start_game)

    def _start_game(self, result):
        plan, account = result[:2]
        options = result[2] if len(result) > 2 else None
        try:
            process = subprocess.Popen(plan.command, cwd=plan.game_directory,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       encoding="utf-8", errors="replace", bufsize=1,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as exc:
            if options:
                try:
                    options.restore()
                except (OSError, UnicodeError) as restore_error:
                    self.append_log(f"恢复游戏窗口设置失败：{restore_error}")
            cleanup_natives(plan.natives_directory, plan.game_directory)
            self.append_log(f"启动失败：{exc}")
            QMessageBox.warning(self, "启动失败", str(exc))
            return
        self.game_process = process
        self._running_version = plan.version
        self.version_status.setText(f"Minecraft {plan.version} 正在创建游戏窗口…" if plan.window_mode == "borderless"
                                    else f"Minecraft {plan.version} 正在运行。")
        self.append_log(f"启动命令：\n{redact_command(plan.command, [account.access_token], compact=True)}\n"
                        f"Minecraft 进程已创建（PID {process.pid}）。")
        self._game_thread = threading.Thread(target=self.read_minecraft_output, args=(process, plan, account.access_token, options),
                                             name="minecraft-output", daemon=False)
        self._game_thread.start()
        if plan.window_mode == "borderless":
            threading.Thread(target=self._prepare_borderless, args=(process, plan.display_bounds),
                             name="minecraft-window", daemon=True).start()

    def _prepare_borderless(self, process, bounds):
        ready = False
        try:
            ready = apply_borderless_when_ready(process, bounds)
            message = "已确认游戏窗口进入无边框全屏。" if ready else "未找到游戏窗口或全屏尺寸未生效；保留当前游戏窗口。"
        except OSError as exc:
            message = f"无边框全屏设置失败，保留游戏窗口：{exc}"
        if not self._closing and process.poll() is None:
            self.signals.log.emit(message)
            self.signals.game_window_ready.emit(process.pid, ready)

    def _game_window_ready(self, pid, fullscreen):
        if self.game_process and self.game_process.pid == pid:
            mode = "无边框全屏" if fullscreen else "窗口模式"
            self.version_status.setText(f"Minecraft {self._running_version} 正在运行（{mode}）。")

    def read_minecraft_output(self, process, plan, token, options=None):
        code = None
        try:
            for line in process.stdout:
                text = line.rstrip("\r\n")
                if token and token != "0":
                    text = text.replace(token, "<已隐藏>")
                if self._closing:
                    continue
                with self._game_log_lock:
                    self._game_log_lines.append(text[:4096] + ("…<长日志已截断>" if len(text) > 4096 else ""))
            code = process.wait()
        except (OSError, ValueError) as exc:
            if not self._closing:
                self.signals.log.emit(f"读取游戏日志失败：{exc}")
            code = process.wait()
        finally:
            if process.stdout:
                process.stdout.close()
            cleanup_natives(plan.natives_directory, plan.game_directory)
            if options:
                try:
                    options.restore()
                except (OSError, UnicodeError) as exc:
                    if not self._closing:
                        self.signals.log.emit(f"恢复游戏窗口设置失败：{exc}")
        if code is not None and not self._closing:
            self.signals.game_exited.emit(code)

    def _flush_game_log(self):
        # A Forge startup burst must not monopolize the UI for thousands of lines.
        with self._game_log_lock:
            lines, size = [], 0
            while self._game_log_lines and len(lines) < 100 and size < 16384:
                line = self._game_log_lines.popleft()
                lines.append(line)
                size += len(line)
        if lines:
            self.append_log("\n".join(lines))

    def _game_exited(self, code):
        self._flush_game_log()
        self.game_process = None
        self._running_version = None
        self.append_log(f"Minecraft 已退出，退出代码：{code}")
        self.version_status.setText("游戏已正常退出。" if code == 0 else f"游戏异常退出（{code}），请查看日志。")
        self._update_actions()

    def closeEvent(self, event):
        if "launch" in self._jobs:
            event.ignore()
            self.append_log("正在准备游戏，请在准备完成后关闭启动器。")
            return
        if not self.save_config():
            answer = QMessageBox.question(self, "配置未保存", "配置保存失败，仍要关闭启动器吗？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        # The non-daemon output reader drains the game pipe after Qt exits.
        # No game process is terminated when the user closes the launcher.
        self.log_timer.stop()
        self._closing = True
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = LauncherWindow()
    window.show()
    sys.exit(app.exec())
