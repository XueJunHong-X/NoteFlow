"""桌面版配置：存到用户目录（%APPDATA%\\NoteFlow\\config.json），不写进仓库、不打进 exe。

配置优先级：config.json > 环境变量 > 内置默认值。
API Key 只存在这个文件里，日志与界面都不会完整显示（界面里打码）。

这里是桌面端与命令行共用的唯一配置入口：
- `default_workspace()` 决定默认的收件箱/笔记库位置（D 盘工作区）；
- `AppConfig.to_settings()` 是 AppConfig -> Settings 的唯一映射，界面不再自己拼一套。
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

APP_DIR_NAME = "NoteFlow"

# 约定的默认工作区。打包后如果它不存在（换机或别人的机器），自动退回用户目录。
# 换路径：设环境变量 NOTEFLOW_WORKSPACE 即可（优先级最高）。
PREFERRED_WORKSPACE = Path(
    os.getenv("NOTEFLOW_PREFERRED_WORKSPACE") or "D:/Trae Work/Files/efficiency"
)


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _repo_root() -> Path:
    """开发态仓库根目录；打包后不可靠，所以只用于非 frozen 场景。"""
    return Path(__file__).resolve().parent.parent


def app_data_dir() -> Path:
    """Windows 下为 %APPDATA%\\NoteFlow，其它平台退回 ~/.noteflow。"""
    base = os.getenv("APPDATA") or os.getenv("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / ".noteflow"


# CRYPTPROTECT_UI_FORBIDDEN：后台进程不允许弹出系统保护对话框时必须带上
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DATA_BLOB(ctypes.Structure):
    """crypt32 的 DATA_BLOB：一段内存缓冲区的指针 + 字节长度。"""

    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_protect(data: bytes) -> bytes:
    """Windows DPAPI 加密（CurrentUser 作用域：只有本机当前用户能解开）。"""
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 仅在 Windows 上可用")
    buffer = ctypes.create_string_buffer(bytes(data), len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,  # 描述字符串不使用
        None,  # 附加熵不使用
        None,  # 保留参数
        None,  # 不弹系统提示框
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptProtectData 调用失败，无法加密 API Key")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def _dpapi_unprotect(data: bytes) -> bytes:
    """Windows DPAPI 解密，与 _dpapi_protect 成对使用。"""
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 仅在 Windows 上可用")
    buffer = ctypes.create_string_buffer(bytes(data), len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptUnprotectData 调用失败：密文可能不是本机当前用户加密的，或已损坏")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def _load_install_id() -> str:
    """读取（首次生成）安装标识：config.json 同目录下的 install_id 文件。"""
    path = app_data_dir() / "install_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError as exc:
        logger.warning("install_id 文件写入失败（%s）：%s", path, exc)
    return value


def legacy_workspace() -> Path:
    """P2 及以前的默认工作区（C 盘用户目录），仅用于旧配置迁移。"""
    return Path.home() / APP_DIR_NAME


def default_workspace() -> Path:
    """默认工作区根：优先 D 盘约定目录，其次仓库根，打包后换机退回用户目录。"""
    env = (os.getenv("NOTEFLOW_WORKSPACE") or "").strip()
    if env:
        return Path(env).expanduser()
    if _is_frozen():
        if PREFERRED_WORKSPACE.is_dir():
            return PREFERRED_WORKSPACE
        return legacy_workspace()
    return _repo_root()


def _path_usable(value: str) -> bool:
    """路径所在盘符/根是否存在。换机后旧配置里的 D:\\... 常常已经不存在。"""
    try:
        anchor = Path(value).expanduser().anchor
    except (OSError, ValueError):
        return False
    return bool(anchor) and Path(anchor).exists()


def _is_legacy_path(value: str) -> bool:
    """是否指向旧的 C 盘默认工作区。"""
    text = str(value).strip().lower().replace("\\", "/")
    old = str(legacy_workspace()).strip().lower().replace("\\", "/")
    return bool(text) and text.startswith(old) and old != str(default_workspace()).lower().replace("\\", "/")


INT_FIELDS = ("recognize_concurrency", "cleanup_retention_days")
VALID_CONCURRENCY = (1, 2, 4)
CLEANUP_MODES = ("release", "delete")


def _is_int_field(key: str) -> bool:
    return key in INT_FIELDS


def _normalize_concurrency(value) -> int:
    """只接受 1/2/4；越界或非法值退回默认 2。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 2
    return number if number in VALID_CONCURRENCY else 2


