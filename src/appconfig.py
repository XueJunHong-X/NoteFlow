"""桌面版配置：存到用户目录（%APPDATA%\\NoteFlow\\config.json），不写进仓库、不打进 exe。

配置优先级：config.json > 环境变量 > 内置默认值。
API Key 只存在这个文件里，日志与界面都不会完整显示（界面里打码）。

这里是桌面端与命令行共用的唯一配置入口：
- `default_workspace()` 决定默认的收件箱/笔记库位置（D 盘工作区）；
- `AppConfig.to_settings()` 是 AppConfig -> Settings 的唯一映射，界面不再自己拼一套。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

APP_DIR_NAME = "NoteFlow"

# 这台机器上约定的默认工作区。打包后如果它不存在（换机），退回用户目录。
PREFERRED_WORKSPACE = Path("D:/Trae Work/Files/efficiency")


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


INT_FIELDS = ("recognize_concurrency",)
VALID_CONCURRENCY = (1, 2, 4)


def _is_int_field(key: str) -> bool:
    return key in INT_FIELDS


def _normalize_concurrency(value) -> int:
    """只接受 1/2/4；越界或非法值退回默认 2。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 2
    return number if number in VALID_CONCURRENCY else 2


@dataclass
class AppConfig:
    """界面可编辑的配置项。"""

    watch_dir: str = ""
    output_dir: str = ""
    vault_dir: str = ""
    api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen-vl-max"
    api_key: str = ""
    offline_mode: bool = False  # 勾选后强制离线示例
    recognize_concurrency: int = 2  # 识别并发：1 / 2 / 4，限流时自动降级

    def __post_init__(self) -> None:
        workspace = default_workspace()
        if not self.watch_dir:
            self.watch_dir = str(workspace / "inbox")
        if not self.output_dir:
            self.output_dir = str(workspace / "output")
        if not self.vault_dir:
            self.vault_dir = str(workspace / "vault")

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

        # 并发数：配置文件 < 环境变量（环境变量用于临时压测/排障）
        config.recognize_concurrency = _normalize_concurrency(
            os.getenv("NOTEFLOW_CONCURRENCY") or config.recognize_concurrency
        )

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
        """原子写入，避免中途崩溃留下半个 JSON。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
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
        """界面展示用：只露头尾，避免投屏时泄露。"""
        if not self.api_key:
            return "（未配置）"
        if len(self.api_key) <= 10:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}…{self.api_key[-4:]}"
