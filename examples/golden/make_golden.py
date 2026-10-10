"""生成标准答案：一个正确的 8 位计数器应该输出什么。

这是「参考模型」——用 Python 描述电路的期望行为。

关键纪律：参考模型必须和硬件**逐拍对齐**，包括使能信号的语义。
如果这里忽略 en（使能），那么设计在 en 为低时正确地保持不变，
参考模型却在自增，diff 就会把一大片正确行为报成错误——
这种「参考模型自己写错」造成的假警报，是实际验证里最常见的坑之一。

所以下面显式建模：en 为低时保持不变，en 为高时每拍加一。
时序语义按上升沿采样：第 N 拍的输出 = 第 N 个上升沿**采到的**值。
"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

CYCLES = 40


def golden_series(n_cycles: int, en_high_from: int) -> list[int]:
    """理想计数器的逐拍输出。

    en_high_from: 使能从第几拍开始为高（本测试平台里 en 在第 3 个上升沿被采到）。
    """
    out: list[int] = []
    count = 0
    for i in range(n_cycles):
        if i >= en_high_from:
            count = (count + 1) & 0xFF
        out.append(count)
    return out


def main() -> None:
    # 与 tb_counter8.v 对齐：en 在 t=30000 拉高，第 3 个上升沿(t=35000)才被采到
    counts = golden_series(CYCLES, en_high_from=3)
    rows = [
        {"cycle": i, "count": counts[i], "carry": 1 if counts[i] == 0xFF else 0}
        for i in range(CYCLES)
    ]
    out = {
        "description": "8 位计数器标准答案：en 有效后每拍加一",
        "en_high_from_cycle": 3,
        "cycles": rows,
    }
    dest = Path(__file__).parent / "golden_count.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {dest}")
    print(f"前 12 拍期望值: {counts[:12]}")


if __name__ == "__main__":
    main()
