"""逐拍差分引擎。

输入：RTL 仿真波形 + 标准答案（参考模型的逐拍输出）
输出：第一处不同的「周期、信号、期望值、实际值、差异位」，外加最小重现窗口。

这一层是整台仪器的核心。它的价值不在于「能比」，而在于「比得准」：
- 复位期间不参与比对（否则每个设计开局都报一片假错）
- 按周期对齐而不是按时间对齐
- 值的比较是「按位宽、按符号」的数值比较，不是字符串比较
  （字符串比较会把字节序颠倒的真 bug 看成正确）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .clock import CycleAxis, build_axis
from .vcd import Signal, VCD, parse


@dataclass
class Mapping:
    """RTL 信号 ↔ 标准答案字段 的对应关系。"""

    rtl: str          # 波形里的信号名（支持全路径或后缀）
    golden: str       # 标准答案里的字段名
    width: int        # 期望位宽
    signed: bool = False
    note: str = ""


@dataclass
class GoldenTrace:
    """标准答案：每个周期一行的记录表。"""

    fields: dict[str, list[int | float | str]]
    cycles: int
    source: str = ""

    @classmethod
    def from_json(cls, path: str | Path) -> "GoldenTrace":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data, source=str(p))

    @classmethod
    def from_dict(cls, data: dict, source: str = "") -> "GoldenTrace":
        """接受两种格式：

        A. {"cycles": [{"cycle":0, "dout":60}, ...]}   逐周期字典列表
        B. {"dout": [...], "state": [...]}             按字段的数组
        """
        fields: dict[str, list] = {}
        if "cycles" in data:
            rows = data["cycles"]
            for row in rows:
                for k, v in row.items():
                    if k == "cycle":
                        continue
                    fields.setdefault(k, []).append(v)
            n = len(rows)
            # 补齐长度
            for k in fields:
                while len(fields[k]) < n:
                    fields[k].append(None)
        else:
            for k, v in data.items():
                if isinstance(v, list):
                    fields[k] = list(v)
            n = max((len(v) for v in fields.values()), default=0)
        return cls(fields=fields, cycles=n, source=source)


@dataclass
class Mismatch:
    cycle: int
    signal: str
    golden_field: str
    expected: str
    actual: str
    bits: list[int] = field(default_factory=list)   # 不同的位（从 0 开始编号）
    detail: str = ""


@dataclass
class DiffResult:
    status: str                       # "match" / "mismatch" / "error"
    mapping: list[Mapping]
    total_cycles: int
    compared_cycles: int
    compared_values: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)
    window: tuple[int, int] | None = None
    message: str = ""
    clock_period_fs: int = 0
    valid_from: int = 0

    @property
    def first(self) -> Mismatch | None:
        return self.mismatches[0] if self.mismatches else None

    def to_dict(self) -> dict:
        d = {
            "status": self.status,
            "message": self.message,
            "total_cycles": self.total_cycles,
            "compared_cycles": self.compared_cycles,
            "compared_values": self.compared_values,
            "valid_from_cycle": self.valid_from,
            "clock_period_fs": self.clock_period_fs,
            "mismatch_count": len(self.mismatches),
        }
        if self.first:
            m = self.first
            d["first_mismatch"] = {
                "cycle": m.cycle,
                "signal": m.signal,
                "golden_field": m.golden_field,
                "expected": m.expected,
                "actual": m.actual,
                "bits_differing": m.bits,
                "detail": m.detail,
            }
        if self.window:
            d["minimal_window"] = {"start": self.window[0], "end": self.window[1]}
        if self.mismatches:
            d["mismatches"] = [
                {
                    "cycle": m.cycle,
                    "signal": m.signal,
                    "expected": m.expected,
                    "actual": m.actual,
                    "bits_differing": m.bits,
                }
                for m in self.mismatches[:20]
            ]
        return d

    def to_text(self) -> str:
        """给人看的一句话结论。"""
        if self.status == "match":
            return (f"通过：{self.compared_cycles} 个周期逐拍比对全部一致"
                    f"（时钟周期 {self.clock_period_fs} fs，"
                    f"前 {self.valid_from} 拍因复位屏蔽）")
        if self.status == "error":
            return f"比对无法进行：{self.message}"
        m = self.first
        assert m is not None
        head = (f"第 {m.cycle} 拍发现不一致：{m.signal}"
                f"（期望 {m.expected}，实际 {m.actual}"
                f"{'，第 ' + '、'.join(str(b) for b in m.bits) + ' 位不同' if m.bits else ''}）")
        extra = ""
        if len(self.mismatches) > 1:
            extra = f"；后续还有 {len(self.mismatches) - 1} 处不一致"
        win = ""
        if self.window:
            win = f"；最小重现窗口：第 {self.window[0]} 拍 ~ 第 {self.window[1]} 拍"
        return head + extra + win


def _to_bits(value, width: int) -> int | None:
    """把各种写法的值统一成整数，便于按位比较。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value & ((1 << width) - 1)
    if isinstance(value, float):
        return int(value) & ((1 << width) - 1)
    s = str(value).strip().lower()
    if s in ("x", "z", ""):
        return None          # 不定态：无法比较，跳过
    if s.startswith("0x"):
        return int(s, 16) & ((1 << width) - 1)
    if s.startswith("0b"):
        return int(s, 2) & ((1 << width) - 1)
    # VCD 里总线值是不带前缀的二进制串，如 "00111100"
    if set(s) <= set("01"):
        return int(s, 2) & ((1 << width) - 1)
    try:
        return int(s) & ((1 << width) - 1)
    except ValueError:
        return None


