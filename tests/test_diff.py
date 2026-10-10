"""差分引擎测试。

重点验证「测得准」而不是「能测」：
- 能定位到具体是哪一位不同
- 复位期不产生假警报
- 值的比较按数值而非字符串（否则字节序颠倒的真 bug 会被看漏）
- 最短窗口收缩
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from specdiff.diff import GoldenTrace, Mapping, diff, load_mapping  # noqa: E402
from specdiff.vcd import parse  # noqa: E402


def write(tmp_path: Path, body: str, name: str = "t.vcd") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def mk_vcd(dout_values: list[str], period_ns: int = 10) -> str:
    """造一个时钟 + 8 位 dout 的波形，dout 的每个值占一拍。

    约定：第 i 拍 = 第 i 个「上升沿采样到的」dout 值。
    做法是先把数据摆好，再给出上升沿：这样上升沿采到的就是该拍应有的值。
    为了让时钟特征足够明显（识别需要足够的跳变），后面会补几个空闲周期。
    """
    half = period_ns // 2
    n = len(dout_values)
    # 末尾补 3 个空闲拍，保证时钟跳变次数足够被识别出来
    total = n + 3

    lines = [
        "$timescale 1ns $end",
        "$scope module tb $end",
        "$var wire 1 ! clk $end",
        '$var reg 8 " dout $end',
        "$upscope $end",
        "$enddefinitions $end",
        "#0",
        "0!",
        f'b{dout_values[0]} "',
    ]
    for i in range(total):
        t_rise = (2 * i + 1) * half
        if i > 0 and i < n:
            # 上升沿之前一点把数据换成这一拍的值
            lines.append(f"#{t_rise - half // 2}")
            lines.append(f'b{dout_values[i]} "')
        lines.append(f"#{t_rise}")
        lines.append("1!")
        lines.append(f"#{t_rise + half}")
        lines.append("0!")
    return "\n".join(lines) + "\n"


class TestGoldenTrace:
    def test_from_cycle_dicts(self, tmp_path):
        p = tmp_path / "g.json"
        p.write_text(json.dumps({"cycles": [
            {"cycle": 0, "dout": 1},
            {"cycle": 1, "dout": 2},
        ]}), encoding="utf-8")
        g = GoldenTrace.from_json(p)
        assert g.cycles == 2
        assert g.fields["dout"] == [1, 2]

    def test_from_field_arrays(self, tmp_path):
        p = tmp_path / "g.json"
        p.write_text(json.dumps({"dout": [1, 2, 3], "valid": [0, 1, 1]}), encoding="utf-8")
        g = GoldenTrace.from_json(p)
        assert g.cycles == 3
        assert g.fields["valid"] == [0, 1, 1]

    def test_ragged_rows_padded(self, tmp_path):
        """某拍漏了字段时补齐，不要因此错位。"""
        p = tmp_path / "g.json"
        p.write_text(json.dumps({"cycles": [
            {"cycle": 0, "a": 1, "b": 2},
            {"cycle": 1, "a": 3},
        ]}), encoding="utf-8")
        g = GoldenTrace.from_json(p)
        assert g.fields["b"] == [2, None]


class TestValueComparison:
    """值的比较必须按数值和位宽，不能按字符串。"""

    def test_detects_byte_swap_that_string_compare_would_miss(self, tmp_path):
        """数值比较 vs 字符串比较的差别。

        波形里的总线是二进制串，标准答案是整数。
        一个偷懒的实现可能把二进制串直接和数字比，或按字符串长度对齐，
        那样「字节序/位序」类错误就会被看漏或错报。
        这里用非对称数据确保比较确实按数值进行。
        """
        vcd_text = mk_vcd(["00000000", "00000001", "00000010"])
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"dout": [0, 1, 2]})
        ok = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v)
        assert ok.status == "match"

        # 把第 1、2 拍的值互换（模拟位序被搞反）
        vcd_text2 = mk_vcd(["00000000", "00000010", "00000001"])
        v2 = parse(write(tmp_path, vcd_text2, "t2.vcd"))
        bad = diff(tmp_path / "t2.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v2)
        assert bad.status == "mismatch"
        assert bad.first.cycle == 1
        # 期望 1(0b01)，实际 2(0b10) → 第 0、1 位都不同
        assert bad.first.bits == [0, 1]

    def test_identifies_exact_bit(self, tmp_path):
        """一个 8 位数只有第 3 位错，必须精确报出第 3 位。"""
        vcd_text = mk_vcd(["00000000", "00000001", "00001001"])
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"dout": [0, 1, 2]})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v)
        assert r.status == "mismatch"
        m = r.first
        assert m.cycle == 2
        # 期望 2(0b00000010)，实际 9(0b00001001) → 第 0、1、3 位不同
        assert m.bits == [0, 1, 3]
        assert m.expected == "0x02"
        assert m.actual == "0x09"

    def test_single_bit_error_isolated(self, tmp_path):
        """只错一位时，必须只报那一位——多报会误导排查方向。"""
        vcd_text = mk_vcd(["00000000", "00000011"])
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"dout": [0, 1]})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v)
        assert r.first.bits == [1]
        assert r.first.expected == "0x01"
        assert r.first.actual == "0x03"

    def test_multiple_bits_reported(self, tmp_path):
        vcd_text = mk_vcd(["00000000", "00001011"])
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"dout": [0, 0]})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v)
        assert r.first.bits == [0, 1, 3]
    def test_width_masking(self, tmp_path):
        """标准答案给的值超出位宽时要按位宽截断，不能整体判错。"""
        vcd_text = mk_vcd(["00000000", "11111111"])
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"dout": [0, 0x1FF]})   # 9 位值
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v)
        assert r.status == "match"

    def test_x_values_are_skipped_not_failed(self, tmp_path):
        """不定态无法判对错，应当跳过而不是报错。"""
        vcd_text = mk_vcd(["00000000", "xxxxxxxx"])
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"dout": [0, 5]})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v)
        assert r.status == "match"


class TestResetMasking:
    def test_no_false_alarm_during_reset(self, tmp_path):
        """复位期间输出是 0（或不定），标准答案若从 0 起算则不该报错。

        更重要的是：复位期间即使对不上，也不能算作不一致，
        否则每个设计开局都会报一片假错，真的 bug 会被淹没。
        """
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var reg 1 " rst_n $end
$var reg 8 # dout $end
$upscope $end
$enddefinitions $end
#0
0!
0"
b00000000 #
#5
1!
#10
0!
b00001111 #
#15
1!
1"
#20
0!
b00000001 #
#25
1!
#30
0!
b00000010 #
#35
1!
#40
0!
b00000011 #
#45
1!
#50
0!
b00000100 #
#55
1!
#60
0!
b00000101 #
#65
1!
"""
        v = parse(write(tmp_path, text))
        # 复位期间（第 0、1 拍，含释放那一拍）的输出故意和标准答案不一致。
        # 第 2 拍起波形是 1,2,3,4,5，标准答案这里也给 1..5，
        # 若复位屏蔽失效，第 1 拍的 0x0F 会立刻被报成不一致。
        golden = GoldenTrace.from_dict({"dout": [0, 0, 1, 2, 3, 4, 5]})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v)
        assert r.valid_from == 2
        assert r.status == "match"
        # 末尾那一拍被刻意丢弃（仿真结束时最后一个边沿的新值没被 dump），
        # 所以是 4 拍而不是 5 拍
        assert r.compared_cycles >= 4


