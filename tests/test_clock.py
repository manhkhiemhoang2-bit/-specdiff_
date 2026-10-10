"""时钟识别、复位屏蔽、周期重编号的测试。

这一层全是领域知识：什么叫时钟、复位期该不该算错、按哪个边沿采样。
测试要覆盖真实设计的各种写法，而不是理想情况。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from specdiff.clock import ClockError, build_axis, find_clock, find_reset  # noqa: E402
from specdiff.vcd import parse  # noqa: E402


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "t.vcd"
    p.write_text(body, encoding="utf-8")
    return p


def simple_clk(n_cycles: int = 10, period_ns: int = 10, rst_low_until: int = 0):
    """造一个规整的时钟，可选带复位。"""
    lines = [
        "$timescale 1ns $end",
        "$scope module tb $end",
        "$var wire 1 ! clk $end",
        "$var reg 1 \" rst_n $end",
        "$upscope $end",
        "$enddefinitions $end",
        "#0",
        "0!",
        f'{0 if rst_low_until else 1}"',
    ]
    t = 0
    half = period_ns // 2
    for i in range(n_cycles * 2):
        t += half
        lines.append(f"#{t}")
        lines.append("1!" if i % 2 == 0 else "0!")
        if rst_low_until and t >= rst_low_until:
            lines.append('1"')
    return "\n".join(lines) + "\n"


class TestClockFinding:
    def test_finds_named_clock(self, tmp_path):
        v = parse(write(tmp_path, simple_clk()))
        info = find_clock(v)
        assert info.signal.name == "clk"
        assert info.period_fs == 10 * v.timescale_fs
        assert len(info.rising_edges) >= 5

    def test_measures_duty_cycle(self, tmp_path):
        v = parse(write(tmp_path, simple_clk()))
        info = find_clock(v)
        assert info.duty == pytest.approx(0.5, abs=0.05)

    def test_hint_overrides_autodetect(self, tmp_path):
        """时钟名字叫得再怪，只要用户指定就该用指定的那条。"""
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! heartbeat $end
$var wire 1 " unrelated $end
$upscope $end
$enddefinitions $end
#0
0!
#5
1!
#10
0!
#15
1!
#20
0!
#25
1!
"""
        v = parse(write(tmp_path, text))
        info = find_clock(v, "heartbeat")
        assert info.signal.name == "heartbeat"

    def test_rejects_signal_without_period(self, tmp_path):
        """乱跳的信号不能当时钟用，必须明确报错而不是猜。"""
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! noise $end
$upscope $end
$enddefinitions $end
#0
0!
#3
1!
#4
0!
#17
1!
#19
0!
#40
1!
"""
        v = parse(write(tmp_path, text))
        with pytest.raises(ClockError):
            find_clock(v)

    def test_rejects_empty_waveform(self, tmp_path):
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$upscope $end
$enddefinitions $end
#0
0!
"""
        v = parse(write(tmp_path, text))
        with pytest.raises(ClockError):
            find_clock(v)

    def test_gated_clock_with_gaps_still_found(self, tmp_path):
        """时钟被门控时会有停顿，不该因此认不出来。"""
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$upscope $end
$enddefinitions $end
#0
0!
#5
1!
#10
0!
#15
1!
#20
0!
#25
1!
#30
0!
#35
1!
#40
0!
"""
        v = parse(write(tmp_path, text))
        info = find_clock(v)
        assert info.period_fs == 10 * v.timescale_fs


class TestResetFinding:
    def test_active_low_reset_detected(self, tmp_path):
        v = parse(write(tmp_path, simple_clk(rst_low_until=23)))
        rst = find_reset(v)
        assert rst is not None
        assert rst.signal.name == "rst_n"
        assert rst.active_low is True
        assert len(rst.asserted) == 1

    def test_active_high_reset_detected(self, tmp_path):
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var reg 1 " rst $end
$upscope $end
$enddefinitions $end
#0
0!
1"
#5
1!
#10
0!
0"
#15
1!
"""
        v = parse(write(tmp_path, text))
        rst = find_reset(v)
        assert rst is not None
        assert rst.active_low is False

    def test_no_reset_returns_none(self, tmp_path):
        """找不到复位不该报错，只是没有屏蔽区。"""
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 1 " data $end
$upscope $end
$enddefinitions $end
#0
0!
0"
#5
1!
"""
        v = parse(write(tmp_path, text))
        assert find_reset(v) is None

    def test_multiple_reset_windows(self, tmp_path):
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var reg 1 " rst_n $end
$upscope $end
$enddefinitions $end
#0
0!
0"
#5
1!
#10
0!
#15
1!
1"
#20
0!
#25
1!
0"
#30
0!
#35
1!
1"
"""
        v = parse(write(tmp_path, text))
        rst = find_reset(v)
        assert rst is not None
        assert len(rst.asserted) == 2


class TestCycleAxis:
    def test_cycles_renumbered_from_zero(self, tmp_path):
        v = parse(write(tmp_path, simple_clk(n_cycles=8)))
        ax = build_axis(v)
        assert len(ax) >= 7
        # 第 0 拍就是第一个上升沿
        assert ax.time_of(0) == ax.clock.rising_edges[0]

    def test_reset_period_masked_out(self, tmp_path):
        """复位期间必须屏蔽，否则每个设计开局都报一片假错。"""
        v = parse(write(tmp_path, simple_clk(n_cycles=20, rst_low_until=23)))
        ax = build_axis(v)
        assert ax.valid_from > 0
        # 有效起始拍对应的时刻必须晚于复位释放
        assert ax.time_of(ax.valid_from) >= 23 * v.timescale_fs

    def test_no_reset_means_no_masking(self, tmp_path):
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$upscope $end
$enddefinitions $end
#0
0!
#5
1!
#10
0!
#15
1!
#20
0!
#25
1!
"""
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        assert ax.valid_from == 0

    def test_sample_edge_override(self, tmp_path):
        v = parse(write(tmp_path, simple_clk()))
        ax = build_axis(v, sample_edge="negedge")
        assert ax.clock.sample_edge == "negedge"
        # 下降沿存在时，第 0 拍应当是第一个下降沿
        if ax.clock.falling_edges:
            assert ax.time_of(0) == ax.clock.falling_edges[0]

    def test_axis_drops_last_sample_point(self, tmp_path):
        """最后一个采样点必须被丢掉。

        仿真以 $finish 结束时，最后一个时钟边沿之后波形就没有记录了，
        而寄存器是在该边沿**之后**才更新——这个新值没被 dump 下来。
        保留它会在结尾取到"更新前的旧值"，产生一处假错。

        代价是少比一拍。宁可少比，也不要在结尾报一个假错——
        假错会浪费大量排查时间，而且会让人不再相信这个工具。
        """
        v = parse(write(tmp_path, simple_clk(n_cycles=10)))
        ax = build_axis(v)
        edges = ax.clock.rising_edges
        assert len(ax) == len(edges) - 1
        # 保留前面那些，丢的是最后一个
        assert ax.time_of(0) == edges[0]
        assert ax.time_of(len(ax) - 1) == edges[-2]
