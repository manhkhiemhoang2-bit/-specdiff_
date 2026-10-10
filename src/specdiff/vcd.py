"""VCD (Value Change Dump) 解析器。

波形文件本质是纯文本：头部声明信号表，正文是「某时刻某信号变成某值」的事件流。
本模块只负责一件事：把事件流读进来，变成一个可以随时查询的结构。

设计要点（每一条都对应真实波形里会遇到的坑）：
1. 流式读取，不把整个文件读进内存（真实波形动辄几百 MB）。
2. 保留同一时间戳内的多次跳变（IEEE 1364 允许，仿真器在 delta cycle 里会产生）。
3. 区分「真实跳变」与「重发当前值」（$dumpall/$dumpon 会重发，不该算作变化）。
4. 四态逻辑 0/1/x/z 原样保留，不擅自当成 0 或 1。
5. 支持实数信号（r 开头的记录），它与位向量比较方式不同。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# VCD 标识符是 ASCII 33-126 的可打印字符，仿真器自行分配，必须读头部才知道对应哪个信号
_ID_CHARS = set(chr(c) for c in range(33, 127))

_VAR_RE = re.compile(
    r"\$var\s+(?P<type>\w+)\s+(?P<width>\d+)\s+(?P<id>\S+)\s+(?P<name>\S+)"
)
# 位范围可能写作 [7:0]（总线）或 [7]（被拆开的单个位），也可能紧贴名字或分开写
_RANGE_RE = re.compile(r"\[(?P<msb>-?\d+)(?:\s*:\s*(?P<lsb>-?\d+))?\]")
_TIMESCALE_RE = re.compile(
    r"\$timescale\s+(?P<mag>\d+)\s*(?P<unit>fs|ps|ns|us|ms|s)\s*\$end"
)
_SCOPE_RE = re.compile(r"\$scope\s+(?P<type>\w+)\s+(?P<name>\S+)\s*\$end")

# 时间单位换算成飞秒（最小公约单位），便于统一比较
_UNIT_FS = {"fs": 1, "ps": 1_000, "ns": 1_000_000, "us": 1_000_000_000,
            "ms": 1_000_000_000_000, "s": 1_000_000_000_000_000}


@dataclass
class Signal:
    """一根本信号线的声明信息。"""

    id: str                      # 仿真器分配的短代号，如 "!" 
    name: str                    # 信号名，如 "dout"
    path: str                    # 全路径，如 "tb.dut.dout"
    width: int                   # 位宽；实数信号为 1
    kind: str                    # wire / reg / real / integer ...
    msb: int | None = None       # 声明的最高位，如 [7:0] 里的 7
    lsb: int | None = None       # 声明的最低位，如 [7:0] 里的 0
    slice_index: int | None = None   # 非 None 表示这是总线被拆开的某一位

    @property
    def is_real(self) -> bool:
        return self.kind == "real" or self.kind == "realtime"

    @property
    def is_slice(self) -> bool:
        """是否是「被仿真器拆成单根」的位切片，如 count [3]。"""
        return self.slice_index is not None

    @property
    def is_vector(self) -> bool:
        """是否是多位总线。1 位信号在 VCD 里用单字符记录，与总线写法不同。"""
        return (not self.is_real) and self.width > 1

    @property
    def declared_msb(self) -> int:
        if self.msb is not None and self.lsb is not None:
            return max(self.msb, self.lsb)
        return self.width - 1

    @property
    def declared_lsb(self) -> int:
        if self.msb is not None and self.lsb is not None:
            return min(self.msb, self.lsb)
        return 0

    def __str__(self) -> str:
        return f"{self.path}[{self.width}]"


@dataclass
class Change:
    """一次值变化事件。"""

    time: int        # 统一换算成飞秒后的时间戳
    signal: Signal
    value: str       # 原始值的字符串形式，如 "00111100" / "1" / "3.14" / "x"
    duplicate: bool = False   # 是否为「重发当前值」的伪事件


@dataclass
class VCD:
    """解析结果：信号表 + 事件流。"""

    path: Path
    timescale_fs: int = 1
    signals: dict[str, Signal] = field(default_factory=dict)   # 按全路径索引
    by_id: dict[str, Signal] = field(default_factory=dict)     # 按短代号索引
    # 被仿真器拆成单根的位切片： 路径 -> {位号: Signal}
    slices: dict[str, dict[int, Signal]] = field(default_factory=dict)
    scopes: list[str] = field(default_factory=list)

    # 每个信号的变化序列： [(时间, 值)]，按时间升序
    traces: dict[str, list[tuple[int, str]]] = field(default_factory=dict)

    time_min: int = 0
    time_max: int = 0
    change_count: int = 0        # 真实变化数（不含伪事件）
    duplicate_count: int = 0     # 伪事件数

    def find(self, pattern: str) -> list[Signal]:
        """按名字找信号。支持精确全路径、精确短名、后缀匹配、通配符。

        层次化设计里同一个短名可能出现在多个层，所以后缀匹配可能返回多个，
        调用方需要自己决定用哪个（或者用全路径消歧）。
        """
        pat = pattern.strip()
        if pat in self.signals:
            return [self.signals[pat]]

        # 短名精确匹配
        exact = [s for s in self.signals.values() if s.name == pat]
        if exact:
            return exact

        # 通配符
        if "*" in pat or "?" in pat:
            import fnmatch

            return [s for s in self.signals.values() if fnmatch.fnmatch(s.path, pat)]

        # 后缀匹配：".dout" 或 "dut.dout"
        needle = pat if pat.startswith(".") else "." + pat
        return [s for s in self.signals.values() if s.path.endswith(needle)]

    def trace_of(self, signal: Signal | str) -> list[tuple[int, str]]:
        """取某信号完整的变化序列。

        外部代码一律走这个方法，不要自己去翻 traces 字典——
        存储键对位切片做了特殊处理，直接按路径取会撞车。
        """
        sig = self._resolve(signal)
        if sig is None:
            return []
        return self.traces.get(sig_key(sig), [])

    def values_at(self, signal: Signal | str, time: int) -> str | None:
        """查询某信号在某时刻的值（取该时刻或之前最后一次变化的值）。"""
        sig = self._resolve(signal)
        if sig is None:
            return None
        trace = self.traces.get(sig_key(sig))
        if not trace:
            return None
        # 二分找最后一个 time <= 目标 的位置
        lo, hi = 0, len(trace) - 1
        ans = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if trace[mid][0] <= time:
                ans = trace[mid][1]
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    def changes_in(self, signal: Signal | str, start: int, end: int) -> list[tuple[int, str]]:
        """取某信号在时间窗内的所有变化。"""
        sig = self._resolve(signal)
        if sig is None:
            return []
        trace = self.traces.get(sig_key(sig), [])
        return [(t, v) for (t, v) in trace if start <= t <= end]

    def _resolve(self, signal: Signal | str) -> Signal | None:
        if isinstance(signal, Signal):
            return signal
        hits = self.find(signal)
        return hits[0] if hits else None

    def summary(self) -> dict:
        return {
            "file": str(self.path),
            "timescale_fs": self.timescale_fs,
            "signals": len(self.signals),
            "changes": self.change_count,
            "duplicate_events": self.duplicate_count,
            "time_min": self.time_min,
            "time_max": self.time_max,
            "time_min_fs": self.time_min,
            "time_max_fs": self.time_max,
            "scopes": self.scopes,
        }


def parse(path: str | Path, *, keep_duplicates: bool = False) -> VCD:
    """解析 VCD 文件。

    keep_duplicates=False 时丢弃「重发当前值」的伪事件：它们不改变电路状态，
    留着会污染「跳变次数」这类统计（真实波形里 $dumpall 会产生大量这种记录）。
    """
    p = Path(path)
    vcd = VCD(path=p)
    stack: list[str] = []
    header_done = False
    cur_time = 0

    def full_name(name: str) -> str:
        return ".".join(stack + [name]) if stack else name

    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue

            # ---------- 头部 ----------
            if not header_done:
                if line.startswith("$timescale"):
                    m = _TIMESCALE_RE.search(line)
                    if m:
                        vcd.timescale_fs = int(m.group("mag")) * _UNIT_FS[m.group("unit")]
                    continue
                if line.startswith("$scope"):
                    m = _SCOPE_RE.search(line)
                    if m:
                        stack.append(m.group("name"))
                    continue
                if line.startswith("$upscope"):
                    if stack:
                        stack.pop()
                    continue
                if line.startswith("$var"):
                    m = _VAR_RE.match(line)
                    if m:
                        raw_name = m.group("name")
                        msb = lsb = None
                        is_slice = False
                        tail = line[m.end("name"):]
                        rm = _RANGE_RE.search(tail)
                        if rm:
                            msb = int(rm.group("msb"))
                            if rm.group("lsb") is not None:
                                lsb = int(rm.group("lsb"))
                            else:
                                # 只有单个下标 → 被拆开的位切片，如 count [3]
                                is_slice = True
                                lsb = msb
                        else:
                            # 范围紧贴在名字后面：count[7:0]
                            rm = _RANGE_RE.search(raw_name)
                            if rm:
                                msb = int(rm.group("msb"))
                                if rm.group("lsb") is not None:
                                    lsb = int(rm.group("lsb"))
                                else:
                                    is_slice = True
                                    lsb = msb
                                raw_name = raw_name[: rm.start()]

                        name = re.sub(r"\[\d+(?::\d+)?\]$", "", raw_name)
                        width = int(m.group("width"))
                        sig = Signal(
                            id=m.group("id"),
                            name=name,
                            path=full_name(name),
                            width=width,
                            kind=m.group("type"),
                            msb=msb,
                            lsb=lsb,
                            slice_index=msb if is_slice else None,
                        )
                        vcd.by_id[sig.id] = sig
                        if is_slice:
                            # 位切片单独收进 slices，不占用 signals 主表：
                            # 否则按名字查 count 会拿到一根 1 位的线，比对全错。
                            vcd.slices.setdefault(sig.path, {})[sig.slice_index] = sig
                        elif width > 1:
                            # 总线：同名同路径若已有更宽的声明（先出现切片再出现整总线），
                            # 以整总线为准
                            old = vcd.signals.get(sig.path)
                            if old is None or sig.width >= old.width:
                                vcd.signals[sig.path] = sig
                        else:
                            vcd.signals[sig.path] = sig
                    continue
                if line.startswith("$enddefinitions"):
                    header_done = True
                    vcd.scopes = list(dict.fromkeys(stack))
                    stack.clear()
                    continue
                # 其余头部指令（$date/$version/$comment...）一律跳过
                continue

            # ---------- 正文 ----------
            if line[0] == "#":
                # 时间戳
                try:
                    cur_time = int(line[1:]) * vcd.timescale_fs
                except ValueError:
                    pass
                if vcd.change_count == 0:
                    vcd.time_min = cur_time
                continue

            if line[0] == "$":
                # $dumpvars / $dumpall / $dumpon / $dumpoff / $end
                continue

            # 值变化记录
            c = line[0]
            if c in "01xXzZ":
                # 单位信号：0! 或 1" 或 x#
                sig = vcd.by_id.get(line[1:])
                if sig is None:
                    continue
                _record(vcd, sig, cur_time, c.lower(), keep_duplicates)
            elif c in "bB":
                # 位向量：b0011 !  也可能带 x/z
                parts = line[1:].split(None, 1)
                if len(parts) != 2:
                    continue
                val, sid = parts[0], parts[1]
                sig = vcd.by_id.get(sid)
                if sig is None:
                    continue
                _record(vcd, sig, cur_time, val, keep_duplicates)
            elif c in "rR":
                # 实数：r3.14 "
                parts = line[1:].split(None, 1)
                if len(parts) != 2:
                    continue
                val, sid = parts[0], parts[1]
                sig = vcd.by_id.get(sid)
                if sig is None:
                    continue
                _record(vcd, sig, cur_time, val, keep_duplicates)
            # 其它（s 字符串等）忽略

            vcd.time_max = max(vcd.time_max, cur_time)

    _reconstruct_sliced_buses(vcd)
    return vcd


def _reconstruct_sliced_buses(vcd: VCD) -> None:
    """把「只有位切片、没有整总线」的信号拼回一条总线。

    有些仿真器/导出参数会把总线拆成一根根线导出（ModelSim 在某些设置下就是如此）。
    这时波形里只有 count[7]、count[6]…，没有完整的 count。
    不拼回去，按名字查 count 就什么都找不到，比对直接失败。

    拼接规则：按位号把每根切片的值放到对应位置。
    """
    for path, bits in vcd.slices.items():
        if path in vcd.signals:
            continue        # 已经有整总线了，不必拼
        if not bits:
            continue
        width = max(bits) + 1
        # 位号必须从 0 开始连续，否则说明导出不全，拼出来会静默出错——宁可不拼
        if set(bits) != set(range(width)):
            continue

        # 取所有切片的采样时刻并集，逐时刻合成整值
        times: set[int] = set()
        slice_traces: dict[int, list[tuple[int, str]]] = {}
        for idx, sig in bits.items():
            tr = vcd.traces.get(sig_key(sig), [])
            slice_traces[idx] = tr
            times.update(t for t, _ in tr)
        if not times:
            continue

        merged: list[tuple[int, str]] = []
        for t in sorted(times):
            val = 0
            ok = True
            for idx, tr in slice_traces.items():
                # 直接在这根切片自己的变化序列里找「t 时刻或之前最后一次」的值
                cur = None
                for tt, vv in tr:
                    if tt <= t:
                        cur = vv
                    else:
                        break
                if cur is None or cur not in ("0", "1"):
                    ok = False
                    break
                if cur == "1":
                    val |= 1 << idx
            if not ok:
                continue
            s = format(val, f"0{width}b")
            if merged and merged[-1][1] == s:
                continue
            merged.append((t, s))

        if not merged:
            continue
        proto = next(iter(bits.values()))
        full = Signal(
            id=f"__merged__{proto.name}",
            name=proto.name,
            path=path,
            width=width,
            kind=proto.kind,
            msb=width - 1,
            lsb=0,
        )
        vcd.signals[path] = full
        vcd.traces[path] = merged
        # 不把合并出来的跳变计入 change_count：
        # 它是**派生**数据，各根切片的真实跳变在 _record 里已经数过了。
        # 加进去就成了重复计数，报告里那个数字会虚高一倍——
        # 数字不对，整个工具的可信度都要打折。


def _record(vcd: VCD, sig: Signal, t: int, value: str, keep_duplicates: bool) -> None:
    """登记一次变化，并识别「重发当前值」的伪事件。

    关键：按信号的**唯一代号**存，而不是按路径。
    被拆开的位切片（count[7]、count[6]…）路径全都一样，
    按路径存会互相覆盖，拼总线时读到的就是错的。
    """
    key = sig_key(sig)
    trace = vcd.traces.setdefault(key, [])
    if trace and trace[-1][1] == value:
        # 值没变 —— $dumpall 之类会重发当前值，这不是真实跳变
        vcd.duplicate_count += 1
        if not keep_duplicates:
            return
    vcd.change_count += 1
    trace.append((t, value))


def sig_key(sig: Signal) -> str:
    """信号的唯一存储键。

    位切片用「路径@位号」区分，其余用路径。
    """
    if sig.is_slice:
        return f"{sig.path}@{sig.slice_index}"
    return sig.path