class TestWindowAndReporting:
    def test_minimal_window_around_first_mismatch(self, tmp_path):
        vals = ["00000000"] * 30
        vals[20] = "00000101"
        vcd_text = mk_vcd(vals)
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"dout": [0] * 31})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)],
                 vcd=v, window_pad=5)
        assert r.first.cycle == 20
        lo, hi = r.window
        assert lo == 15 and hi == 25

    def test_mismatch_cap_respected(self, tmp_path):
        vals = [format(i, "08b") for i in range(1, 40)]
        vcd_text = mk_vcd(vals)
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"dout": [0] * 40})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)],
                 vcd=v, max_mismatches=3)
        assert len(r.mismatches) == 3

    def test_text_report_contains_key_facts(self, tmp_path):
        vcd_text = mk_vcd(["00000000", "00000001", "00001001"])
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"dout": [0, 1, 2]})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v)
        text = r.to_text()
        assert "第 2 拍" in text
        assert "0x02" in text and "0x09" in text
        assert "3" in text

    def test_json_report_is_stable(self, tmp_path):
        vcd_text = mk_vcd(["00000000", "00001001"])
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"dout": [0, 2]})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v)
        d = r.to_dict()
        assert d["status"] == "mismatch"
        assert d["first_mismatch"]["cycle"] == 1
        # 期望 2(0b0010) 实际 9(0b1001) → 第 0、1、3 位不同
        assert d["first_mismatch"]["bits_differing"] == [0, 1, 3]
        assert "minimal_window" in d


