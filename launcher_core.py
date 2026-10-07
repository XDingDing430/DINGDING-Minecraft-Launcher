"""Local Minecraft launch preparation. This module has no GUI or network side effects."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

CURRENT_OS = {"Windows": "windows", "Darwin": "osx"}.get(platform.system(), "linux")
DEFAULT_CONFIG = {
    "minecraft_dir": "", "java_path": "", "memory": 4096,
    "selected_version": "", "account_type": "offline", "offline_name": "Steve",
    "selected_account": "", "game_width": 854, "game_height": 480,
    "fullscreen": False, "java_auto": True,
    "local_modpacks": [], "selected_modpack": "",
}


class LauncherError(ValueError):
    """An actionable error that can be shown directly to the player."""


def get_host_architecture() -> str:
    architecture = platform.machine() or os.getenv("PROCESSOR_ARCHITEW6432") or os.getenv("PROCESSOR_ARCHITECTURE")
    if architecture:
        return architecture.lower()
    if os.name == "nt":
        # Some packaged/sandboxed environments omit the processor environment
        # variables. GetNativeSystemInfo also handles x64 emulation on ARM64.
        import ctypes
        from ctypes import wintypes

        class SystemInfo(ctypes.Structure):
            _fields_ = [("architecture", wintypes.WORD), ("reserved", wintypes.WORD),
                        ("page_size", wintypes.DWORD), ("minimum_address", ctypes.c_void_p),
                        ("maximum_address", ctypes.c_void_p), ("processor_mask", ctypes.c_size_t),
                        ("processor_count", wintypes.DWORD), ("processor_type", wintypes.DWORD),
                        ("allocation_granularity", wintypes.DWORD), ("processor_level", wintypes.WORD),
                        ("processor_revision", wintypes.WORD)]

        try:
            function = ctypes.WinDLL("kernel32").GetNativeSystemInfo
            function.argtypes = [ctypes.POINTER(SystemInfo)]
            function.restype = None
            info = SystemInfo()
            function(ctypes.byref(info))
            return {0: "x86", 9: "amd64", 12: "arm64"}.get(info.architecture, "")
        except OSError:
            pass
    return ""


def load_config(path: Path) -> dict:
    config = copy.deepcopy(DEFAULT_CONFIG)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            config.update(data)
    except (OSError, ValueError):
        pass
    for key, default in DEFAULT_CONFIG.items():
        if isinstance(default, str) and not isinstance(config[key], str):
            config[key] = default
    for key, lower, upper in (("memory", 512, 32768), ("game_width", 320, 7680),
                              ("game_height", 240, 4320)):
        value = config[key]
        if type(value) is not int:
            value = DEFAULT_CONFIG[key]
        config[key] = max(lower, min(upper, value))
    if type(config["fullscreen"]) is not bool:
        config["fullscreen"] = False
    if type(config["java_auto"]) is not bool:
        config["java_auto"] = True
    packs, seen = [], set()
    if isinstance(config["local_modpacks"], list):
        for item in config["local_modpacks"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("version"), str):
                continue
            path, version = item["path"], item["version"]
            if not path or "\x00" in path or not Path(path).is_absolute():
                continue
            try:
                _safe_version_name(version)
            except LauncherError:
                continue
            key = os.path.normcase(path)
            if key not in seen:
                seen.add(key)
                packs.append({"path": path, "version": version})
    config["local_modpacks"] = packs
    # The application ID now belongs to the launcher author, not user settings.
    config.pop("microsoft_client_id", None)
    if config["account_type"] not in ("offline", "microsoft"):
        config["account_type"] = "offline"
    return config


def save_config(config: dict, path: Path) -> None:
    """Replace the config atomically; failed writes leave the previous file intact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="config-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def get_offline_uuid(player_name: str) -> str:
    return str(uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:" + player_name).encode("utf-8")).digest(),
                         version=3))


def validate_player_name(name: str) -> str:
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name):
        raise LauncherError("离线昵称需为 1–16 个英文字母、数字或下划线。")
    return name


def _safe_version_name(name: str) -> str:
    if not isinstance(name, str) or not name or name in (".", "..") or re.search(r'[\\/:\x00]', name):
        raise LauncherError("版本名称无效。")
    return name


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LauncherError(f"无法读取 JSON：{path}\n{exc}") from exc
    if not isinstance(data, dict):
        raise LauncherError(f"JSON 根节点必须是对象：{path}")
    return data


def find_minecraft_versions(minecraft_dir: Path) -> list[str]:
    directory = Path(minecraft_dir) / "versions"
    if not directory.is_dir():
        return []
    try:
        return sorted((p.name for p in directory.iterdir()
                       if p.is_dir() and (p / f"{p.name}.json").is_file()), key=str.casefold)
    except OSError as exc:
        raise LauncherError(f"无法扫描版本目录：{exc}") from exc


def find_modpack_instances(minecraft_dir: Path) -> list[str]:
    directory = Path(minecraft_dir) / "instances"
    if not directory.is_dir():
        return []
    markers = ("instance.json", "manifest.json", "modrinth.index.json", "mods", "config", "versions")
    try:
        return sorted((p.name for p in directory.iterdir() if p.is_dir()
                       and any((p / nested / name).exists() for nested in ("", ".minecraft", "minecraft")
                               for name in markers)),
                      key=str.casefold)
    except OSError as exc:
        raise LauncherError(f"无法扫描实例目录：{exc}") from exc


def _library_key(library: dict) -> tuple:
    parts = library.get("name", "").split(":")
    if len(parts) < 3:
        return (library.get("name", ""),)
    extension = library["name"].split("@", 1)[1] if "@" in library["name"] else "jar"
    classifier = parts[3].split("@", 1)[0] if len(parts) > 3 else ""
    # A parent may declare different versions of the same artifact for Windows
    # and macOS. Those variants must survive inheritance independently.
    rules = json.dumps(library.get("rules", []), sort_keys=True)
    return parts[0], parts[1], classifier, extension, rules


