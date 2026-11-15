"""离线示例识别结果：给仓库内的合成样例图用的固定结构化笔记。

用途只有一个——没有 API Key 时也能演示完整链路（预处理 → 结构规整 → 渲染 → 归档 → 索引/导图/双链）。
内容来自本项目对这两张合成样例图的真实识别记录，不是凭空编写的模板文案。

只按图片文件名（不含扩展名）匹配；没匹配到就退回 recognizer 的通用占位结构。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "sample_note_01": {
        "title": "Biology - Photosynthesis",
        "sections": [
            {
                "heading": "Light reaction",
                "level": 1,
                "content": "Happens in the thylakoid membrane\nSplits water, releases O2\nProduces ATP and NADPH",
                "highlights": ["Produces ATP and NADPH"],
                "subsections": [],
            },
            {
                "heading": "Dark reaction (Calvin cycle)",
                "level": 1,
                "content": "Happens in the stroma\nUses ATP + NADPH to fix CO2\nKey enzyme: RuBisCO",
                "highlights": ["Key enzyme: RuBisCO"],
                "subsections": [],
            },
            {
                "heading": "Exam tip",
                "level": 1,
                "content": "Do not confuse stroma with cytoplasm",
                "highlights": [],
                "subsections": [],
            },
        ],
        "arrows": [{"from": "Light reaction", "to": "Dark reaction (Calvin cycle)", "relation": "推导"}],
        "tags": ["Biology", "Photosynthesis"],
    },
    "sample_note_02": {
        "title": "Calculus - Limits",
        "sections": [
            {
                "heading": "Definition",
                "level": 1,
                "content": "lim f(x) as x approaches a equals L\nLeft limit must equal right limit",
                "highlights": ["Left limit must equal right limit"],
                "subsections": [],
            },
            {
                "heading": "Two important limits",
                "level": 1,
                "content": "sin x / x -> 1 when x -> 0\n(1 + 1/n)^n -> e when n -> infinity",
                "highlights": [],
                "subsections": [],
            },
            {
                "heading": "Common mistake",
                "level": 1,
                "content": "Cancel terms before checking 0/0\nAlways verify the domain first",
                "highlights": [],
                "subsections": [],
            },
        ],
        "arrows": [],
        "tags": ["Calculus", "Limits"],
    },
}


def for_stem(stem: str) -> dict[str, Any] | None:
    """按文件名取一份可安全修改的副本。"""
    preset = PRESETS.get(stem)
    return deepcopy(preset) if preset else None
