"""波形解析层测试。

重点覆盖真实波形里会遇到的坑，而不是只测理想输入：
- 位切片（count [7]）与整总线（count [7:0]）同名共存
- 重发当前值造成的伪事件
- 同一时间戳多次跳变
- 实数信号
- 层次化作用域
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from specdiff.vcd import Signal, parse  # noqa: E402


def write_vcd(tmp_path: Path, body: str, name: str = "t.vcd") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


BASIC = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var reg 8 " dout $end
$upscope $end
$enddefinitions $end
#0
0!
b00000000 "
#5
1!
#10
0!
b00111100 "
#15
1!
#20
0!
b00111101 "
"""


class TestBasicParsing:
    def test_parses_signals_and_timescale(self, tmp_path):
        vcd = parse(write_vcd(tmp_path, BASIC))
        assert vcd.timescale_fs == 1_000_000       # 1ns = 1e6 fs
        assert "tb.clk" in vcd.signals
        assert "tb.dout" in vcd.signals
        assert vcd.signals["tb.dout"].width == 8

    def test_traces_recorded_in_order(self, tmp_path):
        vcd = parse(write_vcd(tmp_path, BASIC))
        trace = vcd.traces["tb.clk"]
        assert [t for t, _ in trace] == [0, 5e6, 1e7, 1.5e7, 2e7] or \
               [t for t, _ in trace] == sorted(t for t, _ in trace)
        assert trace[0][1] == "0"
        assert trace[1][1] == "1"

    def test_values_at_returns_last_known(self, tmp_path):
        vcd = parse(write_vcd(tmp_path, BASIC))
        dout = vcd.signals["tb.dout"]
        assert vcd.values_at(dout, 0) == "00000000"
        assert vcd.values_at(dout, 9_000_000) == "00000000"     # 9ns，还没变
        assert vcd.values_at(dout, 10_000_000) == "00111100"    # 10ns
        assert vcd.values_at(dout, 99_000_000) == "00111101"    # 取最后已知值


class TestRealWorldHazards:
    def test_bit_slices_do_not_hide_full_bus(self, tmp_path):
        """ModelSim 会把 tb 层总线拆成一根根线导出，同时在 dut 层保留整总线。

        如果解析器让切片覆盖了同名总线，按名字查 dout 会拿到 1 位的线，
        比对结果会完全错误。这里验证切片与整总线被分开保存。
        """
        vcd_text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 # dout [7] $end