def _merge_version(parent: dict, child: dict) -> dict:
    merged = copy.deepcopy(parent)
    merged.update(copy.deepcopy(child))
    libraries = {}
    for library in parent.get("libraries", []) + child.get("libraries", []):
        libraries[_library_key(library)] = copy.deepcopy(library)
    merged["libraries"] = list(libraries.values())
    # Legacy arguments and modern arguments are alternative schemas.
    if "minecraftArguments" in child and "arguments" not in child:
        merged.pop("arguments", None)
    else:
        arguments = copy.deepcopy(parent.get("arguments", {}))
        for key, value in child.get("arguments", {}).items():
            if isinstance(value, list) and isinstance(arguments.get(key, []), list):
                arguments[key] = arguments.get(key, []) + copy.deepcopy(value)
            else:
                arguments[key] = copy.deepcopy(value)
        if arguments:
            merged["arguments"] = arguments
    return merged


@dataclass(frozen=True)
class VersionSpec:
    name: str
    data: dict
    json_path: Path
    jar_path: Path
    roots: tuple[Path, ...]


def load_version(minecraft_dir: Path, version: str, extra_roots=(), visited=()) -> VersionSpec:
    version = _safe_version_name(version)
    if version in visited:
        raise LauncherError("版本继承存在循环：" + " → ".join((*visited, version)))
    roots = tuple(dict.fromkeys((Path(minecraft_dir), *(Path(p) for p in extra_roots))))
    json_path = next((root / "versions" / version / f"{version}.json" for root in roots
                      if (root / "versions" / version / f"{version}.json").is_file()), None)
    if json_path is None:
        raise LauncherError(f"没有找到版本 {version} 的 JSON，请先完整安装该版本。")
    child = _read_json(json_path)
    parent_name = child.get("inheritsFrom")
    parent = load_version(roots[0], parent_name, roots[1:], (*visited, version)) if parent_name else None
    data = _merge_version(parent.data, child) if parent else child
    jar_id = _safe_version_name(child.get("jar", version))
    own_jar = next((root / "versions" / jar_id / f"{jar_id}.jar" for root in roots
                    if (root / "versions" / jar_id / f"{jar_id}.jar").is_file()), None)
    if child.get("jar") and own_jar is None:
        raise LauncherError(f"版本声明的游戏 JAR 不存在：{jar_id}")
    jar = own_jar or (parent.jar_path if parent else json_path.with_suffix(".jar"))
    return VersionSpec(version, data, json_path, jar, roots)


def _instance_game_dir(instance: Path) -> Path:
    for name in (".minecraft", "minecraft"):
        if (instance / name).is_dir():
            return instance / name
    return instance


def resolve_instance(minecraft_dir: Path, name: str) -> tuple[VersionSpec, Path]:
    instance = Path(minecraft_dir) / "instances" / _safe_version_name(name)
    game_dir = _instance_game_dir(instance)
    metadata, metadata_type = {}, ""
    for filename in ("instance.json", "manifest.json", "modrinth.index.json"):
        if (instance / filename).is_file():
            metadata = _read_json(instance / filename)
            metadata_type = filename
            break
    minecraft = metadata.get("minecraft", {})
    dependencies = metadata.get("dependencies", {})
    if not isinstance(minecraft, dict) or not isinstance(dependencies, dict):
        raise LauncherError("整合包的 minecraft / dependencies 配置格式无效。")
    requested = next((metadata.get(key) for key in ("version_id", "minecraft_version", "version")
                      if isinstance(metadata.get(key), str)), None) if metadata_type == "instance.json" else None
    # Modrinth's top-level versionId is a pack version, not a game version.
    requested = requested or minecraft.get("version") or dependencies.get("minecraft")
    loaders = minecraft.get("modLoaders", [])
    if not isinstance(loaders, list) or not all(isinstance(item, dict) for item in loaders):
        raise LauncherError("整合包的 modLoaders 配置格式无效。")
    loader = next((item.get("id", "") for item in loaders if item.get("primary")), "")
    if not loader and loaders:
        loader = loaders[0].get("id", "")
    for key in ("fabric-loader", "quilt-loader", "forge", "neoforge"):
        if key in dependencies:
            loader = f"{key}-{dependencies[key]}"
            break
    roots = tuple(dict.fromkeys((game_dir, instance, Path(minecraft_dir))))
    installed = list(dict.fromkeys(v for root in roots for v in find_minecraft_versions(root)))
    if requested and requested in installed and not loader:
        return load_version(roots[0], requested, roots[1:]), game_dir
    if requested:
        candidates = []
        for version in installed:
            spec = load_version(roots[0], version, roots[1:])
            base = spec.data.get("inheritsFrom", spec.data.get("id", version))
            if requested not in (base, version):
                continue
            if loader:
                family, _, loader_version = loader.partition("-")
                loader_version = loader_version.removeprefix("loader-")
                metadata_text = " ".join([version, spec.data.get("mainClass", ""),
                                           *(lib.get("name", "") for lib in spec.data.get("libraries", []))]).lower()
                if family.lower() not in metadata_text or loader_version.lower() not in metadata_text:
                    continue
            candidates.append(spec)
    else:
        # Only infer a version when the instance has exactly one local version.
        candidates = [load_version(roots[0], version, roots[1:]) for version in installed
                      if any((root / "versions" / version).is_dir() for root in roots[:-1])]
    if len(candidates) == 1:
        return candidates[0], game_dir
    if len(candidates) > 1:
        raise LauncherError("整合包对应多个已安装版本，请在 instance.json 中填写准确的 version_id。")
    raise LauncherError(f"整合包尚未关联完整的本地游戏版本。\n请在 instance.json 中填写 version_id，"
                        f"并确保对应版本及依赖已安装。\n实例目录：{instance}")


