"""NoteFlow 全局配置。

所有可变项都可以用环境变量覆盖；API Key 只从环境变量读取，绝不写死在代码里。

常用环境变量：
    NOTEFLOW_WATCH_DIR     监听目录（默认 ./inbox）
    NOTEFLOW_OUTPUT_DIR    输出目录（默认 ./output）
    NOTEFLOW_API_KEY       多模态模型的 API Key（必填，除非使用 --mock / --dry-run）
    NOTEFLOW_API_BASE      OpenAI 兼容接口地址（默认阿里云百炼 compatible-mode）
    NOTEFLOW_MODEL         模型名称（默认 qwen-vl-max）
    NOTEFLOW_LOG_LEVEL     日志级别（默认 INFO）
    NOTEFLOW_CONCURRENCY   识别并发数（1/2/4，默认 2；限流时自动降到 1）

P1 归档相关（都有合理默认值，可改配置换组织方式）：
    NOTEFLOW_VAULT_DIR         Obsidian vault 根目录（默认 <output>/vault）
    NOTEFLOW_VAULT_LAYOUT      目录模板，支持 {course} {subject} {document}
                               默认 "{course}"：一门课一个目录；学科只进 frontmatter
    NOTEFLOW_VAULT_ONEDRIVE    设为 on 时自动探测 OneDrive 并写入 <OneDrive>/NoteFlow
    NOTEFLOW_MINDMAP_MODE      思维导图写入方式：inline（同篇笔记）/ file / off
    NOTEFLOW_MOC               是否自动生成课程索引和总览（默认 on）
    NOTEFLOW_PDF_TEXT_MIN_CHARS  PDF 文字层视为「可读文本」的最少字符数（默认 40）
    NOTEFLOW_PDF_IMAGE_COVERAGE  页面图片覆盖率超过该值时仍走视觉识别（默认 0.2）

P2 跨笔记双链（全本地规则，不额外调用模型）：
    NOTEFLOW_LINKS             是否启用跨笔记链接（默认 on）
    NOTEFLOW_LINK_MIN_SCORE    链接最低权重，越高越保守（默认 3）
    NOTEFLOW_LINK_MAX_PER_NOTE 单篇笔记最多注入几条链接（默认 4）
    NOTEFLOW_LINK_MIN_NOTES    vault 少于该篇数不做跨笔记链接（冷启动保护，默认 3）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 支持监听的图片格式（OpenCV 可直接读取的类型）
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

# 支持监听的文档格式（需先拆成页面图片）
DOCUMENT_EXTENSIONS = (".pdf", ".goodnotes")

# 推断不出学科时的兜底目录名
UNCLASSIFIED = "未分类"

# vault 目录模板默认值：一门课 / 一个笔记本一个目录；学科只作为 frontmatter 标签
DEFAULT_VAULT_LAYOUT = "{course}"

# 课程名 -> 学科。命中即可自动分学科；匹配不到就落「未分类」。
# 用户可以直接改这张表来适配自己的专业设置。
DEFAULT_SUBJECT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "数学": (
        "高等数学", "数学分析", "线性代数", "概率论与数理统计", "概率论",
        "复变函数", "离散数学", "数值分析", "常微分方程", "偏微分方程",
    ),
    "物理": ("大学物理", "普通物理", "力学", "电磁学", "光学", "热力学", "量子力学"),
    "化学": ("无机化学", "有机化学", "物理化学", "分析化学", "结构化学"),
    "计算机": (
        "数据结构", "操作系统", "计算机网络", "数据库系统", "数据库",
        "编译原理", "计算机组成原理", "算法设计与分析", "机器学习", "深度学习",
    ),
    "信息与通信工程": (
        "通信工程", "通信原理", "信号与系统", "数字信号处理", "模拟电子技术",
        "数字电子技术", "电路分析", "电磁场与电磁波", "信息论",
    ),
    "机械工程": (
        "工程制图", "机械设计", "机械原理", "理论力学", "材料力学",
        "机械制造技术", "公差与配合",
    ),
    "经济管理": ("微观经济学", "宏观经济学", "管理学", "会计学", "金融学", "统计学", "运筹学"),
    "外语": ("大学英语", "英语", "日语", "德语", "法语"),
    "思政": ("马克思主义基本原理", "毛泽东思想", "思想道德与法治", "近代史纲要"),
}

# 概念别名表：键是规范名，值是同一概念的其他写法。
# 自动别名（中文名 + 括号缩写，如「嵌入式微控制器 (MCU)」）之外，需要人工归一的写这里。
DEFAULT_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    # "导数": ("n 阶导数", "n阶导数公式"),
}

# 这些词单独出现时不构成概念（太泛，连出去全是噪音）。精确匹配，不影响「嵌入式系统」这类复合词。
CONCEPT_STOPWORDS: frozenset[str] = frozenset(
    {
        "方法", "问题", "内容", "特点", "优点", "缺点", "作用", "意义", "应用", "注意",
        "例如", "如图", "分析", "步骤", "过程", "结构", "关系", "系统", "定义", "性质",
        "定理", "公式", "结论", "小结", "总结", "引言", "概述", "复习", "思考", "练习",
        "作业", "举例", "说明", "证明", "解答", "目的", "要求", "原理", "基础", "概述",
    }
)



def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in ("0", "false", "off", "no")


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return Path(value.strip()).expanduser()


def _clamp_concurrency(value: int) -> int:
    """并发数只允许在 1..4；非法值退回 2（默认），避免一次开出过多请求。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 2
    if number < 1:
        return 1
    return min(number, 4)


