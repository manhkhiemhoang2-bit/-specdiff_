"""规格层测试。

规格层的价值全在"判断得准"上，所以测试重点不是"能跑"，
而是三个容易做错的地方：

1. **不能把"通过"和"判断不了"混为一谈**——那会让报告里的断言数变成假的
2. **规格写错时要报错，不能静默跳过**——静默跳过等于没测
3. **延迟/公式/保持/不定态四类契约的边界**
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from specdiff.clock import build_axis  # noqa: E402
from specdiff.spec import Rule, SpecError, run_rules, safe_eval, to_int  # noqa: E402
from specdiff.specfile import load_spec  # noqa: E402
from specdiff.vcd import parse  # noqa: E402


def write(tmp_path: Path, body: str, name: str = "t.vcd") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def mk_vcd(signals: dict[str, list[tuple[int, str]]], *, period: int = 10) -> str:
    """按「信号 → [(时间, 值)]」构造波形，自动生成贯穿全程的时钟。

    时间单位 ns，时钟周期 10ns，上升沿在 5, 15, 25…
    时钟必须**从头到尾都在跳**，否则周期轴会从最后一个数据变化之后才开始，
    所有采样点都会取到末尾值——那样测试就测不到任何东西了。
    """
    lines = [
        "$timescale 1ns $end",
        "$scope module tb $end",
        "$var wire 1 ! clk $end",
    ]
    ids = {}
    for i, name in enumerate(signals):
        cid = chr(ord("A") + i)
        ids[name] = cid
        width = max((len(v) for _, v in signals[name]), default=1)
        width = max(width, 1)
        kind = "reg" if width > 1 else "wire"
        lines.append(f"$var {kind} {width} {cid} {name} $end")
    lines += ["$upscope $end", "$enddefinitions $end", "#0", "0!"]

    # 初始值
    for name in signals:
        v0 = signals[name][0][1] if signals[name] else "0"
        lines.append(f"b{v0} {ids[name]}" if len(v0) > 1 else f"{v0}{ids[name]}")

    # 把时钟边沿和数据变化按时间合并输出，保证时钟覆盖整条时间轴
    data_times = sorted({t for tr in signals.values() for t, _ in tr if t > 0})
    end = (max(data_times) if data_times else 0) + period * 10
    events: list[tuple[int, list[str]]] = []
    t = period // 2          # 第一个上升沿
    while t <= end:
        events.append((t, ["1!"]))
        events.append((t + period // 2, ["0!"]))
        t += period
    for tt in data_times:
        for name, tr in signals.items():
            for t2, v in tr:
                if t2 == tt:
                    cid = ids[name]
                    events.append((tt, [f"b{v} {cid}" if len(v) > 1 else f"{v}{cid}"]))
    events.sort(key=lambda x: x[0])

    cur_t = 0
    for tt, cmds in events:
        if tt != cur_t:
            lines.append(f"#{tt}")
            cur_t = tt
        lines.extend(cmds)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
class TestValueConversion:
    def test_binary_string_to_int(self):
        assert to_int("00000011") == 3
        assert to_int("11111111") == 255

    def test_int_passthrough(self):
        assert to_int(7) == 7

    def test_hex_and_bin_prefixes(self):
        assert to_int("0xFF") == 255
        assert to_int("0b1010") == 10

    def test_x_z_are_none(self):
        """不定态必须返回 None，绝不能当成 0——那会把假错变成假通过。"""
        assert to_int("xxxx") is None
        assert to_int("x") is None
        assert to_int("z") is None

    def test_empty_is_none(self):
        assert to_int("") is None
        assert to_int(None) is None


class TestSafeEval:
    def test_arithmetic(self):
        assert safe_eval("1 + 2", {}) == 3

    def test_no_builtins_escape(self):
        """受限求值不能让人从表达式里 import 东西。"""
        with pytest.raises(SpecError):
            safe_eval("__import__('os').getcwd()", {})

    def test_name_error_is_spec_error(self):
        with pytest.raises(SpecError):
            safe_eval("undefined_thing + 1", {})


# ---------------------------------------------------------------------------
# 四类契约
# ---------------------------------------------------------------------------
class TestFormulaRule:
    def test_passing_formula(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00000000"), (20000, "00000101")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="恒等于0", args={
            "target": "dout", "expr": "0",
            "when": "cyc < 3",
        })]
        r = run_rules(v, ax, rules)
        assert r.ok

    def test_failing_formula_reports_bits(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00001001")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="应为0", args={
            "target": "dout", "expr": "0", "when": "cyc == 0",
        })]
        r = run_rules(v, ax, rules)
        assert not r.ok
        assert r.violations[0].bits == [0, 3]
        assert r.violations[0].cycle == 0

    def test_expr_zero_is_valid_not_missing(self, tmp_path):
        """expr 写成 0 是合法表达式，不能被当成"没写"。"""
        text = mk_vcd({"dout": [(0, "00000000")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="零", args={"target": "dout", "expr": 0})]
        r = run_rules(v, ax, rules)
        assert r.ok
        assert r.checks_run > 0

    def test_when_false_skips_check(self, tmp_path):
        """when 恒假时，所有拍都应算作"跳过"而不是"通过"。"""
        text = mk_vcd({"dout": [(0, "00001001")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="永不检查", args={
            "target": "dout", "expr": "0", "when": "cyc < 0",
        })]
        r = run_rules(v, ax, rules)
        assert r.ok
        assert r.checks_run == 0
        assert r.checks_skipped == len(ax)


class TestLatencyRule:
    def test_correct_latency_passes(self, tmp_path):
        """输出延迟 2 拍：第 N 拍输出 = 第 N-2 拍输入。

        时钟上升沿在 5, 15, 25…（周期 10ns，所以"2 拍"= 20ns）。
        din 在 t=6000 拉高，dout 在 t=6020 拉高——正好晚 20ns。
        """
        text = mk_vcd({
            "din": [(0, "0"), (6000, "1")],
            "dout": [(0, "0"), (6020, "1")],
        })
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="latency", name="延迟2拍", args={
            "cycles": 2, "pairs": [("dout", "din")], "_axis_times": ax.sample_times,
        })]
        r = run_rules(v, ax, rules)
        assert r.ok, r.violations[:3]
        assert r.checks_run > 0

    def test_wrong_latency_caught(self, tmp_path):
        """输出只延迟 1 拍（晚 10ns），却按 2 拍检查——必须被抓出来。"""
        text = mk_vcd({
            "din": [(0, "0"), (6000, "1")],
            "dout": [(0, "0"), (6010, "1")],   # 只晚 10ns = 1 拍
        })
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="latency", name="延迟2拍", args={
            "cycles": 2, "pairs": [("dout", "din")], "_axis_times": ax.sample_times,
        })]
        r = run_rules(v, ax, rules)
        assert not r.ok
        assert r.violations[0].signal == "dout"


class TestStableRule:
    def test_stable_passes_when_unchanged(self, tmp_path):
        text = mk_vcd({"data": [(0, "00000101")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="stable", name="保持不变", args={"signals": ["data"]})]
        r = run_rules(v, ax, rules)
        assert r.ok

    def test_stable_catches_change(self, tmp_path):
        text = mk_vcd({"data": [(0, "00000001"), (25000, "00000010")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="stable", name="不应变化", args={"signals": ["data"]})]
        r = run_rules(v, ax, rules)
        assert not r.ok
        assert "不应变化" in r.violations[0].detail

    def test_when_gates_the_check(self, tmp_path):
        """when 为假时不做检查（用于只在握手期间约束的场景）。

        用恒假条件，确保整条时间轴上都不适用——这样能验证
        "不适用"被算成跳过而不是通过。
        """
        text = mk_vcd({"data": [(0, "00000001"), (25000, "00000010")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="stable", name="从不检查", args={
            "signals": ["data"], "when": "cyc < 0",
        })]
        r = run_rules(v, ax, rules)
        assert r.ok
        assert r.checks_run == 0
        assert r.checks_skipped == len(ax)


class TestNoXRule:
    def test_clean_signal_passes(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00000101")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="no_x", name="无不定态", args={"signals": ["dout"]})]
        r = run_rules(v, ax, rules)
        assert r.ok

    def test_x_is_caught(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00000101"), (25000, "0000xxxx")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="no_x", name="无不定态", args={"signals": ["dout"]})]
        r = run_rules(v, ax, rules)
        assert not r.ok
        assert "不定态" in r.violations[0].detail


# ---------------------------------------------------------------------------
# 统计必须诚实
# ---------------------------------------------------------------------------
class TestHonestAccounting:
    def test_passed_and_skipped_are_counted_separately(self, tmp_path):
        """通过和"判断不了"必须分开计数。

        如果混在一起，报告里的"断言数"就是假的——
        数字错了，整个检查结果就不可信。
        """
        # 一个信号一直是确定值，另一个一直是不定态
        text = mk_vcd({
            "good": [(0, "00000001")],
            "bad": [(0, "xxxxxxxx")],
        })
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [
            Rule(kind="formula", name="好信号", args={"target": "good", "expr": "1"}),
            Rule(kind="formula", name="坏信号", args={"target": "bad", "expr": "1"}),
        ]
        r = run_rules(v, ax, rules)
        assert r.ok
        assert r.checks_run > 0          # 好信号确实被判断了
        assert r.checks_skipped > 0      # 坏信号判断不了，被单独统计
        assert r.checks_passed == r.checks_run

    def test_violations_reduce_passed_count(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00001001")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="应为0", args={"target": "dout", "expr": "0"})]
        r = run_rules(v, ax, rules)
        assert r.checks_run == len(r.violations) + r.checks_passed


# ---------------------------------------------------------------------------
# 规格写错必须报错，不能静默跳过
# ---------------------------------------------------------------------------
class TestSpecErrorsAreLoud:
    def test_unknown_signal_raises(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00000001")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="引用不存在的信号",
                      args={"target": "nonexistent", "expr": "1"})]
        with pytest.raises(SpecError) as e:
            run_rules(v, ax, rules)
        assert "找不到" in str(e.value)

    def test_unknown_signal_in_expr_raises(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00000001")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="表达式引用错信号",
                      args={"target": "dout", "expr": "cur.nosuch + 1"})]
        with pytest.raises(SpecError):
            run_rules(v, ax, rules)

    def test_unknown_rule_kind_raises(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00000001")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="nonsense", name="瞎写的类型", args={})]
        with pytest.raises(SpecError) as e:
            run_rules(v, ax, rules)
        assert "不支持" in str(e.value) or "未知" in str(e.value)

    def test_missing_expr_raises(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00000001")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="没写表达式", args={"target": "dout"})]
        with pytest.raises(SpecError) as e:
            run_rules(v, ax, rules)
        assert "expr" in str(e.value)


# ---------------------------------------------------------------------------
# 规格文件读取
# ---------------------------------------------------------------------------
class TestSpecFileLoading:
    def test_loads_signals_const_rules(self, tmp_path):
        p = tmp_path / "s.yaml"
        p.write_text("""
