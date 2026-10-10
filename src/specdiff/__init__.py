"""specdiff —— 按规格检查仿真波形，直接告诉你「第几拍、哪个信号、哪几位错了」。

两条路，各解决一个问题：

    diff    波形 vs 标准答案      —— 你有一份已知正确的数据
    check   波形 vs 规格          —— 你只写下「设计应该怎样」

第二条路不需要参照物，所以设计的第一版就能用；而且它比的是
**规格**而不是另一份实现，能抓住"实现和契约不符"这类
拿两份波形互比永远抓不到的问题。

核心不含任何模型调用，纯确定性计算——快、免费、结果可复现。
"""

__version__ = "0.1.0"

from .clock import (
    ClockInfo,
    CycleAxis,
    ResetInfo,
    build_axis,
    find_clock,
    find_reset,
)
from .diff import (
    DiffResult,
    GoldenTrace,
    Mapping,
    Mismatch,
    diff,
    load_mapping,
)
from .spec import Rule, SpecError, SpecResult, Violation, run_rules
from .specfile import SpecFile, load_spec
from .vcd import VCD, Signal, parse

__all__ = [
    "__version__",
    # 波形层
    "VCD",
    "Signal",
    "parse",
    # 时钟层
    "ClockInfo",
    "ResetInfo",
    "CycleAxis",
    "build_axis",
    "find_clock",
    "find_reset",
    # 差分层
    "DiffResult",
    "GoldenTrace",
    "Mapping",
    "Mismatch",
    "diff",
    "load_mapping",
    # 规格层
    "Rule",
    "Violation",
    "SpecResult",
    "SpecError",
    "run_rules",
    "SpecFile",
    "load_spec",
]