def _hex(v: int, width: int) -> str:
    digits = max(1, (width + 3) // 4)
    return f"0x{v:0{digits}X}"


def diff(
    vcd_path: str | Path,
    golden: GoldenTrace,
    mapping: list[Mapping],
    *,
    vcd: VCD | None = None,
    axis: CycleAxis | None = None,
    clock_hint: str | None = None,
    reset_hint: str | None = None,
    sample_edge: str | None = None,
    max_mismatches: int = 20,
    window_pad: int = 20,
) -> DiffResult:
    """执行逐拍差分。"""
    if vcd is None:
        vcd = parse(vcd_path)
    if axis is None:
        axis = build_axis(vcd, clock_hint=clock_hint, reset_hint=reset_hint,
                          sample_edge=sample_edge)

    result = DiffResult(
        status="error",
        mapping=mapping,
        total_cycles=len(axis),
        compared_cycles=0,
        clock_period_fs=axis.clock.period_fs,
        valid_from=axis.valid_from,
    )

    # 解析信号映射
    resolved: list[tuple[Mapping, Signal]] = []
    for mp in mapping:
        hits = vcd.find(mp.rtl)
        if not hits:
            result.message = f"波形里找不到信号 {mp.rtl}"
            return result
        # 多个同短名时优先取位宽匹配的那个（层次化设计里很容易撞名）
        best = None
        for h in hits:
            if h.width == mp.width:
                best = h
                break
        resolved.append((mp, best or hits[0]))

    for mp, _ in resolved:
        if mp.golden not in golden.fields:
            result.message = (f"标准答案里没有字段 {mp.golden}"
                              f"（现有字段：{', '.join(golden.fields) or '空'}）")
            return result

    total = min(len(axis), golden.cycles)
    result.total_cycles = total
    compared_cycles = 0      # 真正逐拍比过的周期数（>=1 个信号有可比值才算）
    compared_values = 0      # 信号 × 周期的比较次数
    mismatches: list[Mismatch] = []

    for cyc in range(axis.valid_from, total):
        t = axis.time_of(cyc)
        cycle_had_comparison = False
        for mp, sig in resolved:
            raw = vcd.values_at(sig, t)
            actual = _to_bits(raw, mp.width)
            expected = _to_bits(golden.fields[mp.golden][cyc], mp.width)
            if actual is None or expected is None:
                continue        # 不定态或缺失，无法判对错
            compared_values += 1
            cycle_had_comparison = True
            if actual != expected:
                xor = actual ^ expected
                bits = [i for i in range(mp.width) if (xor >> i) & 1]
                mismatches.append(Mismatch(
                    cycle=cyc,
                    signal=sig.path,
                    golden_field=mp.golden,
                    expected=_hex(expected, mp.width),
                    actual=_hex(actual, mp.width),
                    bits=bits,
                ))
                if len(mismatches) >= max_mismatches:
                    break
        if cycle_had_comparison:
            compared_cycles += 1
        if len(mismatches) >= max_mismatches:
            break

    result.compared_cycles = compared_cycles
    result.compared_values = compared_values
    result.mismatches = mismatches

    if not mismatches:
        result.status = "match"
        result.message = "全部一致"
        return result

    result.status = "mismatch"
    first = mismatches[0]
    lo = max(axis.valid_from, first.cycle - window_pad)
    hi = min(total - 1, first.cycle + window_pad)
    result.window = (lo, hi)
    result.message = f"首处不一致在第 {first.cycle} 拍"
    return result


def load_mapping(path: str | Path) -> list[Mapping]:
    """从 YAML 或 JSON 读映射配置。

    支持行内注释与转义点号，全路径写起来不必加引号。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        data = _mini_yaml(text)
    else:
        data = json.loads(text)

    items = data.get("map") or data.get("signals") or []
    out: list[Mapping] = []
    for it in items:
        out.append(Mapping(
            rtl=it["rtl"],
            golden=it.get("golden", it["rtl"].split(".")[-1]),
            width=int(it.get("width", 1)),
            signed=bool(it.get("signed", False)),
            note=it.get("note", ""),
        ))
    return out


def _mini_yaml(text: str) -> dict:
    """极简 YAML 读取：只支持本项目配置需要的结构。

    刻意不引入第三方依赖——本工具的目标是零依赖，
    这样任何环境（CI 容器、EDA 服务器）都能直接跑。
    """
    data: dict = {}
    current_list: list | None = None
    current_item: dict | None = None

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if indent == 0:
            if stripped.endswith(":"):
                key = stripped[:-1].strip()
                data[key] = []
                current_list = data[key]
                current_item = None
            elif ":" in stripped:
                k, v = stripped.split(":", 1)
                data[k.strip()] = _scalar(v.strip())
                current_list = None
            continue

        if current_list is None:
            continue
        if stripped.startswith("- "):
            current_item = {}
            current_list.append(current_item)
            rest = stripped[2:].strip()
            if ":" in rest:
                k, v = rest.split(":", 1)
                current_item[k.strip()] = _scalar(v.strip())
        elif stripped.startswith("-"):
            current_item = {}
            current_list.append(current_item)
        elif ":" in stripped and current_item is not None:
            k, v = stripped.split(":", 1)
            current_item[k.strip()] = _scalar(v.strip())
    return data


def _scalar(v: str):
    v = v.strip()
    if v == "":
        return ""
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v
