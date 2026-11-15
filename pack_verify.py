"""NoteFlow 打包验收：红线核对 + 打包版三项验收 + 体积/启动计时 + 结论表。

设计约束（对应任务要求）：
- 可重复运行：临时目录每次清空重建，全部判定基于退出码 / 报告文件 / 产物文件。
- 不依赖人工看截图：本脚本不生成、不读取任何图片。
- 凭据只经环境变量传给子进程，从不打印、从不写入结果文件。
- 任一项不通过：打印哪一项、为什么，并以非零退出码结束。
- 明细写入固定位置：仓库根 package_report.json。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 产物目录：默认沿用备赛目录，可用环境变量 NOTEFLOW_DIST_ROOT 覆盖
DIST_ROOT = Path(os.getenv("NOTEFLOW_DIST_ROOT") or (ROOT / "2026-11-11/note-flow-competition/outputs"))
OUT_DIR = DIST_ROOT / "NoteFlow"
EXE = OUT_DIR / "NoteFlow.exe"
REPORT = ROOT / "package_report.json"
ENV_FILE = ROOT / ".env"
REAL_DIR = ROOT / "tests" / "sample_images" / "real"
SYNTHETIC_DIR = ROOT / "tests" / "sample_images"
SCRATCH = Path(tempfile.gettempdir()) / "noteflow_pack_verify"
TITLE = "NoteFlow · 手写笔记智能整理"
KEY_RE = re.compile(rb"sk-[A-Za-z0-9]{16,}")

checks: list[dict] = []


def record(tag: str, item: str, ok: bool, detail: str) -> bool:
    checks.append({"tag": tag, "item": item, "ok": bool(ok), "detail": detail})
    return bool(ok)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def env_credentials() -> dict[str, str]:
    """从 .env 读凭据（不打印）。"""
    creds: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            name, sep, value = line.partition("=")
            if sep and value.strip():
                creds[name.strip()] = value.strip().strip('"').strip("'")
    return creds


def fresh_env(tag: str, with_key: bool = False):
    base = SCRATCH / tag
    if base.exists():
        shutil.rmtree(base)
    appdata = base / "appdata"
    workspace = base / "workspace"
    inbox = workspace / "inbox"
    appdata.mkdir(parents=True)
    inbox.mkdir(parents=True)
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("NOTEFLOW_"):
            env.pop(name, None)
    env["APPDATA"] = str(appdata)
    env["NOTEFLOW_WORKSPACE"] = str(workspace)
    if with_key:
        creds = env_credentials()
        for name in ("NOTEFLOW_API_KEY", "NOTEFLOW_API_BASE", "NOTEFLOW_MODEL"):
            if creds.get(name):
                env[name] = creds[name]
    return env, workspace, inbox


def run_exe(args: list[str], env: dict, timeout: int):
    return subprocess.run([str(EXE), *args], env=env, capture_output=True, timeout=timeout)


def note_files(vault: Path) -> list[Path]:
    """vault 里的笔记 md（剔除课程总览与索引页）。"""
    if not vault.is_dir():
        return []
    return [
        p
        for p in sorted(vault.rglob("*.md"))
        if p.name != "课程总览.md" and "索引" not in p.name
    ]


# ---------------------------------------------------------------- 红线核对
def check_redline() -> None:
    real_hashes: set[str] = set()
    if REAL_DIR.is_dir():
        for path in REAL_DIR.rglob("*"):
            if path.is_file():
                real_hashes.add(sha256(path.read_bytes()))
    creds = env_credentials()
    key_literals = [v for k, v in creds.items() if "KEY" in k.upper() and len(v) > 8]

    hits = {"real": [], "env": [], "config": [], "key": []}
    for path in OUT_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(OUT_DIR))
        parts = [p.lower() for p in path.parts]
        if "real" in parts:
            hits["real"].append(rel)
        name = path.name.lower()
        if name == ".env" or name.endswith(".env"):
            hits["env"].append(rel)
        if name == "config.json":
            hits["config"].append(rel)
        data = path.read_bytes()
        if sha256(data) in real_hashes:
            hits["real"].append(rel)
        if KEY_RE.search(data):
            hits["key"].append(rel)
        for literal in key_literals:
            if literal.encode("utf-8") in data:
                hits["key"].append(rel)
                break

    synthetic = {sha256(p.read_bytes()): p.name for p in SYNTHETIC_DIR.glob("*.png")}
    sample_dir = OUT_DIR / "_internal" / "sample_images"
    packed = [p for p in sample_dir.iterdir() if p.is_file()] if sample_dir.is_dir() else []
    sample_ok = bool(packed) and all(sha256(p.read_bytes()) in synthetic for p in packed)

    record("红线", "真实笔记未进包", not hits["real"], f"命中 {len(hits['real'])} 个文件" + ("：" + "、".join(hits["real"][:3]) if hits["real"] else "（按内容哈希与路径双重比对）"))
    record("红线", ".env 未进包", not hits["env"], f"命中 {len(hits['env'])} 个" + ("：" + "、".join(hits["env"][:3]) if hits["env"] else ""))
    record("红线", "config.json 未进包", not hits["config"], f"命中 {len(hits['config'])} 个" + ("：" + "、".join(hits["config"][:3]) if hits["config"] else ""))
    record("红线", "Key 形态字符串未进包", not hits["key"], f"命中 {len(hits['key'])} 个" + ("：" + "、".join(hits["key"][:3]) if hits["key"] else "（正则 + 本机凭据字面双重扫描全包文件）"))
    record("红线", "样例仅为合成图", sample_ok, f"包内样例 {len(packed)} 张，全部与合成样例哈希一致" if sample_ok else f"包内样例 {len(packed)} 张，存在非合成样例")


# ---------------------------------------------------------------- 三项验收
def check_selftest() -> None:
    env, workspace, _ = fresh_env("selftest")
    started = time.time()
    proc = run_exe(["--selftest"], env, 300)
    elapsed = round(time.time() - started, 1)
    report_file = workspace / "output" / "selftest" / "selftest.json"
    data: dict = {}
    if report_file.is_file():
        try:
            data = json.loads(report_file.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    notes = data.get("notes", [])
    ok = proc.returncode == 0 and data.get("ok") is True
    record("验收", "--selftest", ok, f"退出码 {proc.returncode}，{len(notes)} 篇笔记，{elapsed}s（隔离 APPDATA 运行）")


def check_offline_demo() -> None:
    env, workspace, inbox = fresh_env("demo")
    for png in SYNTHETIC_DIR.glob("*.png"):
        shutil.copy2(png, inbox / png.name)
    started = time.time()
    proc = run_exe(
        [
            "--cli",
            "--once",
            "--mock",
            "--watch-dir",
            str(inbox),
            "--output-dir",
            str(workspace / "out"),
            "--vault-dir",
            str(workspace / "vault"),
        ],
        env,
        300,
    )
    elapsed = round(time.time() - started, 1)
    notes = note_files(workspace / "vault")
    ok = proc.returncode == 0 and len(notes) == 2
    record("验收", "离线演示（2 页）", ok, f"退出码 {proc.returncode}，产出 {len(notes)} 篇笔记（预期 2），{elapsed}s")


def check_real_page() -> None:
    creds = env_credentials()
    if not creds.get("NOTEFLOW_API_KEY"):
        record("验收", "真实单页识别", False, "本机 .env 未配置凭据，无法执行（这不是打包缺陷，但按验收口径记为不通过）")
        return
    env, workspace, inbox = fresh_env("real", with_key=True)
    shutil.copy2(SYNTHETIC_DIR / "sample_note_01.png", inbox / "sample_note_01.png")
    started = time.time()
    proc = run_exe(
        [
            "--cli",
            "--once",
            "--max-pages",
            "1",
            "--watch-dir",
            str(inbox),
            "--output-dir",
            str(workspace / "out"),
            "--vault-dir",
            str(workspace / "vault"),
        ],
        env,
        300,
    )
    elapsed = round(time.time() - started, 1)
    notes = note_files(workspace / "vault")
    frontmatter = bool(notes) and notes[0].read_text(encoding="utf-8", errors="ignore").startswith("---")
    ok = proc.returncode == 0 and len(notes) >= 1 and frontmatter
    record("验收", "真实单页识别", ok, f"退出码 {proc.returncode}，{len(notes)} 篇笔记，frontmatter={frontmatter}，{elapsed}s")


# ---------------------------------------------------------------- 体积与启动
def dir_size_mb(path: Path) -> float:
    return round(sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1e6, 1)


def measure_startup(tag: str):
    user32 = ctypes.windll.user32
    env, workspace, _ = fresh_env(f"boot_{tag}")
    began = time.perf_counter()
    proc = subprocess.Popen([str(EXE)], env=env)
    handle = 0
    while time.perf_counter() - began < 90:
        handle = user32.FindWindowW(None, TITLE)
        if handle:
            break
        time.sleep(0.04)
    sec = round(time.perf_counter() - began, 2)
    if handle:
        user32.PostMessageW(handle, 0x0010, 0, 0)
    try:
        proc.wait(20)
    except subprocess.TimeoutExpired:
        proc.kill()
    return sec if handle else None


# ---------------------------------------------------------------- 主流程
def main() -> int:
    # 防止执行中途系统休眠（计时与验收都要连续运行）
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    if not EXE.is_file():
        print(f"找不到产物：{EXE}")
        return 1

    size_mb = dir_size_mb(OUT_DIR)
    exe_mb = round(EXE.stat().st_size / 1e6, 1)
    built_at = datetime.fromtimestamp(EXE.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    check_redline()
    check_selftest()
    check_offline_demo()
    check_real_page()

    cold = measure_startup("cold")
    warm = measure_startup("warm")
    record("统计", "体积", True, f"整目录 {size_mb} MB（NoteFlow.exe {exe_mb} MB）")
    record("统计", "冷启动（主窗口出现）", cold is not None, f"{cold}s" if cold else "90 秒内未见主窗口")
    record("统计", "热启动（连续第二次）", warm is not None, f"{warm}s" if warm else "90 秒内未见主窗口")

    all_ok = all(c["ok"] for c in checks)
    verdict = "可拷" if all_ok else "不可拷"
    failed = [c for c in checks if not c["ok"]]

    print()
    print("=" * 62)
    print(f"NoteFlow 打包验收  ·  产物 {OUT_DIR}")
    print(f"体积 {size_mb} MB  ·  exe 构建于 {built_at}")
    print("-" * 62)
    for c in checks:
        mark = "通过" if c["ok"] else "不通过"
        print(f"[{c['tag']}] {c['item']}  {mark}  {c['detail']}")
    print("-" * 62)
    if failed:
        print("未通过项：")
        for c in failed:
            print(f"  · [{c['tag']}] {c['item']} —— {c['detail']}")
    print(f"最终判定：{verdict}")
    print(f"明细：{REPORT}")
    print("=" * 62)

    REPORT.write_text(
        json.dumps(
            {
                "verdict": verdict,
                "ok": all_ok,
                "artifact": {
                    "path": str(OUT_DIR),
                    "exe": str(EXE),
                    "size_mb": size_mb,
                    "exe_mb": exe_mb,
                    "built_at": built_at,
                },
                "startup": {"cold_s": cold, "warm_s": warm},
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