def _normalize_retention(value) -> int:
    """清理记录保留天数：1-365，非法值退回 7。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 7
    return min(max(number, 1), 365)


def _apply_stored_key(config: "AppConfig", raw: dict) -> None:
    """按 config.json 里的 Key 字段恢复内存明文，兼容新旧三种形态。

    - 只有明文 api_key（老版本配置）：读入内存，下一次 save 自动迁移成 DPAPI 密文；
    - 只有 api_key_enc（新版本配置）：DPAPI 解密，失败则置空并提示重新配置；
    - 两者并存：以密文解密结果为准，忽略明文；
    - 都没有：保留环境变量 NOTEFLOW_API_KEY（load 里已先行设置）。
    """
    encrypted = str(raw.get("api_key_enc") or "").strip()
    plaintext = str(raw.get("api_key") or "").strip()
    if encrypted:
        try:
            config.api_key = _dpapi_unprotect(base64.b64decode(encrypted)).decode("utf-8").strip()
            config.api_key_last4 = config.api_key[-4:] if config.api_key else ""
        except Exception as exc:
            # 解密失败（换机/换用户/文件损坏）：置空并引导重新配置，不猜也不崩
            config.api_key = ""
            config.api_key_last4 = ""
            logger.warning("API Key 解密失败，已置空，请到设置里重新填写：%s", exc)
    elif plaintext:
        config.api_key = plaintext


@dataclass
class AppConfig:
    """界面可编辑的配置项。"""

    watch_dir: str = ""
    output_dir: str = ""
    vault_dir: str = ""
    api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen-vl-max"
    api_key: str = ""  # 运行时明文：只在内存里流转，save 时按平台决定落盘形态
    api_key_enc: str = ""  # 持久化：base64(DPAPI 密文)，Windows 下的落盘形态
    api_key_last4: str = ""  # 持久化：Key 末四位，Key 不落盘时掩码显示仍可用
    # 持久化：当前 Key 是否通过过「测试连接」，以及最近一次通过的时间（本地时间文本）
    api_key_verified: bool = False
    api_key_verified_at: str = ""
    offline_mode: bool = False  # 勾选后强制离线示例
    recognize_concurrency: int = 2  # 识别并发：1 / 2 / 4，限流时自动降级
    # 本地缓存清理：默认关闭，必须显式开启；删除还需单独许可
    cleanup_enabled: bool = False
    cleanup_mode: str = "release"  # release / delete
    cleanup_delete_allowed: bool = False
    cleanup_retention_days: int = 7

    def __post_init__(self) -> None:
        workspace = default_workspace()
        if not self.watch_dir:
            self.watch_dir = str(workspace / "inbox")
        if not self.output_dir:
            self.output_dir = str(workspace / "output")
        if not self.vault_dir:
            self.vault_dir = str(workspace / "vault")
        if self.cleanup_mode not in CLEANUP_MODES:
            self.cleanup_mode = "release"
        self.cleanup_retention_days = _normalize_retention(self.cleanup_retention_days)
        # 运行时属性：本机安装标识（config.json 同目录的 install_id 文件）。
        # 不作为 dataclass 字段，asdict 不会带上它，config.json 里也就不会出现。
        self.install_id = _load_install_id()

    @property
    def path(self) -> Path:
        return app_data_dir() / "config.json"

    @property
    def is_offline(self) -> bool:
        """没有 Key 时只能离线，即使没勾复选框。"""
        return self.offline_mode or not self.api_key.strip()

    @classmethod
    def load(cls) -> "AppConfig":
        """读取用户配置；缺字段用默认值补齐，文件损坏时退回默认值而不是崩溃。

        会在实例上附加两个非持久化属性，供界面提示：
        - repaired: list[str] 自动修正过的路径说明
        - load_error: str 配置文件读取/解析失败的中文说明
        """
        config = cls()
        config.repaired = []
        config.load_error = ""

        defaults = cls()
        env_key = (os.getenv("NOTEFLOW_API_KEY") or "").strip()
        if env_key:
            config.api_key = env_key
        config.api_base = (os.getenv("NOTEFLOW_API_BASE") or config.api_base).strip()
        config.model = (os.getenv("NOTEFLOW_MODEL") or config.model).strip()

        raw: dict = {}
        try:
            raw = json.loads(config.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            config.load_error = (
                f"配置文件无法读取（{config.path}）：{exc}。本次已改用默认设置，原文件保留未动。"
            )
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not hasattr(config, key):
                    continue
                if _is_int_field(key):
                    try:
                        setattr(config, key, int(value))
                    except (TypeError, ValueError):
                        pass
                elif isinstance(value, (str, bool)):
                    setattr(config, key, value)

        # API Key：兼容明文（旧版）/ DPAPI 密文（新版）/ 两者并存三种形态
        _apply_stored_key(config, raw if isinstance(raw, dict) else {})

        # 验证状态只对「本机配置文件里保存的 Key」有意义：
        # 换机、解密失败、只有环境变量 Key 或 Key 已被清空时，一律回到未验证。
        has_stored_key = bool(
            str(raw.get("api_key_enc") or "").strip() or str(raw.get("api_key") or "").strip()
        ) if isinstance(raw, dict) else False
        config.api_key_verified = (
            config.api_key_verified is True and bool(config.api_key.strip()) and has_stored_key
        )
        config.api_key_verified_at = str(config.api_key_verified_at or "").strip() if config.api_key_verified else ""

        # 并发数：配置文件 < 环境变量（环境变量用于临时压测/排障）
        config.recognize_concurrency = _normalize_concurrency(
            os.getenv("NOTEFLOW_CONCURRENCY") or config.recognize_concurrency
        )
        if config.cleanup_mode not in CLEANUP_MODES:
            config.cleanup_mode = "release"
        config.cleanup_retention_days = _normalize_retention(config.cleanup_retention_days)

        for key in ("watch_dir", "output_dir", "vault_dir"):
            value = str(getattr(config, key) or "").strip()
            fallback = getattr(defaults, key)
            if not value:
                setattr(config, key, fallback)
                continue
            if _is_legacy_path(value):
                setattr(config, key, fallback)
                config.repaired.append(
                    f"{key} 仍在旧的 C 盘默认目录（{value}），已迁移到 {fallback}"
                )
            elif not _path_usable(value):
                setattr(config, key, fallback)
                config.repaired.append(
                    f"{key} 指向的磁盘在当前电脑不存在（{value}），已改用默认路径：{fallback}"
                )

        config.api_base = (config.api_base or "").strip().rstrip("/")
        config.model = (config.model or "qwen-vl-max").strip()

        if config.repaired:
            try:
                config.save()
            except OSError:
                pass
        return config

    def save(self) -> None:
        """原子写入，避免中途崩溃留下半个 JSON。

        Windows 上 API Key 不落明文：写 api_key_enc（base64 的 DPAPI 密文）与
        api_key_last4，并把明文 api_key 字段从 JSON 里删掉；
        其它平台保持旧行为（明文），DPAPI 加密失败时也回退明文并打 warning。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # asdict 只收 dataclass 字段，install_id 这类运行时属性天然不会进 JSON
        data = asdict(self)
        key = (self.api_key or "").strip()
        if key and sys.platform == "win32":
            try:
                data["api_key_enc"] = base64.b64encode(_dpapi_protect(key.encode("utf-8"))).decode("ascii")
                data["api_key_last4"] = key[-4:]
                data.pop("api_key", None)  # 明文字段彻底不落盘
            except Exception as exc:
                logger.warning("DPAPI 加密失败，本次将以明文保存 API Key：%s", exc)
                data["api_key"] = key
                data["api_key_enc"] = ""
                data["api_key_last4"] = ""
        else:
            if key and sys.platform != "win32":
                logger.warning("当前平台不支持 DPAPI，API Key 将以明文保存。")
            data["api_key"] = key
            data["api_key_enc"] = ""
            data["api_key_last4"] = ""
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def to_settings(self, offline: bool | None = None):
        """AppConfig -> 运行期 Settings 的唯一映射（界面 / 自检都走这里）。"""
        from config import Settings

        settings = Settings.from_env()
        settings.watch_dir = Path(self.watch_dir).expanduser()
        settings.output_dir = Path(self.output_dir).expanduser()
        settings.vault_root = str(Path(self.vault_dir).expanduser())
        settings.api_key = "" if (self.is_offline if offline is None else offline) else self.api_key.strip()
        settings.api_base = (self.api_base or "").strip().rstrip("/")
        settings.model = (self.model or "qwen-vl-max").strip()
        settings.recognize_concurrency = _normalize_concurrency(self.recognize_concurrency)
        settings.log_level = "INFO"
        return settings

    def masked_key(self) -> str:
        """界面展示用：只露末四位（优先用持久化的 last4），避免投屏时泄露。"""
        last4 = (self.api_key_last4 or "").strip()
        if last4:
            return f"sk-****{last4}"
        key = (self.api_key or "").strip()
        if not key:
            return "（未配置）"
        if len(key) <= 10:
            return "*" * len(key)
        return f"sk-****{key[-4:]}"
