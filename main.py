import sys
import os
import json
import platform
import subprocess
import zipfile
import shutil
import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QComboBox,
    QFileDialog,
    QTextEdit,
    QLineEdit,
    QSpinBox,
    QMessageBox
)
from PySide6.QtCore import QObject, Signal
import msal
import requests
CURRENT_OS = "windows"
CURRENT_ARCH = platform.machine().lower()


# =========================================================
# Java
# =========================================================

def extract_natives(native_jars, natives_directory):

    natives_directory = Path(
        natives_directory
    )

    # 创建 natives 文件夹
    natives_directory.mkdir(
        parents=True,
        exist_ok=True
    )

    # 清理旧的 DLL
    for file in natives_directory.iterdir():

        if file.is_file():

            try:
                file.unlink()
            except:
                pass

    # 解压 Native JAR
    for native_jar in native_jars:

        try:

            with zipfile.ZipFile(
                native_jar,
                "r"
            ) as z:

                for item in z.infolist():

                    filename = item.filename

                    # 只提取 Windows DLL
                    if not filename.lower().endswith(
                        ".dll"
                    ):
                        continue

                    # 防止 JAR 内部目录结构导致 DLL
                    # 被解压到子文件夹
                    target = (
                        natives_directory /
                        Path(filename).name
                    )

                    with z.open(item) as source:

                        with open(
                            target,
                            "wb"
                        ) as target_file:

                            shutil.copyfileobj(
                                source,
                                target_file
                            )

        except Exception as e:

            print(
                f"Native 解压失败：{native_jar}"
            )

            print(e)