def _has_mod_loader(spec: VersionSpec) -> bool:
    prefixes = ("net.minecraftforge:", "net.neoforged:", "net.fabricmc:fabric-loader:",
                "org.quiltmc:quilt-loader:", "com.mumfrey:liteloader:")
    return any(lib.get("name", "").startswith(prefixes) for lib in spec.data.get("libraries", []))


def _contains_mods(directory: Path) -> bool:
    mods = directory / "mods"
    return mods.is_dir() and next(mods.rglob("*.jar"), None) is not None


def _matches_mod_loader(spec: VersionSpec, loader: str) -> bool:
    family, _, release = loader.partition("-")
    release = release.removeprefix("loader-")
    artifacts = {"forge": ("net.minecraftforge", "forge"), "neoforge": ("net.neoforged", "neoforge"),
                 "fabric": ("net.fabricmc", "fabric-loader"), "quilt": ("org.quiltmc", "quilt-loader")}
    expected = artifacts.get(family.lower())
    if not expected or not release:
        return False
    for lib in spec.data.get("libraries", []):
        parts = lib.get("name", "").split(":")
        if len(parts) >= 3 and tuple(parts[:2]) == expected:
            actual = parts[2].split("@", 1)[0]
            if actual == release or family == "forge" and actual.endswith("-" + release):
                return True
    return False


def find_local_modpack_versions(directory: Path, shared_root: Path) -> list[tuple[VersionSpec, Path]]:
    """Read an existing unpacked pack in place. Never install, copy, or edit it."""
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise LauncherError(f"整合包文件夹不存在：{directory}")
    if directory.name == "overrides" and any((directory.parent / name).is_file()
            for name in ("manifest.json", "modrinth.index.json")):
        raise LauncherError("overrides 是分发包的覆盖文件，不是完整的游戏目录。请先完成安装，再选择实际游戏文件夹。")
    game_dir = _instance_game_dir(directory)
    version_folder = (directory.parent.name == "versions"
                      and (directory / f"{directory.name}.json").is_file())
    local_roots = (directory.parent.parent,) if version_folder else tuple(dict.fromkeys((game_dir, directory)))
    roots = tuple(dict.fromkeys((*local_roots, Path(shared_root).resolve())))
    metadata_dir = directory
    if directory.name in (".minecraft", "minecraft") and any(
            (directory.parent / name).is_file() for name in ("instance.json", "manifest.json", "modrinth.index.json")):
        metadata_dir = directory.parent
    metadata, metadata_type = {}, ""
    for name in ("instance.json", "manifest.json", "modrinth.index.json"):
        if (metadata_dir / name).is_file():
            metadata, metadata_type = _read_json(metadata_dir / name), name
            break
    local = [directory.name] if version_folder else list(dict.fromkeys(
        name for root in local_roots for name in find_minecraft_versions(root)))
    if not local and not any((game_dir / marker).is_dir() for marker in ("mods", "config", "saves")):
        raise LauncherError("没有找到整合包的游戏文件。请选择包含 versions、mods 或 .minecraft 的完整游戏文件夹。\n"
                            "只有 manifest / index / overrides 的已解压分发包仍需安装，不属于可直接启动的整合包。")
    minecraft, dependencies = metadata.get("minecraft", {}), metadata.get("dependencies", {})
    if not isinstance(minecraft, dict) or not isinstance(dependencies, dict):
        raise LauncherError("整合包的 minecraft / dependencies 配置格式无效。")
    exact = next((metadata.get(key) for key in ("version_id", "minecraft_version", "version")
                  if isinstance(metadata.get(key), str) and metadata.get(key)), None) if metadata_type == "instance.json" else None
    requested = minecraft.get("version") or dependencies.get("minecraft")
    loaders = minecraft.get("modLoaders", [])
    if not isinstance(loaders, list) or not all(isinstance(item, dict) for item in loaders):
        raise LauncherError("整合包的 modLoaders 配置格式无效。")
    loader = next((item.get("id", "") for item in loaders if item.get("primary")), "")
    loader = loader or (loaders[0].get("id", "") if loaders else "")
    for key in ("fabric-loader", "quilt-loader", "forge", "neoforge"):
        if key in dependencies:
            loader = f"{key}-{dependencies[key]}"
            break
    if not isinstance(requested, (str, type(None))) or not isinstance(loader, str):
        raise LauncherError("整合包的游戏版本或加载器信息无效。")
    installed = list(dict.fromkeys(name for root in roots for name in find_minecraft_versions(root)))
    # Without metadata, keep local versions local. A mods-only folder can be
    # explicitly linked by the user to an already-installed mod-loader profile.
    names = [exact] if exact else installed if requested else local or installed
    candidates, errors = [], []
    for name in names:
        try:
            spec = load_version(roots[0], name, roots[1:])
        except LauncherError as exc:
            errors.append(str(exc))
            continue
        if requested and requested not in (spec.name, spec.data.get("id"), spec.data.get("inheritsFrom"),
                                          spec.data.get("minecraftVersion"), spec.data.get("clientVersion")):
            continue
        if loader:
            if not _matches_mod_loader(spec, loader):
                continue
        effective_dir = game_dir
        isolated = spec.json_path.parent
        if not version_folder and not any((game_dir / marker).is_dir() for marker in ("mods", "config", "saves")):
            if any((isolated / marker).is_dir() for marker in ("mods", "config", "saves")):
                effective_dir = isolated
        if (_contains_mods(effective_dir) or not local and not exact and not requested) and not _has_mod_loader(spec):
            continue
        candidates.append((spec, effective_dir))
    if not candidates:
        detail = f"\n{errors[0]}" if errors else ""
        raise LauncherError("没有找到匹配的已安装游戏版本／Mod 加载器。\n"
                            "请使用包含版本和依赖的完整整合包，或先在主游戏目录安装对应版本。"
                            "此功能只启动已有文件，不下载依赖。" + detail)
    return candidates


