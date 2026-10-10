"""时钟识别与周期重编号。

波形的时间轴是飞秒/纳秒，但硬件对不对讲的是「第几个时钟周期」。
本模块负责把时间轴翻译成周期轴，这是整条链路里最体现领域知识的一层：

- 自动找时钟（规律跳变的那根线），推断周期与采样边沿
- 自动找复位，复位有效期间的数据不算错（否则每个设计都会报一堆假警报）
- 按采样边沿取「稳定值」，避开 delta cycle 的瞬态
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .vcd import Signal, VCD


class ClockError(Exception):
    """找不到可用时钟。"""


@dataclass
class ClockInfo:
    signal: Signal
    period_fs: int              # 一个完整周期的时长
    rising_edges: list[int]     # 每个上升沿的时间戳
    falling_edges: list[int]
    sample_edge: str            # "posedge" 或 "negedge"
    duty: float | None = None   # 占空比，仅供参考


@dataclass
class ResetInfo:
    signal: Signal | None
    active_low: bool
    asserted: list[tuple[int, int]] = field(default_factory=list)   # 有效区间


@dataclass
class CycleAxis:
    """周期轴：第 N 拍 ↔ 时间戳 的映射。"""

    clock: ClockInfo
    reset: ResetInfo | None
    sample_times: list[int]          # 每个采样点的时间戳（第 0 拍、第 1 拍……）
    valid_from: int = 0              # 从第几拍开始比对有效（复位释放后）

    def __len__(self) -> int:
        return len(self.sample_times)

    def time_of(self, cycle: int) -> int:
        return self.sample_times[cycle]


def _is_regular(values: list[tuple[int, str]]) -> bool:
    return all(v in ("0", "1") for _, v in values)


def find_clock(vcd: VCD, hint: str | None = None) -> ClockInfo:
    """找出时钟信号并推断周期。

    策略：候选信号必须只取 0/1 两态、跳变次数足够多、且边沿间隔基本恒定。
    优先名字里带 clk/clock/ck 的，但名字只作加分，不当作判据——
    实际工程里时钟叫 `sys_clk`、`i_clk`、`CLK_50M` 的都有，甚至有人叫 `clk_i`。
    """
    if hint:
        hits = vcd.find(hint)
        if not hits:
            raise ClockError(f"指定的时钟信号找不到: {hint}")
        candidates = hits
    else:
        scored: list[tuple[float, Signal]] = []
        for sig in vcd.signals.values():
            if sig.is_real or sig.is_vector:
                continue
            trace = vcd.trace_of(sig)
            if len(trace) < 4 or not _is_regular(trace):
                continue
            # 名字加分
            name = sig.name.lower()
            score = float(len(trace))
            if re.search(r"(^|_)(clk|clock|ck)($|_)", name) or name in ("clk", "clock"):
                score *= 10
            scored.append((score, sig))
        if not scored:
            raise ClockError("波形里找不到符合时钟特征的信号（无规律的 0/1 跳变）")
        scored.sort(key=lambda x: -x[0])
        candidates = [scored[0][1]]

    # 从候选中挑出边沿间隔最规整的
    best: ClockInfo | None = None
    for sig in candidates:
        info = _analyze_clock(vcd, sig)
        if info is None:
            continue
        if best is None or len(info.rising_edges) > len(best.rising_edges):
            best = info
    if best is None:
        raise ClockError(f"信号 {candidates[0].path} 的跳变没有固定周期，不能当时钟用")
    return best


def _analyze_clock(vcd: VCD, sig: Signal) -> ClockInfo | None:
    trace = vcd.trace_of(sig)
    if len(trace) < 4:
        return None

    rising: list[int] = []
    falling: list[int] = []
    prev: str | None = None
    for t, v in trace:
        if prev == "0" and v == "1":
            rising.append(t)
        elif prev == "1" and v == "0":
            falling.append(t)
        prev = v

    if len(rising) < 2:
        return None

    # 检查上升沿间隔是否稳定（允许最后一段不完整）
    gaps = [rising[i + 1] - rising[i] for i in range(len(rising) - 1)]
    if not gaps:
        return None
    common = max(set(gaps), key=gaps.count)
    stable = sum(1 for g in gaps if g == common)
    if stable / len(gaps) < 0.8:
        return None

    # 占空比
    duty = None
    if falling:
        highs = [f - r for r, f in zip(rising, falling) if f > r]
        if highs:
            duty = (sum(highs) / len(highs)) / common

    return ClockInfo(
        signal=sig,
        period_fs=common,
        rising_edges=rising,
        falling_edges=falling,
        # 默认在上升沿采样：绝大多数同步设计如此。
        # 若某设计在下降沿驱动，比对时会出现整体错位，届时可通过配置改成 negedge。
        sample_edge="posedge",
        duty=duty,
    )


def find_reset(vcd: VCD, hint: str | None = None) -> ResetInfo | None:
    """找复位信号。找不到就返回 None（不阻塞流程，只是没有屏蔽区）。"""
    sig: Signal | None = None
    if hint:
        hits = vcd.find(hint)
        if hits:
            sig = hits[0]
    else:
        for cand in vcd.signals.values():
            if cand.is_real or cand.is_vector:
                continue
            if re.search(r"(^|_)(rst|reset|nrst|rstn|resetn|arst|srst)($|_)", cand.name.lower()):
                sig = cand
                break
    if sig is None:
        return None

    trace = vcd.trace_of(sig)
    if not trace:
        return None

    # 判断高有效还是低有效：上电后进入的那一段通常就是复位有效
    first_val = trace[0][1]
    active_low = first_val == "0"

    # 找出所有「有效区间」
    asserted: list[tuple[int, int]] = []
    start: int | None = None
    active_val = "0" if active_low else "1"
    for i, (t, v) in enumerate(trace):
        if v == active_val and start is None:
            start = t
        elif v != active_val and start is not None:
            asserted.append((start, t))
            start = None
    if start is not None:
        asserted.append((start, trace[-1][0] if trace else start))

    return ResetInfo(signal=sig, active_low=active_low, asserted=asserted)


def build_axis(
    vcd: VCD,
    *,
    clock_hint: str | None = None,
    reset_hint: str | None = None,
    sample_edge: str | None = None,
    start_cycle: int = 0,
) -> CycleAxis:
    """构建周期轴，并算出「从第几拍开始比对有效」。"""
    clock = find_clock(vcd, clock_hint)
    if sample_edge:
        clock.sample_edge = sample_edge

    reset = find_reset(vcd, reset_hint)

    edges = clock.rising_edges if clock.sample_edge == "posedge" else clock.falling_edges
    # 第 0 拍定义为第一个采样边沿
    sample_times = list(edges[start_cycle:])

    # 丢掉最后一个采样点。
    #
    # 原因：仿真以 $finish 结束时，最后一个时钟边沿之后波形就没有记录了，
    # 而寄存器在该边沿**之后**才更新——这个新值根本没被 dump 下来。
    # 于是最后那一拍取到的是"更新前的旧值"，逐拍比对会在结尾误报一处不符。
    # 实测里这表现为"前面全对、最后一拍突然错"，很容易被误判成真 bug。
    #
    # 宁可少比一拍，也不要在结尾报一个假错——假错会浪费大量排查时间。
    # 真需要检查最后一拍，应当让测试平台在 $finish 前多跑几个周期。
    if len(sample_times) > 1:
        sample_times = sample_times[:-1]

    axis = CycleAxis(clock=clock, reset=reset, sample_times=sample_times)

    # 复位屏蔽：找到复位最后一次释放的时刻。
    #
    # 用「严格晚于」释放时刻：复位在某个时钟边沿释放时，那一个边沿本身
    # 仍处于复位语义（触发器还在被复位），真正的第一个有效周期是
    # **释放之后**的第一个采样边沿。这个差一拍的判断如果搞错，
    # 每个设计都会在释放那一拍报出一片假错。
    if reset and reset.asserted:
        last_release = max(end for _, end in reset.asserted)
        idx = len(sample_times)
        for i, t in enumerate(sample_times):
            if t > last_release:
                idx = i
                break
        axis.valid_from = idx
    return axis