signals:
  clk: tb.clk
  dout: tb.dut.dout

const:
  W: 32

rules:
  - name: 输出等于输入
    kind: formula
    target: dout
    expr: cur.dout
    note: 直通
""", encoding="utf-8")
        spec = load_spec(p)
        assert spec.signals == {"clk": "tb.clk", "dout": "tb.dut.dout"}
        assert spec.const["W"] == 32
        assert len(spec.rules) == 1
        assert spec.rules[0].name == "输出等于输入"
        assert spec.rules[0].note == "直通"

    def test_pairs_parsed(self, tmp_path):
        p = tmp_path / "s.yaml"
        p.write_text("""
rules:
  - name: 延迟
    kind: latency
    cycles: 2
    pairs:
      - [O_tvalid, I_tvalid]
      - [O_tdata, I_tdata]
""", encoding="utf-8")
        spec = load_spec(p)
        assert spec.rules[0].args["pairs"] == [
            ("O_tvalid", "I_tvalid"), ("O_tdata", "I_tdata")]

    def test_bad_kind_rejected(self, tmp_path):
        p = tmp_path / "s.yaml"
        p.write_text("rules:\n  - name: x\n    kind: bogus\n", encoding="utf-8")
        with pytest.raises(SpecError) as e:
            load_spec(p)
        assert "kind" in str(e.value)

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(SpecError):
            load_spec(tmp_path / "nope.yaml")

    def test_rules_must_be_list(self, tmp_path):
        p = tmp_path / "s.yaml"
        p.write_text("rules: not_a_list\n", encoding="utf-8")
        with pytest.raises(SpecError):
            load_spec(p)

    def test_block_scalar_note(self, tmp_path):
        """note 用 | 写多行，要能正确读进来。"""
        p = tmp_path / "s.yaml"
        p.write_text("""
rules:
  - name: 多行说明
    kind: no_x
    signals: [dout]
    note: |
      第一行说明
      第二行说明
""", encoding="utf-8")
        spec = load_spec(p)
        assert "第一行说明" in spec.rules[0].note
        assert "第二行说明" in spec.rules[0].note

    def test_enabled_flag(self, tmp_path):
        p = tmp_path / "s.yaml"
        p.write_text("""
rules:
  - name: 关掉的规则
    kind: no_x
    signals: [dout]
    enabled: false
""", encoding="utf-8")
        spec = load_spec(p)
        assert spec.rules[0].enabled is False


class TestSpecIntegration:
    def test_disabled_rule_is_not_run(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00001001")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="关掉", enabled=False,
                      args={"target": "dout", "expr": "0"})]
        r = run_rules(v, ax, rules)
        assert r.ok
        assert r.checks_run == 0

    def test_max_violations_cap(self, tmp_path):
        text = mk_vcd({"dout": [(0, "00001001")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="全错", args={"target": "dout", "expr": "0"})]
        r = run_rules(v, ax, rules, max_violations=3)
        assert len(r.violations) == 3
