"""唤起本机外部应用：Obsidian（笔记库）与 ima（腾讯知识库）。

降级顺序固定为「协议 → 可执行文件 → 网页」，每一步失败都返回明确说明，
由界面决定是否回退到「打开文件夹」，不做静默失败。
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote

IMA_PROTOCOL = "ima.copilot"
IMA_WEB = "https://ima.qq.com"
IMA_IMPORT_HINT = (
    "导入方法：在 ima 左侧进入「知识库」→ 新建或打开一个知识库 → 「添加文件」，"
    "选中 NoteFlow 导出的 .md（整库可先导出 zip，解压后批量拖入）。"
)


def _registered(prog_id: str) -> bool:
    """Windows 上判断某个 URI 协议是否已注册（HKCR 合并了 HKCU，够用）。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id):
            return True
    except OSError:
        return False


def _protocol_exe(prog_id: str) -> Path | None:
    """从协议注册项里取出可执行文件路径（命令行的首个路径）。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
            command = str(winreg.QueryValueEx(key, "")[0]).strip()
    except OSError:
        return None
    if command.startswith('"'):
        end = command.find('"', 1)
        candidate = command[1:end] if end != -1 else command[1:]
    else:
        candidate = command.split(" ")[0]
    path = Path(candidate)
    return path if path.exists() else None


def obsidian_uri(vault_dir: Path) -> str:
    """用 path 参数而不是 vault 名。

    同一台机器上可能有多个都叫 vault 的库（NoteFlow 默认目录就叫 vault），
    用名字会打开错的那个，path 是 Obsidian 支持的无歧义写法。
    """
    return "obsidian://open?path=" + quote(str(Path(vault_dir).resolve()))


def open_in_obsidian(vault_dir: Path) -> tuple[bool, str]:
    """唤起 Obsidian 打开指定笔记库，返回 (是否成功, 给用户看的说明)。"""
    vault = Path(vault_dir).expanduser()
    if not _registered("obsidian"):
        return False, (
            "没有检测到已安装的 Obsidian（obsidian:// 协议未注册），已改为打开笔记库文件夹。"
            "装好 Obsidian 并至少打开过一次后即可使用本入口。"
        )
    if not vault.is_dir():
        return False, f"笔记库目录还不存在：{vault}"
    try:
        os.startfile(obsidian_uri(vault))
    except OSError as exc:
        return False, f"唤起 Obsidian 失败：{exc}，已改为打开笔记库文件夹。"
    return True, f"已在 Obsidian 中打开笔记库“{vault.name}”。"


def open_ima() -> tuple[bool, str]:
    """唤起 ima 客户端；没有客户端时退回网页版。"""
    if sys.platform == "win32":
        if _registered(IMA_PROTOCOL):
            try:
                os.startfile(f"{IMA_PROTOCOL}://")
                return True, "已唤起 ima 客户端。" + IMA_IMPORT_HINT
            except OSError:
                pass
        exe = _protocol_exe(IMA_PROTOCOL)
        if exe is not None:
            try:
                subprocess.Popen([str(exe)])
                return True, "已启动 ima 客户端。" + IMA_IMPORT_HINT
            except OSError:
                pass
    try:
        opened = webbrowser.open(IMA_WEB)
    except Exception:  # 浏览器调用失败不抛出，交给下面的兜底提示
        opened = False
    if opened:
        return True, "本机没有可用的 ima 客户端，已打开 ima 网页版。" + IMA_IMPORT_HINT
    return False, (
        "没有找到 ima 客户端，也没能打开 ima 网页版（https://ima.qq.com）。"
        "请检查网络，或先从 ima 官网安装桌面客户端后重试。"
    )