def resolve_local_modpack(directory: Path, shared_root: Path, version: str) -> tuple[VersionSpec, Path]:
    version = _safe_version_name(version)
    for spec, game_dir in find_local_modpack_versions(directory, shared_root):
        if spec.name == version:
            return spec, game_dir
    raise LauncherError(f"整合包关联的版本 {version} 已不存在或不匹配，请重新添加该文件夹。")


VERSION_TYPE_LABELS = {"vanilla": "原版", "modded": "Mod 版本", "modpack": "整合包", "unknown": "未识别"}


def detect_version_type(spec: VersionSpec, game_dir: Path, declared_modpack=False) -> str:
    """Classify by installed contents and backend, never by a profile's nickname."""
    if declared_modpack or any((game_dir / name).is_file() for name in
            ("instance.json", "manifest.json", "modrinth.index.json")):
        return "modpack"
    main_class = spec.data.get("mainClass", "").casefold()
    modified_main = main_class.startswith(("net.fabricmc.", "org.quiltmc.", "net.minecraftforge.",
        "net.neoforged.", "cpw.mods.", "net.minecraft.launchwrapper."))
    if _has_mod_loader(spec) or modified_main or any(lib.get("name", "").casefold().startswith("optifine:")
            for lib in spec.data.get("libraries", [])):
        # Vanilla ignores shared root/mods; those files alone must not turn
        # every vanilla installation into a pack.
        return "modpack" if _has_mod_loader(spec) and _contains_mods(game_dir) else "modded"
    if main_class in ("net.minecraft.client.main.main", "net.minecraft.client.minecraft",
                      "com.mojang.minecraft.minecraft", "com.mojang.rubydung.rubydung"):
        return "vanilla"
    return "unknown"


def build_version_entries(root: Path, versions: list[str], instances: list[str], packs: list[dict],
                          selected_pack="") -> list[dict]:
    """Merge scan/manual entries by real version JSON AND effective game folder.

    Manual entries take precedence, preserving their saved selection and path.
    Equal names in different directories are deliberately not merged.
    """
    def path_key(path):
        return os.path.normcase(str(Path(path).resolve()))

    def describe(item):
        spec, directory = None, root
        try:
            if item["type"] == "local_modpack":
                spec, directory = resolve_local_modpack(Path(item["path"]), root, item["version"])
            elif item["type"] == "modpack":
                spec, directory = resolve_instance(root, item["name"])
            else:
                spec, directory = load_version(root, item["name"]), root
                isolated = spec.json_path.parent
                if any((isolated / marker).is_dir() for marker in ("mods", "config", "saves")):
                    directory = isolated
                    spec, directory = resolve_local_modpack(isolated, root, item["name"])
            key = path_key(spec.json_path), path_key(directory)
        except (LauncherError, OSError):
            # Retain broken/moved entries so selecting them still explains the
            # problem. Never hide a healthy version merely because names match.
            key = "unresolved", item["type"], path_key(item.get("path", root)), item.get("version", item["name"])
        declared = item["type"] != "version"
        try:
            category = detect_version_type(spec, directory, declared) if spec else "modpack" if declared else "unknown"
        except OSError:
            category = "modpack" if declared else "unknown"
        return key, category, str(directory)

    manual, automatic, merged = [], [], {}
    ordered_packs = sorted(packs, key=lambda pack: path_key(pack["path"]) != path_key(selected_pack)) if selected_pack else packs
    for pack in ordered_packs:
        name = Path(pack["path"]).name
        label = f"整合包：{name}" if name == pack["version"] else f"整合包：{name}（{pack['version']}）"
        item = {"type": "local_modpack", "name": name, "path": pack["path"], "version": pack["version"]}
        key, category, directory = describe(item)
        item["category"] = category
        if key not in merged:
            entry = {"label": label, "data": item, "game_dir": directory,
                     "aliases": [f"整合包：{name}（{pack['version']}）"]}
            merged[key] = entry
            manual.append(entry)
    for item in ([{"type": "version", "name": version} for version in versions]
                 + [{"type": "modpack", "name": name} for name in instances]):
        old_label = item["name"] if item["type"] == "version" else f"整合包：{item['name']}"
        key, category, directory = describe(item)
        item["category"] = category
        label = f"{VERSION_TYPE_LABELS[category]}：{item['name']}"
        aliases = [old_label] + [f"{prefix}：{item['name']}" for prefix in VERSION_TYPE_LABELS.values()]
        if key in merged:
            merged[key]["aliases"].extend(aliases)
        else:
            entry = {"label": label, "data": item, "game_dir": directory, "aliases": aliases}
            merged[key] = entry
            automatic.append(entry)
    rank = {"vanilla": 0, "modded": 1, "modpack": 2, "unknown": 3}
    return sorted(automatic + manual, key=lambda entry: rank[entry["data"]["category"]])