def get_java_version(java_path):
    try:
        result = subprocess.run(
            [java_path, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )

        output = result.stderr.strip()

        if not output:
            output = result.stdout.strip()

        if output:
            return output.splitlines()[0]

        return "无法获取版本"

    except Exception as e:
        return f"检测失败：{e}"


def find_java():
    java_paths = set()

    # PATH 中的 Java
    try:
        result = subprocess.run(
            ["where", "java"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        for line in result.stdout.splitlines():
            line = line.strip()

            if line and os.path.exists(line):
                java_paths.add(Path(line))

    except Exception:
        pass

    # 常见 Java 安装目录
    search_dirs = [
        Path("C:/Program Files/Java"),
        Path("C:/Program Files/Eclipse Adoptium"),
        Path("C:/Program Files/Microsoft"),
        Path("C:/Program Files/Amazon Corretto"),
        Path("C:/Program Files/Zulu"),
        Path("C:/Program Files/BellSoft"),
        Path("C:/Program Files/SapMachine"),
        Path.home() / "AppData/Local/Programs",
        Path.home() / "AppData/Roaming/.minecraft"
    ]

    for base_dir in search_dirs:

        if not base_dir.exists():
            continue

        try:

            for java_exe in base_dir.rglob("java.exe"):
                java_paths.add(java_exe)

        except Exception:
            pass

    return sorted(
        java_paths,
        key=lambda x: str(x).lower()
    )


# =========================================================
# Minecraft 版本
# =========================================================

def find_minecraft_versions(minecraft_dir):

    versions_dir = minecraft_dir / "versions"

    if not versions_dir.exists():
        return []

    versions = []

    try:

        for item in versions_dir.iterdir():

            if item.is_dir():
                versions.append(item.name)

    except Exception:
        pass

    return sorted(versions)


# =========================================================
# JSON
# =========================================================

def read_json_file(path):

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:
        return None


def load_version_json(
    minecraft_dir,
    version
):

    version_dir = (
        minecraft_dir /
        "versions" /
        version
    )

    json_file = (
        version_dir /
        f"{version}.json"
    )

    jar_file = (
        version_dir /
        f"{version}.jar"
    )

    if not json_file.exists():

        return {
            "success": False,
            "error": "没有找到版本 JSON",
            "json_file": json_file,
            "jar_file": jar_file
        }

    data = read_json_file(json_file)

    if data is None:

        return {
            "success": False,
            "error": "JSON 文件读取失败",
            "json_file": json_file,
            "jar_file": jar_file
        }

    return {
        "success": True,
        "data": data,
        "json_file": json_file,
        "jar_file": jar_file
    }


# =========================================================
# inheritsFrom
# =========================================================

def load_version_with_inheritance(
    minecraft_dir,
    version,
    visited=None
):

    if visited is None:
        visited = set()

    if version in visited:
        return None

    visited.add(version)

    result = load_version_json(
        minecraft_dir,
        version
    )

    if not result["success"]:
        return None

    data = result["data"]

    parent_version = data.get(
        "inheritsFrom"
    )

    if not parent_version:
        return data

    parent_data = load_version_with_inheritance(
        minecraft_dir,
        parent_version,
        visited
    )

    if parent_data is None:
        return data

    # Libraries
    parent_libraries = parent_data.get(
        "libraries",
        []
    )

    child_libraries = data.get(
        "libraries",
        []
    )

    data["libraries"] = (
        parent_libraries +
        child_libraries
    )

    # Arguments
    parent_arguments = parent_data.get(
        "arguments",
        {}
    )

    child_arguments = data.get(
        "arguments",
        {}
    )

    merged_arguments = {}

    for key in parent_arguments:

        merged_arguments[key] = (
            parent_arguments[key]
        )

    for key in child_arguments:

        if key in merged_arguments:

            if (
                isinstance(
                    merged_arguments[key],
                    list
                )
                and
                isinstance(
                    child_arguments[key],
                    list
                )
            ):

                merged_arguments[key] = (
                    merged_arguments[key]
                    +
                    child_arguments[key]
                )

            else:

                merged_arguments[key] = (
                    child_arguments[key]
                )

        else:

            merged_arguments[key] = (
                child_arguments[key]
            )

    data["arguments"] = merged_arguments

    # mainClass
    if (
        "mainClass" not in data
        and
        "mainClass" in parent_data
    ):

        data["mainClass"] = (
            parent_data["mainClass"]
        )

    # assets
    if (
        "assets" not in data
        and
        "assets" in parent_data
    ):

        data["assets"] = (
            parent_data["assets"]
        )

    return data


# =========================================================
# Library rules
# =========================================================

def library_allowed(library):

    rules = library.get("rules")

    if not rules:
        return True

    allowed = False

    for rule in rules:

        os_rule = rule.get("os")

        # 没有 OS 限制
        if not os_rule:

            if rule.get("action") == "allow":
                allowed = True

            elif rule.get("action") == "disallow":
                allowed = False

            continue

        os_name = os_rule.get("name")

        # Windows
        if os_name == CURRENT_OS:

            if rule.get("action") == "allow":
                allowed = True

            elif rule.get("action") == "disallow":
                allowed = False

    return allowed


# =========================================================
# Maven 路径
# =========================================================

def library_name_to_path(
    library_name
):

    parts = library_name.split(":")

    if len(parts) < 3:
        return None

    group = parts[0]
    artifact = parts[1]
    version = parts[2]

    classifier = None
    extension = "jar"

    if len(parts) >= 4:
        classifier = parts[3]

    if "@" in version:

        version, extension = (
            version.split("@", 1)
        )

    group_path = group.replace(
        ".",
        "/"
    )

    filename = (
        artifact +
        "-" +
        version
    )

    if classifier:

        filename += (
            "-" +
            classifier
        )

    filename += "." + extension

    return (
        Path(group_path) /
        artifact /
        version /
        filename
    )


# =========================================================
# 获取 Libraries
# =========================================================

def get_library_jars(
    minecraft_dir,
    data
):

    libraries = data.get(
        "libraries",
        []
    )

    library_dir = (
        minecraft_dir /
        "libraries"
    )

    normal_jars = []
    native_libraries = []
    missing_libraries = []
    skipped_libraries = []

    for library in libraries:

        name = library.get(
            "name",
            "未知 Library"
        )

        if not library_allowed(library):

            skipped_libraries.append(name)

            continue

        downloads = library.get(
            "downloads",
            {}
        )

        artifact = downloads.get(
            "artifact"
        )

        # 普通 JAR
        if artifact:

            relative_path = artifact.get(
                "path"
            )

            if relative_path:

                jar_path = (
                    library_dir /
                    relative_path
                )

                if jar_path.exists():

                    normal_jars.append(
                        jar_path
                    )

                else:

                    missing_libraries.append(
                        (
                            name,
                            jar_path
                        )
                    )

        else:

            relative_path = (
                library_name_to_path(
                    name
                )
            )

            if relative_path:

                jar_path = (
                    library_dir /
                    relative_path
                )

                if jar_path.exists():

                    normal_jars.append(
                        jar_path
                    )

                else:

                    missing_libraries.append(
                        (
                            name,
                            jar_path
                        )
                    )

        # Windows Native
        classifiers = downloads.get(
            "classifiers",
            {}
        )

        native_info = classifiers.get(
            "natives-windows"
        )

        if native_info:

            native_path = native_info.get(
                "path"
            )

            if native_path:

                native_jar = (
                    library_dir /
                    native_path
                )

                if native_jar.exists():

                    native_libraries.append(
                        native_jar
                    )

                else:

                    missing_libraries.append(
                        (
                            f"{name} (native)",
                            native_jar
                        )
                    )

    return {
        "normal": normal_jars,
        "native": native_libraries,
        "skipped": skipped_libraries,
        "missing": missing_libraries
    }


# =========================================================
# 参数处理
# =========================================================

def flatten_argument(
    argument
):

    if isinstance(argument, str):
        return [argument]

    if isinstance(argument, dict):

        value = argument.get(
            "value"
        )

        if isinstance(value, list):
            return value

        return [value]

    return []


def get_game_arguments(data):

    result = []

    arguments = data.get(
        "arguments"
    )

    if arguments:

        game_arguments = arguments.get(
            "game",
            []
        )

        for arg in game_arguments:

            # 普通字符串
            if isinstance(arg, str):

                # 暂时过滤 Quick Play
                # 我们目前做的是普通启动，
                # 不需要 Quick Play
                if arg.startswith("--quickPlay"):
                    continue

                if "${quickPlay" in arg:
                    continue

                result.append(arg)

            # 字典形式
            elif isinstance(arg, dict):

                value = arg.get(
                    "value"
                )

                if isinstance(value, str):

                    if (
                        value.startswith(
                            "--quickPlay"
                        )
                    ):
                        continue

                    if (
                        "${quickPlay" in value
                    ):
                        continue

                    result.append(value)

                elif isinstance(value, list):

                    for item in value:

                        if not isinstance(
                            item,
                            str
                        ):
                            continue

                        if item.startswith(
                            "--quickPlay"
                        ):
                            continue

                        if "${quickPlay" in item:
                            continue

                        result.append(item)

        return result

    # 老版本 Minecraft
    old_arguments = data.get(
        "minecraftArguments"
    )

    if old_arguments:

        return old_arguments.split()

    return []


def get_jvm_arguments(data):

    result = []

    arguments = data.get(
        "arguments"
    )

    if not arguments:
        return result

    jvm_arguments = arguments.get(
        "jvm",
        []
    )

    for arg in jvm_arguments:

        # 普通字符串参数
        if isinstance(arg, str):

            result.append(arg)

        # 带 rules 的参数
        elif isinstance(arg, dict):

            rules = arg.get("rules")

            # 没有 rules，直接使用
            if not rules:

                value = arg.get("value")

                if isinstance(value, list):
                    result.extend(value)
                elif value:
                    result.append(value)

                continue

            # 判断当前 Windows 是否允许
            allowed = False

            for rule in rules:

                os_rule = rule.get("os")

                # 没有 OS 限制
                if not os_rule:

                    if rule.get("action") == "allow":
                        allowed = True

                    elif rule.get("action") == "disallow":
                        allowed = False

                    continue

                os_name = os_rule.get("name")

                # 当前系统是 Windows
                if os_name == "windows":

                    if rule.get("action") == "allow":
                        allowed = True

                    elif rule.get("action") == "disallow":
                        allowed = False

            if not allowed:
                continue

            value = arg.get("value")

            if isinstance(value, list):
                result.extend(value)

            elif value:
                result.append(value)

    return result


# =========================================================
# 参数变量替换
# =========================================================

def replace_placeholders(
    value,
    variables
):

    if not isinstance(value, str):
        return value

    for key, replacement in variables.items():

        value = value.replace(
            "${" + key + "}",
            str(replacement)
        )

    return value


# =========================================================
# 主窗口
# =========================================================
class LogEmitter(QObject):
    log_signal = Signal(str)
CLIENT_ID = ""

AUTHORITY = "https://login.microsoftonline.com/consumers"

REDIRECT_URI = "http://localhost"

SCOPES = [
    "XboxLive.signin",
    "XboxLive.offline_access"
]
class LauncherWindow(QWidget):

    def __init__(self):

        super().__init__()
        self.log_emitter = LogEmitter()
        self.log_emitter.log_signal.connect(self.append_log)
        self.setWindowTitle(
            "我的 Minecraft 启动器"
        )

        self.resize(
            1000,
            850
        )

        self.minecraft_dir = (
            Path(
                os.environ.get(
                    "APPDATA",
                    ""
                )
            )
            /
            ".minecraft"
        )

        self.java_paths = []

        self.current_version = None

        self.current_json = None

        self.init_ui()

        self.scan_java()

        if self.minecraft_dir.exists():

            self.scan_versions()


    # =====================================================
    # UI
    # =====================================================
    def append_log(self, text):
        self.log_text.append(text)
    def init_ui(self):

        layout = QVBoxLayout()

        # 标题
        title = QLabel(
            "Minecraft Java 启动器"
        )

        title.setStyleSheet(
            "font-size: 24px;"
            "font-weight: bold;"
        )

        layout.addWidget(title)

        # Java
        java_title = QLabel(
            "Java 环境"
        )

        java_title.setStyleSheet(
            "font-size: 18px;"
            "font-weight: bold;"
        )

        layout.addWidget(java_title)

        self.java_status = QLabel(
            "正在检测 Java..."
        )

        layout.addWidget(
            self.java_status
        )

        java_layout = QHBoxLayout()

        self.java_combo = QComboBox()

        java_layout.addWidget(
            self.java_combo
        )

        java_button = QPushButton(
            "扫描 Java"
        )

        java_button.clicked.connect(
            self.scan_java
        )

        java_layout.addWidget(
            java_button
        )

        layout.addLayout(
            java_layout
        )

        # Minecraft 目录
        mc_title = QLabel(
            "Minecraft 游戏目录"
        )

        mc_title.setStyleSheet(
            "font-size: 18px;"
            "font-weight: bold;"
        )

        layout.addWidget(
            mc_title
        )

        self.minecraft_path_label = QLabel(
            str(self.minecraft_dir)
        )

        layout.addWidget(
            self.minecraft_path_label
        )

        path_button = QPushButton(
            "选择游戏目录"
        )

        path_button.clicked.connect(
            self.choose_minecraft_directory
        )

        layout.addWidget(
            path_button
        )

        # 版本
        version_title = QLabel(
            "Minecraft 版本"
        )

        version_title.setStyleSheet(
            "font-size: 18px;"
            "font-weight: bold;"
        )

        layout.addWidget(
            version_title
        )

        version_layout = QHBoxLayout()

        self.version_combo = QComboBox()

        self.version_combo.currentTextChanged.connect(
            self.version_selected
        )

        version_layout.addWidget(
            self.version_combo
        )

        scan_button = QPushButton(
            "扫描版本"
        )

        scan_button.clicked.connect(
            self.scan_versions
        )

        version_layout.addWidget(
            scan_button
        )

        layout.addLayout(
            version_layout
        )

        # 玩家名称
        player_layout = QHBoxLayout()

        player_layout.addWidget(
            QLabel("游戏昵称：")
        )

        self.player_name = QLineEdit()

        self.player_name.setText(
            "Steve"
        )

        player_layout.addWidget(
            self.player_name
        )

        layout.addLayout(
            player_layout
        )

        # 内存
        memory_layout = QHBoxLayout()

        memory_layout.addWidget(
            QLabel("最大内存：")
        )

        self.memory_spin = QSpinBox()

        self.memory_spin.setRange(
            512,
            32768
        )

        self.memory_spin.setValue(
            4096
        )

        self.memory_spin.setSuffix(
            " MB"
        )

        memory_layout.addWidget(
            self.memory_spin
        )

        layout.addLayout(
            memory_layout
        )

        # 状态
        self.version_status = QLabel(
            "请选择 Minecraft 版本"
        )

        layout.addWidget(
            self.version_status
        )

        # 启动按钮
        self.launch_button = QPushButton(
            "启动 Minecraft"
        )
        self.login_button = QPushButton("登录 Microsoft")
        self.login_button.clicked.connect(self.login_microsoft)

        self.launch_button.setStyleSheet(
            "font-size: 18px;"
            "font-weight: bold;"
            "padding: 10px;"
        )

        self.launch_button.clicked.connect(
            self.launch_minecraft
        )
        layout.addWidget(self.login_button)
        layout.addWidget(self.launch_button)


        # 版本信息
        info_title = QLabel(
            "版本信息"
        )

        info_title.setStyleSheet(
            "font-size: 18px;"
            "font-weight: bold;"
        )

        layout.addWidget(
            info_title
        )

        self.info_text = QTextEdit()

        self.info_text.setReadOnly(
            True
        )

        layout.addWidget(
            self.info_text
        )

        # 启动日志
        log_title = QLabel(
            "启动日志"
        )

        log_title.setStyleSheet(
            "font-size: 18px;"
            "font-weight: bold;"
        )

        layout.addWidget(
            log_title
        )

        self.log_text = QTextEdit()

        self.log_text.setReadOnly(
            True
        )

        layout.addWidget(
            self.log_text
        )

        self.setLayout(
            layout
        )


    # =====================================================
    # Java 扫描
    # =====================================================

    def scan_java(self):

        self.java_combo.clear()

        self.java_paths = find_java()

        if not self.java_paths:

            self.java_status.setText(
                "没有找到 Java"
            )

            return

        self.java_status.setText(
            f"找到 {len(self.java_paths)} 个 Java"
        )

        for java_path in self.java_paths:

            version = get_java_version(
                str(java_path)
            )

            text = (
                f"{version} | "
                f"{java_path}"
            )

            self.java_combo.addItem(
                text,
                str(java_path)
            )


    # =====================================================
    # Minecraft 目录
    # =====================================================

    def choose_minecraft_directory(self):

        directory = QFileDialog.getExistingDirectory(
            self,
            "选择 Minecraft 游戏目录"
        )

        if not directory:
            return

        self.minecraft_dir = Path(
            directory
        )

        self.minecraft_path_label.setText(
            str(self.minecraft_dir)
        )

        self.scan_versions()


    # =====================================================
    # 扫描版本
    # =====================================================

    def scan_versions(self):

        self.version_combo.clear()

        versions = find_minecraft_versions(
            self.minecraft_dir
        )

        if not versions:

            self.version_status.setText(
                "没有找到 Minecraft 版本"
            )

            return

        self.version_combo.addItems(
            versions
        )

        self.version_status.setText(
            f"找到 {len(versions)} 个版本"
        )

        self.version_selected(
            versions[0]
        )


    # =====================================================
    # 版本选择
    # =====================================================

    def version_selected(
        self,
        version
    ):

        if not version:
            return

        self.current_version = version

        data = load_version_with_inheritance(
            self.minecraft_dir,
            version
        )

        if data is None:

            self.version_status.setText(
                "JSON 读取失败"
            )

            self.info_text.clear()

            self.current_json = None

            return

        self.current_json = data

        version_dir = (
            self.minecraft_dir /
            "versions" /
            version
        )

        json_file = (
            version_dir /
            f"{version}.json"
        )

        jar_file = (
            version_dir /
            f"{version}.jar"
        )

        main_class = data.get(
            "mainClass",
            "没有找到"
        )

        libraries = data.get(
            "libraries",
            []
        )

        library_result = get_library_jars(
            self.minecraft_dir,
            data
        )

        info = (
            f"版本：{version}\n\n"
            f"JSON：\n"
            f"{json_file}\n\n"
            f"Minecraft JAR：\n"
            f"{jar_file}\n\n"
            f"Minecraft JAR："
            f"{'存在' if jar_file.exists() else '不存在'}\n\n"
            f"主类：\n"
            f"{main_class}\n\n"
            f"Libraries："
            f"{len(libraries)}\n\n"
            f"实际普通 JAR："
            f"{len(library_result['normal'])}\n\n"
            f"Native："
            f"{len(library_result['native'])}\n\n"
            f"真正缺失："
            f"{len(library_result['missing'])}"
        )

        self.info_text.setPlainText(
            info
        )

        self.version_status.setText(
            f"{version}：准备完成"
        )


    # =====================================================
    # 启动 Minecraft
    # =====================================================
    def read_minecraft_output(self, process):
        try:
            for line in process.stdout:
                line = line.rstrip()

                if line:
                    print(line)

                    # 把 Minecraft 日志发送到启动器窗口
                    self.log_emitter.log_signal.emit(line)

            process.wait()

            message = (
                "\n========== Minecraft 已退出 ==========\n"
                f"退出代码：{process.returncode}"
            )

            print(message)
            self.log_emitter.log_signal.emit(message)

        except Exception as e:
            message = (
                f"读取 Minecraft 日志失败：{e}"
            )

            print(message)
            self.log_emitter.log_signal.emit(message)

    def login_microsoft(self):
        try:
            self.log_text.append(
                "\n========== Microsoft 登录 =========="
            )

            self.log_text.append(
                "正在打开 Microsoft 登录页面..."
            )

            app = msal.PublicClientApplication(
                CLIENT_ID,
                authority=AUTHORITY
            )

            result = app.acquire_token_interactive(
                scopes=SCOPES
            )

            if "access_token" in result:

                # 保存 Microsoft Access Token
                self.microsoft_access_token = result["access_token"]

                account = result.get("account")

                if account:
                    username = account.get(
                        "username",
                        "未知账户"
                    )

                    self.log_text.append(
                        f"Microsoft 登录成功：{username}"
                    )

                    self.login_button.setText(
                        f"已登录：{username}"
                    )

                else:
                    self.log_text.append(
                        "Microsoft 登录成功"
                    )

                # ==============================
                # Microsoft → Xbox Live
                # ==============================
                self.authenticate_xbox(
                    self.microsoft_access_token
                )

            else:
                error = result.get(
                    "error_description",
                    result.get("error", "未知错误")
                )

                self.log_text.append(
                    f"Microsoft 登录失败：{error}"
                )

        except Exception as e:

            self.log_text.append(
                f"Microsoft 登录异常：{e}"
            )

    def authenticate_xbox(self, microsoft_token):
        try:
            self.log_text.append("正在获取 Xbox Live Token...")

            url = "https://user.auth.xboxlive.com/user/authenticate"

            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-xbl-contract-version": "1"
            }

            data = {
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": "d=" + microsoft_token
                },
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT"
            }

            response = requests.post(
                url,
                headers=headers,
                json=data,
                timeout=15
            )

            self.log_text.append(
                f"Xbox Live HTTP 状态码：{response.status_code}"
            )

            if response.status_code != 200:
                self.log_text.append(
                    f"Xbox Live 登录失败：{response.text}"
                )
                return None

            result = response.json()

            xbox_token = result.get("Token")
            user_hash = None

            claims = result.get("DisplayClaims", {})
            xui = claims.get("xui", [])

            if xui:
                user_hash = xui[0].get("uhs")

            if not xbox_token or not user_hash:
                self.log_text.append(
                    "Xbox Live 返回的数据不完整"
                )
                return None

            self.log_text.append("Xbox Live 登录成功！")

            # 保存下来，后面 XSTS 要用
            self.xbox_token = xbox_token
            self.user_hash = user_hash

            self.log_text.append("Xbox Live 登录成功！")

            # 继续进行 XSTS 认证
            self.authenticate_xsts(xbox_token)

            return xbox_token, user_hash

        except Exception as e:
            self.log_text.append(
                f"Xbox Live 登录异常：{e}"
            )
            return None

    def authenticate_xsts(self, xbox_token):
        try:
            self.log_text.append("正在获取 XSTS Token...")

            url = "https://xsts.auth.xboxlive.com/xsts/authorize"

            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-xbl-contract-version": "1"
            }

            data = {
                "Properties": {
                    "SandboxId": "RETAIL",
                    "UserTokens": [
                        xbox_token
                    ]
                },
                "RelyingParty": "rp://api.minecraftservices.com/",
                "TokenType": "JWT"
            }

            response = requests.post(
                url,
                headers=headers,
                json=data,
                timeout=15
            )

            self.log_text.append(
                f"XSTS HTTP 状态码：{response.status_code}"
            )

            if response.status_code != 200:
                self.log_text.append(
                    f"XSTS 登录失败：{response.text}"
                )
                return None

            result = response.json()

            xsts_token = result.get("Token")

            if not xsts_token:
                self.log_text.append(
                    "XSTS 返回的数据中没有 Token"
                )
                return None

            self.xsts_token = xsts_token

            self.log_text.append(
                "XSTS 登录成功！"
            )

            # 继续获取 Minecraft Access Token
            self.authenticate_minecraft(xsts_token)

            return xsts_token

            return xsts_token

        except Exception as e:
            self.log_text.append(
                f"XSTS 登录异常：{e}"
            )

    def authenticate_minecraft(self, xsts_token):
        try:
            self.log_text.append("正在获取 Minecraft Access Token...")

            url = "https://api.minecraftservices.com/authentication/login_with_xbox"

            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

            data = {
                "identityToken": f"XBL3.0 x={self.user_hash};{xsts_token}"
            }

            response = requests.post(
                url,
                headers=headers,
                json=data,
                timeout=15
            )

            self.log_text.append(
                f"Minecraft HTTP 状态码：{response.status_code}"
            )

            if response.status_code != 200:
                self.log_text.append(
                    f"Minecraft 登录失败：{response.text}"
                )
                return None

            result = response.json()

            minecraft_token = result.get("access_token")

            if not minecraft_token:
                self.log_text.append(
                    "Minecraft 返回的数据中没有 Access Token"
                )
                return None

            self.minecraft_access_token = minecraft_token

            self.log_text.append(
                "Minecraft Access Token 获取成功！"
            )

            return minecraft_token

        except Exception as e:
            self.log_text.append(
                f"Minecraft 登录异常：{e}"
            )
            return None


    def launch_minecraft(self):

        self.log_text.clear()

        # -------------------------------
        # 检查 Java
        # -------------------------------

        java_index = (
            self.java_combo.currentIndex()
        )

        if java_index < 0:

            QMessageBox.warning(
                self,
                "启动失败",
                "没有选择 Java"
            )

            return

        java_path = (
            self.java_combo.itemData(
                java_index
            )
        )

        if not java_path:

            QMessageBox.warning(
                self,
                "启动失败",
                "Java 路径无效"
            )

            return

        # -------------------------------
        # 检查版本
        # -------------------------------

        version = (
            self.version_combo.currentText()
        )

        if not version:

            QMessageBox.warning(
                self,
                "启动失败",
                "没有选择 Minecraft 版本"
            )

            return

        data = load_version_with_inheritance(
            self.minecraft_dir,
            version
        )

        if data is None:

            QMessageBox.warning(
                self,
                "启动失败",
                "无法读取版本 JSON"
            )

            return

        # -------------------------------
        # Minecraft JAR
        # -------------------------------

        version_dir = (
            self.minecraft_dir /
            "versions" /
            version
        )

        version_jar = (
            version_dir /
            f"{version}.jar"
        )

        if not version_jar.exists():

            QMessageBox.warning(
                self,
                "启动失败",
                f"没有找到 Minecraft JAR：\n"
                f"{version_jar}"
            )

            return

        # -------------------------------
        # Libraries
        # -------------------------------

        library_result = get_library_jars(
            self.minecraft_dir,
            data
        )

        normal_jars = (
            library_result["normal"]
        )

        missing = (
            library_result["missing"]
        )

        if missing:

            text = (
                "有 Libraries 缺失，暂时不能启动。\n\n"
                f"缺失数量：{len(missing)}\n\n"
            )

            for name, path in missing[:10]:

                text += (
                    f"{name}\n"
                    f"{path}\n\n"
                )

            QMessageBox.warning(
                self,
                "Libraries 缺失",
                text
            )

            return

        # -------------------------------
        # ClassPath
        # -------------------------------

        classpath_list = []

        for jar in normal_jars:

            classpath_list.append(
                str(jar)
            )

        classpath_list.append(
            str(version_jar)
        )

        classpath = ";".join(
            classpath_list
        )

        # -------------------------------
        # Assets
        # -------------------------------

        assets_name = data.get(
            "assets",
            ""
        )

        assets_root = (
            self.minecraft_dir /
            "assets"
        )

        # -------------------------------
        # 玩家信息
        # -------------------------------

        player_name = (
            self.player_name.text().strip()
        )

        if not player_name:

            player_name = "Steve"

        # -------------------------------
        # 参数变量
        # -------------------------------

        variables = {

            "auth_player_name":
                player_name,

            "version_name":
                version,

            "game_directory":
                str(self.minecraft_dir),

            "assets_root":
                str(assets_root),

            "assets_index_name":
                assets_name,

            "auth_uuid":
                "00000000-0000-0000-0000-000000000000",

            "auth_access_token":
                "0",

            "user_type":
                "legacy",

            "version_type":
                "release",

            "resolution_width":
                "854",

            "resolution_height":
                "480",

            "natives_directory":
                str(
                    self.minecraft_dir /
                    "natives"
                ),

            "classpath":
                classpath
        }

        # -------------------------------
        # JVM 参数
        # -------------------------------

        jvm_arguments = get_jvm_arguments(
            data
        )

        processed_jvm = []

        for arg in jvm_arguments:

            arg = replace_placeholders(
                arg,
                variables
            )

            processed_jvm.append(
                arg
            )

        # -------------------------------
        # 游戏参数
        # -------------------------------

        game_arguments = get_game_arguments(
            data
        )

        processed_game = []

        for arg in game_arguments:

            arg = replace_placeholders(
                arg,
                variables
            )

            processed_game.append(
                arg
            )

        # -------------------------------
        # 内存
        # -------------------------------

        memory = (
            self.memory_spin.value()
        )

        # -------------------------------
        # 构造 Java 命令
        # -------------------------------
        natives_directory = (
                self.minecraft_dir /
                "natives"
        )

        extract_natives(
            library_result["native"],
            natives_directory
        )
        command = []

        command.append(
            java_path
        )

        command.append(
            f"-Xmx{memory}M"
        )

        command.append(
            f"-Xms{min(memory, 1024)}M"
        )

        # JVM 参数
        command.extend(
            processed_jvm
        )

        # 某些 JSON 自己已经有 classpath
        # 如果没有，我们手动加入
        if "-cp" not in processed_jvm and "-classpath" not in processed_jvm:

            command.extend([
                "-cp",
                classpath
            ])

        # 主类
        main_class = data.get(
            "mainClass"
        )

        if not main_class:

            QMessageBox.warning(
                self,
                "启动失败",
                "JSON 中没有 mainClass"
            )

            return

        command.append(
            main_class
        )

        # 游戏参数
        command.extend(
            processed_game
        )

        # -------------------------------
        # 显示命令
        # -------------------------------

        self.log_text.append(
            "========== Minecraft 启动 ==========\n"
        )

        self.log_text.append(
            "Java：\n"
            f"{java_path}\n"
        )

        self.log_text.append(
            "版本：\n"
            f"{version}\n"
        )

        self.log_text.append(
            "玩家：\n"
            f"{player_name}\n"
        )

        self.log_text.append(
            "ClassPath 数量：\n"
            f"{len(classpath_list)}\n"
        )

        self.log_text.append(
            "\n========== 启动命令 ==========\n"
        )

        self.log_text.append(
            " ".join(
                f'"{x}"'
                if " " in str(x)
                else str(x)
                for x in command
            )
        )

        self.log_text.append(
            "\n\n正在启动 Minecraft..."
        )

        # -------------------------------
        # 启动
        # -------------------------------

        try:

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1
            )

            self.log_text.append(
                "\n========== Minecraft 已启动 ==========\n"
            )

            self.log_text.append(
                "Minecraft 正在运行，启动器保持可用。\n"
            )

            thread = threading.Thread(
                target=self.read_minecraft_output,
                args=(process,),
                daemon=True
            )

            thread.start()

        except Exception as e:

            QMessageBox.critical(
                self,
                "启动失败",
                str(e)
            )

            self.log_text.append(
                f"\n启动失败：{e}"
            )


# =========================================================
# 程序入口
# =========================================================

if __name__ == "__main__":

    app = QApplication(
        sys.argv
    )

    window = LauncherWindow()

    window.show()

    sys.exit(
        app.exec()
    )