$var wire 1 $ dout [6] $end
$var wire 1 % dout [5] $end
$var wire 1 & dout [4] $end
$var wire 1 ' dout [3] $end
$var wire 1 ( dout [2] $end
$var wire 1 ) dout [1] $end
$var wire 1 * dout [0] $end
$scope module dut $end
$var reg 8 + dout [7:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0#
0$
0%
0&
0'
0(
0)
0*
b00111100 +
#5
1#
b00111101 +
"""
        vcd = parse(write_vcd(tmp_path, vcd_text))
        # 整总线必须存在且位宽为 8
        full = vcd.signals.get("tb.dut.dout")
        assert full is not None
        assert full.width == 8
        # tb 层的切片被单独收好，位号齐全
        assert "tb.dout" in vcd.slices
        assert sorted(vcd.slices["tb.dout"]) == list(range(8))
        # tb 层没有整总线声明，应当由切片拼回一条 8 位总线
        assert "tb.dout" in vcd.signals
        assert vcd.signals["tb.dout"].width == 8
        assert vcd.signals["tb.dout"].id.startswith("__merged__")
        # 按短名查时，8 位那条必须能被找到
        hits = vcd.find("dut.dout")
        assert [h.width for h in hits] == [8]
        # 切片各自的变化必须独立保存，不能互相覆盖
        # 波形在 #5 把 1# 置 1，其中 # 是 dout[7]
        assert vcd.values_at(vcd.slices["tb.dout"][7], 5 * vcd.timescale_fs) == "1"
        assert vcd.values_at(vcd.slices["tb.dout"][0], 5 * vcd.timescale_fs) == "0"

    def test_reconstructs_bus_from_slices_alone(self, tmp_path):
        """如果波形里只有位切片、没有整总线，应当能拼回一条总线。"""
        lines = ["$timescale 1ns $end", "$scope module tb $end"]
        ids = "#$%&'()*"
        for i, cid in enumerate(ids):
            bit = 7 - i
            lines.append(f"$var wire 1 {cid} dout [{bit}] $end")
        lines += ["$upscope $end", "$enddefinitions $end", "#0"]
        # 值 0b00000101 = 5 → dout[2]=1, dout[0]=1
        for i, cid in enumerate(ids):
            bit = 7 - i
            val = "1" if bit in (2, 0) else "0"
            lines.append(f"{val}{cid}")
        vcd = parse(write_vcd(tmp_path, "\n".join(lines) + "\n"))
        assert "tb.dout" in vcd.signals
        merged = vcd.signals["tb.dout"]
        assert merged.width == 8
        assert vcd.values_at(merged, 0) == "00000101"

    def test_reconstructed_bus_is_not_double_counted(self, tmp_path):
        """重建出来的总线是派生数据，不能重复计入跳变总数。

        各根切片的跳变在解析时已经数过一次了；如果再把拼出来的总线
        也加进去，报告里的跳变数会凭空翻倍。数字虚高会让用户
        对工具的其余输出也产生怀疑。
        """
        # 8 根切片，每根只变一次 → 真实跳变数就是 8
        lines = ["$timescale 1ns $end", "$scope module tb $end"]
        ids = "#$%&'()*"
        for i, cid in enumerate(ids):
            lines.append(f"$var wire 1 {cid} dout [{7 - i}] $end")
        lines += ["$upscope $end", "$enddefinitions $end", "#0"]
        for cid in ids:
            lines.append(f"0{cid}")
        lines.append("#10")
        lines.append("1#")          # 只让最高位跳一次
        vcd = parse(write_vcd(tmp_path, "\n".join(lines) + "\n"))

        # 8 根切片初值 + 1 次跳变 = 9 个真实变化
        assert vcd.change_count == 9, (
            f"跳变数应为 9（8 根切片初值 + 1 次跳变），实际 {vcd.change_count}——"
            f"重建出的总线被重复计数了"
        )
        # 而拼出来的总线确实存在且可用
        assert "tb.dout" in vcd.signals

    def test_duplicate_events_are_not_real_changes(self, tmp_path):
        """$dumpall 会重发当前值，那不是真实跳变，不该计入跳变次数。"""
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! a $end
$upscope $end
$enddefinitions $end
#0
0!
#5
1!
#10
1!
#15
1!
#20
0!
"""
        vcd = parse(write_vcd(tmp_path, text))
        assert vcd.change_count == 3          # 0→1→0 共 3 次真实变化
        assert vcd.duplicate_count == 2       # 两次重发
        assert len(vcd.traces["tb.a"]) == 3

    def test_keeps_duplicates_when_requested(self, tmp_path):
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! a $end
$upscope $end
$enddefinitions $end
#0
0!
#5
0!
"""
        vcd = parse(write_vcd(tmp_path, text), keep_duplicates=True)
        assert len(vcd.traces["tb.a"]) == 2

    def test_four_state_values_preserved(self, tmp_path):
        """x/z 必须原样保留，不能擅自当成 0 或 1——那是不同的语义。"""
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 4 ! bus $end
$upscope $end
$enddefinitions $end
#0
bxxxx !
#5
b01xz !
"""
        vcd = parse(write_vcd(tmp_path, text))
        ns = vcd.timescale_fs
        assert vcd.values_at("tb.bus", 0) == "xxxx"
        assert vcd.values_at("tb.bus", 5 * ns) == "01xz"

    def test_real_signal(self, tmp_path):
        text = """$timescale 1ns $end
$scope module tb $end
$var real 1 ! volt $end
$upscope $end
$enddefinitions $end
#0
r1.5 !
#5
r3.25 !
"""
        vcd = parse(write_vcd(tmp_path, text))
        sig = vcd.signals["tb.volt"]
        assert sig.is_real
        assert vcd.values_at(sig, 5 * vcd.timescale_fs) == "3.25"

    def test_multi_scope_paths(self, tmp_path):
        text = """$timescale 1ns $end
$scope module tb $end
$scope module dut $end
$scope module u_sub $end
$var wire 1 ! sig $end
$upscope $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
"""
        vcd = parse(write_vcd(tmp_path, text))
        assert "tb.dut.u_sub.sig" in vcd.signals

    def test_name_with_attached_range(self, tmp_path):
        """有的工具把范围紧贴名字写：dout[7:0]"""
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 8 ! dout[7:0] $end
$upscope $end
$enddefinitions $end
#0
b10101010 !
"""
        vcd = parse(write_vcd(tmp_path, text))
        sig = vcd.signals.get("tb.dout")
        assert sig is not None and sig.width == 8
        assert vcd.values_at(sig, 0) == "10101010"


class TestSignalLookup:
    def make(self, tmp_path):
        text = """$timescale 1ns $end
$scope module tb $end
$scope module dut $end
$var wire 8 ! data $end
$var wire 1 " valid $end
$upscope $end
$scope module other $end
$var wire 8 # data $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
b00000001 !
0"
b00000010 #
"""
        return parse(write_vcd(tmp_path, text))

    def test_exact_path(self, tmp_path):
        vcd = self.make(tmp_path)
        hits = vcd.find("tb.dut.data")
        assert len(hits) == 1 and hits[0].width == 8

    def test_short_name_can_match_multiple(self, tmp_path):
        vcd = self.make(tmp_path)
        hits = vcd.find("data")
        assert len(hits) == 2

    def test_suffix_match(self, tmp_path):
        vcd = self.make(tmp_path)
        hits = vcd.find("dut.data")
        assert len(hits) == 1 and hits[0].path == "tb.dut.data"

    def test_wildcard(self, tmp_path):
        vcd = self.make(tmp_path)
        hits = vcd.find("tb.*.data")
        assert len(hits) == 2

    def test_missing_returns_empty(self, tmp_path):
        vcd = self.make(tmp_path)
        assert vcd.find("nonexistent") == []

    def test_changes_in_window(self, tmp_path):
        vcd = self.make(tmp_path)
        got = vcd.changes_in("tb.dut.data", 0, 0)
        assert got == [(0, "00000001")]


class TestSummary:
    def test_summary_fields(self, tmp_path):
        vcd = parse(write_vcd(tmp_path, BASIC))
        s = vcd.summary()
        assert s["signals"] == 2
        assert s["changes"] > 0
        assert s["time_max"] > s["time_min"]
