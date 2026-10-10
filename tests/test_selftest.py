"""变异自检的测试。

自检机制本身也必须被测试——否则它可能永远报"PASS"，
那样它就变成了一个安慰剂。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from specdiff.clock import build_axis  # noqa: E402
from specdiff.selftest import Mutation, run_selftest, standard_mutations  # noqa: E402
from specdiff.spec import Rule  # noqa: E402
from specdiff.vcd import parse  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_spec import mk_vcd, write  # noqa: E402


def counter_like(tmp_path):
    """一个每拍加一的计数器波形 + 与之相符的规格。

    时序要算准：时钟上升沿在 t = 5, 15, 25…（第 k 拍 = t = 5 + 10k）。
    要让第 k 拍采到值 k，就得在**该边沿之前**把值摆好，
    也就是在 t = 4 + 10k 处更新数据。

    基准规格必须是通过的——否则后面所有变异都"抓住"了，
    自检就失去意义（它证明不了是变异导致的失败）。
    """
    n = 20
    data = [(0, format(0, "08b"))]
    for k in range(1, n):
        data.append((4 + 10 * k, format(k, "08b")))
    text = mk_vcd({"dout": data})
    v = parse(write(tmp_path, text))
    ax = build_axis(v)
    rules = [Rule(kind="formula", name="逐拍加一", args={
        "target": "dout",
        # when 必须限定在"有数据"的范围内：测试辅助函数会在末尾补若干
        # 空闲时钟周期，那些拍上计数器已经不再自增。不加限制的话
        # 基准规格自己就会报错，后面所有变异都"抓住"，自检就证明不了任何事。
        "when": f"1 <= cyc <= {n - 1}",
        "expr": "(prev.dout + 1) % 256",
    })]
    return v, ax, rules


class TestSelfTestMechanism:
    def test_correct_spec_passes_all_mutations(self, tmp_path):
        """规格是对的，那么每个"改错"的变异都必须被抓出来。"""
        v, ax, rules = counter_like(tmp_path)
        report = run_selftest(v, ax, rules)
        assert report.ok, report.to_text()
        assert report.caught == report.total
        assert report.total > 0

    def test_ineffective_mutation_is_reported_as_missed(self, tmp_path):
        """反向验证：自检必须**有能力报 FAIL**。

        用一个什么都不改的变异（恒等变换）——它当然抓不出任何东西，
        所以必须被报成"漏掉"，整个自检必须判 FAIL。
        如果自检对任何输入都报 PASS，它就只是个安慰剂。
        """
        v, ax, rules = counter_like(tmp_path)

        def do_nothing(rs):
            return [Rule(kind=r.kind, name=r.name, args=dict(r.args),
                         enabled=r.enabled, note=r.note) for r in rs]

        report = run_selftest(v, ax, rules, mutations=[
            Mutation("什么都不改", do_nothing, applies_to=("formula",)),
        ])
        assert not report.ok
        assert len(report.missed) == 1
        assert report.missed[0].name == "什么都不改"

    def test_never_violated_contract_marked_inconclusive(self, tmp_path):
        """契约在本数据里从未被违反时，应报"无法验证"而不是"漏掉"。

        这是"数据不够"和"检查失效"的区分：前者不该算失败，
        否则用户会被误导去修一个本来没问题的检查。
        """
        v, ax, rules = counter_like(tmp_path)
        report = run_selftest(v, ax, rules)
        # 计数器波形里没有 X，所以 no_x 的取反测不出差别
        # （该规格里没有 no_x 规则，这里验证的是分类机制本身）
        for r in report.results:
            if r.inconclusive:
                assert not r.caught
                assert "从未被违反" in r.detail or "数据" in r.detail

    def test_report_text_lists_misses(self, tmp_path):
        v, ax, rules = counter_like(tmp_path)

        def do_nothing(rs):
            return [Rule(kind=r.kind, name=r.name, args=dict(r.args),
                         enabled=r.enabled, note=r.note) for r in rs]

        report = run_selftest(v, ax, rules, mutations=[
            Mutation("什么都不改", do_nothing, applies_to=("formula",)),
        ])
        out = report.to_text()
        assert "漏掉" in out
        assert "FAIL" in out

    def test_inapplicable_mutations_are_marked_not_failed(self, tmp_path):
        """规格里没有 latency 规则时，延迟类变异应标"不适用"，不算漏掉。"""
        v, ax, rules = counter_like(tmp_path)
        report = run_selftest(v, ax, rules)
        names = {r.name for r in report.skipped}
        assert "延迟多算一拍" in names
        assert all(not r.applicable for r in report.skipped)
        # 不适用不计入分母
        assert report.total == len([r for r in report.results if r.applicable])

    def test_disabled_rules_make_no_mutations_applicable(self, tmp_path):
        """全部规则被关掉时，没有可篡改的对象，自检应明确说明。"""
        text = mk_vcd({"dout": [(0, "00000000")]})
        v = parse(write(tmp_path, text))
        ax = build_axis(v)
        rules = [Rule(kind="formula", name="关掉", enabled=False,
                      args={"target": "dout", "expr": "0"})]
        report = run_selftest(v, ax, rules)
        assert report.total == 0
        assert not report.ok
        assert "无从进行" in report.to_text()

    def test_custom_mutation(self, tmp_path):
        """可以传入自定义变异。"""
        v, ax, rules = counter_like(tmp_path)

        def make_impossible(rs):
            out = [Rule(kind=r.kind, name=r.name, args=dict(r.args),
                        enabled=r.enabled, note=r.note) for r in rs]
            for r in out:
                if r.kind == "formula":
                    r.args["expr"] = "-999"
            return out

        report = run_selftest(v, ax, rules, mutations=[
            Mutation("自定义：公式改成 -999", make_impossible,
                     applies_to=("formula",)),
        ])
        assert report.ok
        assert report.results[0].caught

    def test_report_text_lists_inconclusive(self, tmp_path):
        """无法验证的条目要在报告里说明原因。"""
        v, ax, rules = counter_like(tmp_path)
        report = run_selftest(v, ax, rules)
        out = report.to_text()
        # 计数器的公式规则带 when，取反后可能测不出差别
        if report.inconclusive:
            assert "无法验证" in out
            assert "从未被违反" in out or "数据" in out

    def test_standard_mutations_have_applies_to(self):
        """标准变异必须声明适用类型，否则会误报"漏掉"。"""
        for m in standard_mutations():
            assert m.applies_to, f"{m.name} 没有声明 applies_to"