def rules_allow(rules, features=None, os_name=None, architecture=None, os_version=None) -> bool:
    if not rules:
        return True
    features = features or {}
    os_name = os_name or CURRENT_OS
    architecture = architecture or get_host_architecture()
    os_version = os_version if os_version is not None else platform.version()
    allowed = False
    for rule in rules:
        os_rule = rule.get("os", {})
        if os_rule.get("name", os_name) != os_name:
            continue
        arch = os_rule.get("arch")
        normalized = {"amd64": "x86_64", "x64": "x86_64", "i386": "x86", "i686": "x86",
                      "aarch64": "arm64"}
        if arch and normalized.get(arch.lower(), arch.lower()) != normalized.get(architecture, architecture):
            continue
        if "version" in os_rule:
            try:
                if not re.search(os_rule["version"], os_version):
                    continue
            except re.error as exc:
                raise LauncherError(f"无效的系统版本规则：{exc}") from exc
        if any(features.get(key, False) != value for key, value in rule.get("features", {}).items()):
            continue
        allowed = rule.get("action") == "allow"
    return allowed


def library_name_to_path(name: str) -> Path:
    coordinate, _, extension = name.partition("@")
    parts = coordinate.split(":")
    if len(parts) not in (3, 4):
        raise LauncherError(f"无法识别 Library 坐标：{name}")
    group, artifact, version = parts[:3]
    classifier = f"-{parts[3]}" if len(parts) == 4 else ""
    return Path(group.replace(".", "/")) / artifact / version / f"{artifact}-{version}{classifier}.{extension or 'jar'}"