class TestErrorHandling:
    def test_missing_signal_reports_clearly(self, tmp_path):
        vcd_text = mk_vcd(["00000000"])
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"nope": [0]})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("nope", "nope", 8)], vcd=v)
        assert r.status == "error"
        assert "找不到信号" in r.message

    def test_missing_golden_field_reports_clearly(self, tmp_path):
        vcd_text = mk_vcd(["00000000"])
        v = parse(write(tmp_path, vcd_text))
        golden = GoldenTrace.from_dict({"other": [0]})
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dout", "dout", 8)], vcd=v)
        assert r.status == "error"
        assert "标准答案里没有字段" in r.message

    def test_prefers_width_matching_signal(self, tmp_path):
        """同级同名的 1 位切片和 8 位总线并存时，按位宽挑对的那根。"""
        text = """$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
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
0!
0#
0$
0%
0&
0'
0(
0)
0*
b00000001 +
#5
1!
#10
0!
b00000010 +
#15
1!
#20
0!
b00000011 +
#25
1!
#30
0!
b00000100 +
#35
1!
#40
0!
b00000101 +
#45
1!
"""
        v = parse(write(tmp_path, text))
        golden = GoldenTrace.from_dict({"dout": [1]})
        # 用短名 dout，应当自动选中 8 位那条
        r = diff(tmp_path / "t.vcd", golden, [Mapping("dut.dout", "dout", 8)], vcd=v)
        assert r.status == "match"


class TestMappingConfig:
    def test_yaml_config(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text("""
map:
  - rtl: dut.count
    golden: count
    width: 8
    note: 计数器
  - rtl: dut.carry
    golden: carry
    width: 1
""", encoding="utf-8")
        m = load_mapping(p)
        assert len(m) == 2
        assert m[0].rtl == "dut.count"
        assert m[0].golden == "count"
        assert m[0].width == 8
        assert m[1].width == 1

    def test_yaml_null_placeholder(self, tmp_path):
        """顶层 key 后面没有内容时，解析结果应当是空列表。"""
        p = tmp_path / "m.yaml"
        p.write_text("map:\n", encoding="utf-8")
        assert load_mapping(p) == []

    def test_golden_defaults_to_last_name_segment(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text("map:\n  - rtl: dut.count\n    width: 8\n", encoding="utf-8")
        m = load_mapping(p)
        assert m[0].golden == "count"

    def test_json_config(self, tmp_path):
        p = tmp_path / "m.json"
        p.write_text(json.dumps({"map": [
            {"rtl": "a", "golden": "b", "width": 4}
        ]}), encoding="utf-8")
        m = load_mapping(p)
        assert m[0].width == 4