def load_dotenv(path: Path) -> None:
    """加载项目根目录的 .env（KEY=VALUE），已存在的环境变量优先。"""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _native_path(path: Path) -> Path:
    """Windows 长路径：加 \\\\?\\ 前缀后 mkdir/读写都不受 260 字符限制。"""
    if os.name != "nt":
        return path
    text = str(path)
    if text.startswith("\\\\?\\"):
        return path
    try:
        absolute = str(path.resolve())
    except OSError:
        absolute = str(path.absolute())
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


@dataclass
class Settings:
    """运行期配置。"""

    watch_dir: Path
    output_dir: Path
    api_key: str
    api_base: str
    model: str
    request_timeout: int = 60
    max_retries: int = 2
    # 识别并发：1=串行，2/4=并行。遇到 429 会自动降到 1，稳定后再逐步升回。
    recognize_concurrency: int = 2
    max_image_side: int = 1800
    jpeg_quality: int = 90
    log_level: str = "INFO"
    image_extensions: tuple[str, ...] = field(default=IMAGE_EXTENSIONS)

    # ---- P1 归档 ----
    vault_root: str = ""
    vault_layout: str = DEFAULT_VAULT_LAYOUT
    vault_onedrive: bool = False
    mindmap_mode: str = "inline"
    moc_enabled: bool = True
    pdf_text_min_chars: int = 40
    pdf_image_coverage: float = 0.2
    # 默认在处理完一页后清掉拆页/预处理中间图，避免大文档把临时盘撑满；
    # 需要留档排查时设 NOTEFLOW_KEEP_TEMP=1
    keep_temp: bool = False
    subject_keywords: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_SUBJECT_KEYWORDS)
    )

    # ---- P2 跨笔记关联 ----
    link_enabled: bool = True
    link_min_score: int = 3
    link_max_per_note: int = 4
    link_min_notes: int = 3
    concept_min_len: int = 2
    concept_max_len: int = 20
    concept_aliases: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_CONCEPT_ALIASES)
    )
    concept_stopwords: frozenset[str] = CONCEPT_STOPWORDS

    # ---- 派生路径 ----
    @property
    def notes_dir(self) -> Path:
        return self.output_dir / "notes"

    @property
    def vault_dir(self) -> Path:
        """Obsidian vault 根目录；可用 NOTEFLOW_VAULT_DIR 指向已有 vault。"""
        if self.vault_root:
            return Path(self.vault_root).expanduser()
        return self.output_dir / "vault"

    @property
    def preprocessed_dir(self) -> Path:
        return self.output_dir / "preprocessed"

    @property
    def pages_dir(self) -> Path:
        """PDF / .goodnotes 拆页后的中间图片目录。"""
        return self.output_dir / "pages"

    @property
    def failure_log(self) -> Path:
        """失败留痕：每行一条 JSON，便于事后排查跳过的文件/页面。"""
        return self.output_dir / "failures.jsonl"

    @property
    def supported_extensions(self) -> tuple[str, ...]:
        return IMAGE_EXTENSIONS + DOCUMENT_EXTENSIONS

    @property
    def state_file(self) -> Path:
        return self.output_dir / ".noteflow_state.json"

    def ensure_dirs(self) -> None:
        """创建监听、输出与 vault 所需目录（vault 根超 260 字符时走扩展路径）。"""
        for path in (self.watch_dir, self.output_dir, self.notes_dir, self.preprocessed_dir, self.vault_dir):
            _native_path(path).mkdir(parents=True, exist_ok=True)

    def require_api(self) -> None:
        """识别前校验接口配置，缺失时给出可操作的提示。"""
        if not self.api_key:
            raise RuntimeError(
                "未设置 NOTEFLOW_API_KEY，无法调用多模态模型。\n"
                "可先设置环境变量后重试，或使用 --mock（离线自检）/ --dry-run（仅预处理）。"
            )

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(BASE_DIR / ".env")
        return cls(
            watch_dir=_env_path("NOTEFLOW_WATCH_DIR", BASE_DIR / "inbox"),
            output_dir=_env_path("NOTEFLOW_OUTPUT_DIR", BASE_DIR / "output"),
            api_key=_env_str("NOTEFLOW_API_KEY", ""),
            api_base=_env_str("NOTEFLOW_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/"),
            model=_env_str("NOTEFLOW_MODEL", "qwen-vl-max"),
            request_timeout=_env_int("NOTEFLOW_TIMEOUT", 60),
            max_retries=_env_int("NOTEFLOW_MAX_RETRIES", 2),
            recognize_concurrency=_clamp_concurrency(_env_int("NOTEFLOW_CONCURRENCY", 2)),
            max_image_side=_env_int("NOTEFLOW_MAX_IMAGE_SIDE", 1800),
            jpeg_quality=_env_int("NOTEFLOW_JPEG_QUALITY", 90),
            keep_temp=_env_bool("NOTEFLOW_KEEP_TEMP", False),
            log_level=_env_str("NOTEFLOW_LOG_LEVEL", "INFO").upper(),
            vault_root=_env_str("NOTEFLOW_VAULT_DIR", ""),
            vault_layout=_env_str("NOTEFLOW_VAULT_LAYOUT", DEFAULT_VAULT_LAYOUT),
            vault_onedrive=_env_bool("NOTEFLOW_VAULT_ONEDRIVE", False),
            mindmap_mode=_env_str("NOTEFLOW_MINDMAP_MODE", "inline").lower(),
            moc_enabled=_env_bool("NOTEFLOW_MOC", True),
            pdf_text_min_chars=_env_int("NOTEFLOW_PDF_TEXT_MIN_CHARS", 40),
            pdf_image_coverage=_env_float("NOTEFLOW_PDF_IMAGE_COVERAGE", 0.2),
            link_enabled=_env_bool("NOTEFLOW_LINKS", True),
            link_min_score=_env_int("NOTEFLOW_LINK_MIN_SCORE", 3),
            link_max_per_note=_env_int("NOTEFLOW_LINK_MAX_PER_NOTE", 4),
            link_min_notes=_env_int("NOTEFLOW_LINK_MIN_NOTES", 3),
        )


def setup_logging(level: str = "INFO") -> None:
    """统一日志格式，便于在终端观察流水线进度。"""
    import logging

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