def _find_relative(roots, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts or relative.drive:
        raise LauncherError(f"依赖文件路径无效：{relative}")
    return next((root / relative for root in roots if (root / relative).is_file()), roots[0] / relative)


def get_library_jars(spec: VersionSpec, architecture=None) -> dict:
    normal, natives, missing, skipped = [], [], [], []
    library_roots = tuple(root / "libraries" for root in spec.roots)
    architecture = architecture or get_host_architecture()
    arch_bits = "32" if architecture.lower() in ("x86", "i386", "i686") else "64"
    for library in spec.data.get("libraries", []):
        name = library.get("name", "未知 Library")
        if not rules_allow(library.get("rules"), architecture=architecture):
            skipped.append(name)
            continue
        downloads = library.get("downloads", {})
        artifact = downloads.get("artifact")
        classifiers = downloads.get("classifiers", {})
        native_classifier = library.get("natives", {}).get(CURRENT_OS)
        # Native-only legacy libraries need only their selected classifier JAR.
        # Some legacy manifests also declare a 22-byte empty ZIP placeholder.
        placeholder = bool(native_classifier and artifact and artifact.get("size") == 22)
        if artifact and not placeholder or not artifact and not native_classifier and not classifiers:
            relative = Path(artifact.get("path") or library_name_to_path(name)) if artifact else library_name_to_path(name)
            jar = _find_relative(library_roots, relative)
            if jar.is_file():
                normal.append(jar)
                if len(name.split(":")) > 3 and name.split(":")[3].startswith(f"natives-{CURRENT_OS}"):
                    natives.append((jar, ()))
            else:
                missing.append((name, jar))
        if native_classifier:
            native_classifier = native_classifier.replace("${arch}", arch_bits)
        elif f"natives-{CURRENT_OS}-{arch_bits}" in classifiers:
            native_classifier = f"natives-{CURRENT_OS}-{arch_bits}"
        elif f"natives-{CURRENT_OS}" in classifiers:
            native_classifier = f"natives-{CURRENT_OS}"
        if native_classifier:
            info = classifiers.get(native_classifier, {})
            coordinate = ":".join(name.split(":")[:3]) + ":" + native_classifier
            relative = Path(info.get("path") or library_name_to_path(coordinate))
            jar = _find_relative(library_roots, relative)
            if jar.is_file():
                natives.append((jar, tuple(library.get("extract", {}).get("exclude", []))))
            else:
                missing.append((f"{name} ({native_classifier})", jar))
    return {"normal": list(dict.fromkeys(normal)), "native": list(dict.fromkeys(natives)),
            "missing": missing, "skipped": skipped}


def get_arguments(data: dict, kind: str, features=None, architecture=None) -> list[str]:
    if "arguments" not in data:
        if kind != "game":
            return []
        try:
            return shlex.split(data.get("minecraftArguments", ""))
        except ValueError as exc:
            raise LauncherError(f"旧版启动参数格式无效：{exc}") from exc
    result = []
    for argument in data["arguments"].get(kind, []):
        if isinstance(argument, str):
            result.append(argument)
        elif isinstance(argument, dict) and rules_allow(argument.get("rules"), features, architecture=architecture):
            value = argument.get("value", [])
            values = value if isinstance(value, list) else [value]
            if not all(isinstance(item, str) for item in values):
                raise LauncherError("版本 JSON 包含非文本启动参数。")
            result.extend(values)
    return result


def replace_placeholders(value: str, variables: dict) -> str:
    def replace(match):
        key = match.group(1)
        if key not in variables:
            raise LauncherError(f"版本使用了不支持的参数变量：${{{key}}}")
        return str(variables[key])
    return re.sub(r"\$\{([^}]+)\}", replace, value)


@dataclass(frozen=True)
class JavaInfo:
    path: Path
    major: int
    description: str
    architecture: str = ""
    runtime_path: Path | None = None

    @property
    def launch_path(self) -> Path:
        # Keep the selected path for configuration/UI. Launch the actual VM,
        # not an Oracle javapath helper that spawns a different process/PID.
        return self.runtime_path or self.path


def inspect_java(path: Path) -> JavaInfo:
    try:
        path = Path(path).resolve()
        stat = path.stat()
    except OSError as exc:
        raise LauncherError(f"Java 文件不可用：{path}\n{exc}") from exc
    return _inspect_java_cached(path, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=64)
def _inspect_java_cached(path: Path, modified_ns: int, size: int) -> JavaInfo:
    # File metadata invalidates cached probes after a Java upgrade/replacement.
    try:
        result = subprocess.run([str(path), "-XshowSettings:properties", "-version"],
                                capture_output=True, text=True, errors="replace", timeout=8,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LauncherError(f"无法运行 Java：{path}\n{exc}") from exc
    output = result.stderr + result.stdout
    match = re.search(r'(?:openjdk|java)(?: version)?\s+"?((?:1\.)?\d+[\w.+-]*)', output)
    if not match:
        match = re.search(r"java\.version\s*=\s*(\S+)", output)
    if result.returncode or not match:
        raise LauncherError(f"无法识别 Java 版本：{path}")
    version = match.group(1)
    major = int(version.split(".")[1] if version.startswith("1.") else re.match(r"\d+", version).group())
    arch_match = re.search(r"os\.arch\s*=\s*(\S+)", output)
    description = next((line.strip() for line in output.splitlines() if "version \"" in line), version)
    bits = re.search(r"sun\.arch\.data\.model\s*=\s*(32|64)", output)
    architecture = arch_match.group(1).lower() if arch_match else "x86" if bits and bits.group(1) == "32" else ""
    home_match = re.search(r"(?m)^[ \t]*java\.home\s*=\s*(.+)$", output)
    runtime_path = None
    if home_match:
        candidate = Path(home_match.group(1).strip()) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if candidate.is_file():
            runtime_path = candidate.resolve()
    return JavaInfo(Path(path), major, description, architecture, runtime_path)


def find_java(minecraft_dir: Path | None = None, saved_path="") -> list[JavaInfo]:
    candidates = set()
    path_java = shutil.which("java")
    if path_java:
        candidates.add(Path(path_java))
    for directory in os.get_exec_path():
        path = Path(directory) / ("java.exe" if os.name == "nt" else "java")
        if path.is_file():
            candidates.add(path)
    if saved_path:
        candidates.add(Path(saved_path))
        # Custom installations commonly keep several JDKs next to the saved one.
        saved = Path(saved_path)
        if saved.parent.name.lower() == "bin":
            siblings = saved.parent.parent.parent
            # Inspect just sibling bin/java paths, never recurse through a drive.
            if siblings.is_dir():
                try:
                    for sibling in siblings.iterdir():
                        candidate = sibling / "bin" / ("java.exe" if os.name == "nt" else "java")
                        if candidate.is_file():
                            candidates.add(candidate)
                except OSError:
                    pass
    for variable in ("JAVA_HOME", "JDK_HOME"):
        if os.getenv(variable):
            candidates.add(Path(os.environ[variable]) / "bin" / ("java.exe" if os.name == "nt" else "java"))
    search_dirs = [Path(os.getenv("ProgramFiles", "C:/Program Files")) / name for name in
                   ("Java", "Eclipse Adoptium", "Microsoft", "Amazon Corretto", "Zulu", "BellSoft", "SapMachine")]
    local_programs = Path.home() / "AppData/Local/Programs"
    if local_programs.is_dir():
        try:
            search_dirs.extend(path for path in local_programs.iterdir() if path.is_dir()
                               and any(name in path.name.lower() for name in
                                       ("java", "jdk", "jre", "openjdk", "adoptium", "temurin", "corretto", "zulu", "bellsoft")))
        except OSError:
            pass
    search_dirs.append(Path(os.getenv("APPDATA", str(Path.home()))) / ".minecraft/runtime")
    if minecraft_dir:
        search_dirs.extend([Path(minecraft_dir) / "runtime", Path(minecraft_dir).parent / "runtime",
                            Path(minecraft_dir) / "java"])
    if os.name == "nt":
        # Installers registered in Windows need not live in Program Files.
        import winreg
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for branch in ("Java Runtime Environment", "JRE", "Java Development Kit", "JDK"):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\JavaSoft\\" + branch,
                                        0, winreg.KEY_READ | view) as key:
                        for index in range(winreg.QueryInfoKey(key)[0]):
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as version_key:
                                home = winreg.QueryValueEx(version_key, "JavaHome")[0]
                                candidates.add(Path(home) / "bin/java.exe")
                except OSError:
                    continue
    for directory in dict.fromkeys(search_dirs):
        if directory.is_dir():
            try:
                candidates.update(directory.rglob("java.exe" if os.name == "nt" else "java"))
            except OSError:
                continue
    found, seen = [], set()
    for path in sorted(candidates, key=lambda p: str(p).casefold()):
        try:
            resolved = path.resolve()
            key = os.path.normcase(str(resolved))
            if key not in seen:
                seen.add(key)
                found.append(inspect_java(resolved))
        except (LauncherError, OSError):
            continue
    return found


def get_required_java_version(version: str, data: dict) -> int | None:
    metadata = data.get("javaVersion", {})
    major = metadata.get("majorVersion") if isinstance(metadata, dict) else None
    if major is not None:
        try:
            if int(major) > 0:
                return int(major)
        except (TypeError, ValueError):
            pass
    # Snapshots and renamed modded versions should use their inherited metadata.
    names = [data.get(key) for key in ("minecraftVersion", "clientVersion", "inheritsFrom", "jar", "id")]
    # Forge profiles with renamed IDs often retain --fml.mcVersion.
    arguments = data.get("arguments", {}).get("game", [])
    if "--fml.mcVersion" in arguments:
        index = arguments.index("--fml.mcVersion") + 1
        if index < len(arguments):
            names.insert(0, arguments[index])
    names.append(version)
    match = next((re.match(r"^1\.(\d+)(?:\.(\d+))?(?=$|[-_+ ])", name)
                  for name in names if isinstance(name, str)
                  and re.match(r"^1\.(\d+)(?:\.(\d+))?(?=$|[-_+ ])", name)), None)
    if not match:
        return None
    minor, patch = int(match.group(1)), int(match.group(2) or 0)
    if minor > 20 or minor == 20 and patch >= 5:
        return 21
    if minor >= 18:
        return 17
    return 16 if minor == 17 else 8


def select_best_java(java_infos: list[JavaInfo], required: int | None, memory=4096,
                     preferred_path="", host_arch=None) -> JavaInfo | None:
    if required is None:
        return None
    normalize = lambda arch: {"amd64": "x86_64", "x64": "x86_64", "i386": "x86", "i686": "x86",
                              "aarch64": "arm64"}.get(arch.lower(), arch.lower())
    host = normalize(host_arch or get_host_architecture())
    usable_architectures = ("", host, "x86", "x86_64" if host == "arm64" else host)
    if not host:
        # Successfully inspected runtimes have already run on this machine.
        usable_architectures = ("", "x86", "x86_64", "arm64")
    candidates = [info for info in java_infos if info.major >= required
                  and not (normalize(info.architecture) == "x86" and (memory > 1536 or required >= 21))
                  and normalize(info.architecture) in usable_architectures]
    return min(candidates, key=lambda info: (
        info.major, normalize(info.architecture) != host, not bool(info.architecture),
        os.path.normcase(str(info.path)) != os.path.normcase(str(preferred_path)), str(info.path).casefold()), default=None)


@dataclass(frozen=True)
class Account:
    name: str
    uuid: str
    access_token: str = "0"
    kind: str = "offline"
    xuid: str = ""
    expires_at: float = 0


@dataclass(frozen=True)
class LaunchPlan:
    command: list[str]
    game_directory: Path
    natives_directory: Path
    native_jars: list
    version: str
    asset_files: list[tuple[Path, Path]] = field(default_factory=list)
    window_mode: str = "windowed"
    display_bounds: tuple[int, int, int, int] | None = None


def build_launch_plan(spec: VersionSpec, java: JavaInfo, game_dir: Path,
                      config: dict, account: Account, client_id="", display_bounds=None) -> LaunchPlan:
    if not java.path.is_file():
        raise LauncherError(f"Java 文件不存在：{java.path}")
    if not java.launch_path.is_file():
        raise LauncherError(f"Java 运行文件不存在，请重新扫描 Java：{java.launch_path}")
    required = get_required_java_version(spec.name, spec.data)
    if required and java.major < required:
        raise LauncherError(f"当前为 Java {java.major}，该版本至少需要 Java {required}。")
    if java.architecture in ("x86", "i386") and config["memory"] > 1536:
        raise LauncherError("当前 Java 为 32 位，无法分配所选内存。请选择 64 位 Java 或将内存调至 1536 MB 以下。")
    if not spec.jar_path.is_file():
        raise LauncherError(f"游戏 JAR 不存在：{spec.jar_path}")
    if account.kind == "offline":
        validate_player_name(account.name)
    elif not account.access_token or account.access_token == "0" or not account.name or not account.uuid:
        raise LauncherError("请先登录 Microsoft 并获取 Minecraft 玩家资料。")
    main_class = spec.data.get("mainClass")
    if not isinstance(main_class, str) or not main_class:
        raise LauncherError("版本 JSON 缺少 mainClass。")
    libraries = get_library_jars(spec, java.architecture or None)
    if libraries["missing"]:
        details = "\n".join(f"{name}\n{path}" for name, path in libraries["missing"][:8])
        raise LauncherError(f"缺少 {len(libraries['missing'])} 个依赖文件，请先完整安装该版本。\n\n{details}")
    asset_index = spec.data.get("assetIndex", {})
    asset_name = asset_index.get("id") or spec.data.get("assets", "")
    if asset_name:
        _safe_version_name(asset_name)
    assets_root = next((root / "assets" for root in spec.roots
                        if (root / "assets/indexes" / f"{asset_name}.json").is_file()), spec.roots[-1] / "assets")
    asset_files = []
    if asset_name:
        index_path = assets_root / "indexes" / f"{asset_name}.json"
        index = _read_json(index_path)
        missing_assets = []
        for name, info in index.get("objects", {}).items():
            digest = info.get("hash", "")
            if not re.fullmatch(r"[0-9a-f]{40}", digest):
                raise LauncherError(f"资源索引包含无效哈希：{name}")
            source = assets_root / "objects" / digest[:2] / digest
            if not source.is_file():
                missing_assets.append(name)
            relative = Path(name)
            if relative.is_absolute() or relative.drive or ".." in relative.parts:
                raise LauncherError(f"资源文件路径无效：{name}")
            if index.get("virtual"):
                asset_files.append((source, assets_root / "virtual" / asset_name / relative))
            if index.get("map_to_resources"):
                asset_files.append((source, Path(game_dir) / "resources" / relative))
        if missing_assets:
            raise LauncherError(f"缺少 {len(missing_assets)} 个游戏资源，请先补全 assets。\n"
                                + "\n".join(missing_assets[:8]))
    classpath = os.pathsep.join(str(p.resolve()) for p in [*libraries["normal"], spec.jar_path])
    # Each run gets its own directory so a second launch cannot delete DLLs in use.
    natives_dir = Path(game_dir) / ".dingding/natives" / uuid.uuid4().hex
    window_mode = "borderless" if config["fullscreen"] and CURRENT_OS == "windows" else "fullscreen" if config["fullscreen"] else "windowed"
    # Detect the rendering backend, not the profile name (Forge/OptiFine may
    # rename it). LWJGL 2 uses DPI-virtualized window coordinates on Java 8.
    legacy_lwjgl = any(lib.get("name", "").startswith("org.lwjgl.lwjgl:lwjgl:")
                       for lib in spec.data.get("libraries", []))
    if "minecraftArguments" in spec.data and not any(
            lib.get("name", "").startswith("org.lwjgl:lwjgl:") for lib in spec.data.get("libraries", [])):
        legacy_lwjgl = True
    width, height = config["game_width"], config["game_height"]
    if window_mode == "borderless":
        if legacy_lwjgl:
            # The child's actual DPI context is only known after HWND creation.
            # Start small; the worker expands it using that window's coordinates.
            width, height = min(width, 854), min(height, 480)
        elif display_bounds:
            width, height = display_bounds[2] - display_bounds[0], display_bounds[3] - display_bounds[1]
    variables = {
        "auth_player_name": account.name, "auth_uuid": account.uuid.replace("-", ""),
        "auth_access_token": account.access_token, "user_type": "msa" if account.kind == "microsoft" else "legacy",
        "auth_xuid": account.xuid, "clientid": client_id,
        "version_name": spec.name, "version_type": spec.data.get("type", "release"),
        "game_directory": str(Path(game_dir).resolve()), "assets_root": str(assets_root.resolve()),
        "assets_index_name": asset_name, "game_assets": str((assets_root / "virtual" / asset_name).resolve()),
        "resolution_width": width, "resolution_height": height,
        "natives_directory": str(natives_dir.resolve()), "classpath": classpath,
        "classpath_separator": os.pathsep, "library_directory": str((spec.roots[-1] / "libraries").resolve()),
        "launcher_name": "DINGDING Launcher", "launcher_version": "1.1", "user_properties": "{}",
    }
    features = {"is_demo_user": False, "has_custom_resolution": window_mode != "fullscreen",
                "has_quick_plays_support": False, "is_quick_play_singleplayer": False,
                "is_quick_play_multiplayer": False, "is_quick_play_realms": False}
    jvm = [replace_placeholders(arg, variables) for arg in get_arguments(spec.data, "jvm", features, java.architecture or None)
           if not re.match(r"-Xm[xs]", arg)]
    if window_mode == "borderless" and legacy_lwjgl:
        jvm = [arg for arg in jvm if not arg.startswith("-Dorg.lwjgl.opengl.Window.undecorated=")]
        jvm.append("-Dorg.lwjgl.opengl.Window.undecorated=true")
    if not any(arg.startswith("-Djava.library.path=") for arg in jvm):
        jvm.append(f"-Djava.library.path={natives_dir.resolve()}")
    if "-cp" not in jvm and "-classpath" not in jvm:
        jvm.extend(["-cp", classpath])
    game = get_arguments(spec.data, "game", features, java.architecture or None)
    processed, i = [], 0
    while i < len(game):
        argument = game[i]
        if argument.startswith("--quickPlay") or account.kind == "offline" and argument in ("--xuid", "--clientId"):
            i += 2
            continue
        if argument == "--demo":
            i += 1
            continue
        if argument == "--gameDir" or argument.startswith("--gameDir="):
            i += 2 if argument == "--gameDir" else 1
            continue
        if argument in ("--width", "--height", "--fullscreenWidth", "--fullscreenHeight"):
            i += 2
            continue
        if argument == "--fullscreen":
            i += 1
            continue
        processed.append(replace_placeholders(argument, variables))
        i += 1
    processed.extend(["--gameDir", variables["game_directory"]])
    # Apply resolution even to legacy JSON without feature arguments.
    if window_mode == "fullscreen":
        processed.append("--fullscreen")
    else:
        processed.extend(["--width", str(width), "--height", str(height)])
    memory = config["memory"]
    command = [str(java.launch_path), f"-Xmx{memory}M", f"-Xms{min(memory, 1024)}M", *jvm, main_class, *processed]
    return LaunchPlan(command, Path(game_dir), natives_dir, libraries["native"], spec.name, asset_files,
                      window_mode, display_bounds)


def prepare_legacy_assets(asset_files: list[tuple[Path, Path]]) -> None:
    """Older clients expect named files rather than the content-addressed store."""
    for source, target in asset_files:
        if target.is_file() and target.stat().st_size == source.stat().st_size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def extract_natives(native_jars: list, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    suffixes = {"windows": (".dll",), "linux": (".so",), "osx": (".dylib", ".jnilib")}[CURRENT_OS]
    try:
        for jar, excludes in native_jars:
            with zipfile.ZipFile(jar) as archive:
                for entry in archive.infolist():
                    if entry.is_dir() or any(entry.filename.startswith(prefix) for prefix in excludes):
                        continue
                    if entry.filename.lower().endswith(suffixes):
                        target = directory / Path(entry.filename).name
                        with archive.open(entry) as source, target.open("wb") as stream:
                            shutil.copyfileobj(source, stream)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise LauncherError(f"Native 解压失败：{exc}") from exc


def cleanup_natives(directory: Path, game_dir: Path) -> None:
    expected_parent = (Path(game_dir) / ".dingding/natives").resolve()
    resolved = Path(directory).resolve()
    if resolved.parent == expected_parent and re.fullmatch(r"[0-9a-f]{32}", resolved.name):
        shutil.rmtree(resolved, ignore_errors=True)


def redact_command(command: list[str], secrets=(), compact=False) -> str:
    result, hide_next = [], False
    for argument in command:
        if hide_next:
            result.append("<已隐藏>")
            hide_next = False
            continue
        value = str(argument)
        if compact and result and result[-1] in ("-cp", "-classpath"):
            value = f"<ClassPath: {len(value.split(os.pathsep))} 个文件>"
        if compact and len(value) > 1024:
            value = value[:512] + "…<已省略长参数>"
        for secret in secrets:
            if secret and secret != "0":
                value = value.replace(secret, "<已隐藏>")
        result.append(value)
        hide_next = argument.lower() in ("--accesstoken", "--session", "--clienttoken")
    return subprocess.list2cmdline(result)
