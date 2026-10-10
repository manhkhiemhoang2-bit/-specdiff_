"""变异自检：证明检查器真的有鉴别力。

这条纪律来自一个实际项目：

    抓不住的条目，说明测试在这个维度上没测到东西。

一个检查器如果永远报"通过"，它和不存在没区别。所以自检的做法是：
故意把规格改错（把延迟说成 1 拍、把几何整体偏移一行……），
再看检查器抓不抓得住。抓不住，那个维度的断言就是空的。

验收和答辩时这套机制好用：能拿出证据说明测试有效，
而不是只能说"我测过了"。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .clock import CycleAxis
from .spec import Rule, run_rules
from .vcd import VCD, sig_key


@dataclass
class Mutation:
    """一次故意的规格篡改。

    applies_to 声明它只对哪些规则类型有意义。
    规格里没有该类规则时，这个变异会被跳过并标注"不适用"——
    否则会报出一堆假的"漏掉"，把真正的问题淹没。

    may_be_inconclusive 用于「取反适用条件」这类变异：
    它的鉴别力取决于**数据是否触发过该契约**。如果原始波形里
    这个契约从来没被违反过（比如全程没有 X），那么无论怎么改条件
    都测不出差别——这时应报"无法验证"而不是"漏掉"，
    否则会把"数据不够"误判成"检查失效"。
    """

    name: str
    apply: callable                      # (rules) -> rules，返回被改坏的规则
    applies_to: tuple[str, ...] = ()     # 空元组表示对任何规则都适用
    may_be_inconclusive: bool = False


@dataclass
class MutationResult:
    name: str
    caught: bool
    violations: int
    detail: str = ""
    applicable: bool = True
    inconclusive: bool = False           # 无法用本数据判断（既不算抓住也不算漏掉）


@dataclass
class SelfTestReport:
    results: list[MutationResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(1 for r in self.results if r.applicable and not r.inconclusive)

    @property
    def caught(self) -> int:
        return sum(1 for r in self.results
                   if r.applicable and not r.inconclusive and r.caught)

    @property
    def missed(self) -> list[MutationResult]:
        return [r for r in self.results
                if r.applicable and not r.inconclusive and not r.caught]

    @property
    def skipped(self) -> list[MutationResult]:
        return [r for r in self.results if not r.applicable]

    @property
    def inconclusive(self) -> list[MutationResult]:
        return [r for r in self.results if r.inconclusive]

    @property
    def ok(self) -> bool:
        return not self.missed and self.total > 0

    def to_text(self) -> str:
        lines = ["变异自检：故意改错规格，每条都必须被抓出来",
                 "（抓不住的条目说明这个维度没测到东西）", ""]
        for r in self.results:
            if not r.applicable:
                lines.append(f"  {r.name:<28} {'不适用':>8}   —— 规格里没有这类规则")
                continue
            if r.inconclusive:
                lines.append(f"  {r.name:<28} {'无法验证':>8}   —— {r.detail}")
                continue
            mark = "抓住" if r.caught else "!! 漏掉 !!"
            lines.append(f"  {r.name:<28} 报错 {r.violations:>4} 条   {mark}")
        lines.append("")
        if self.total == 0:
            lines.append("鉴别力：0/0 —— 没有可验证的条目，自检无从进行")
            return "\n".join(lines)
        lines.append(f"鉴别力：{self.caught}/{self.total} 个变异被抓住  -> "
                     f"{'PASS' if self.ok else 'FAIL'}")
        for r in self.missed:
            lines.append(f"  漏掉的：{r.name} —— {r.detail}")
        for r in self.inconclusive:
            lines.append(f"  无法验证：{r.name} —— {r.detail}")
        return "\n".join(lines)


def standard_mutations() -> list[Mutation]:
    """一组通用的规格篡改，覆盖常见维度。

    每条都对应一类真实错误：延迟搞错、条件反了、该保持的没保持、
    该检查的没检查。
    """

    def flip_latency(rules):
        out = copy.deepcopy(rules)
        for r in out:
            if r.kind == "latency":
                r.args["cycles"] = int(r.args.get("cycles", 1)) + 1
        return out

    def drop_latency(rules):
        out = copy.deepcopy(rules)
        for r in out:
            if r.kind == "latency":
                r.args["cycles"] = max(1, int(r.args.get("cycles", 1)) - 1)
        return out

    def invert_when(rules):
        out = copy.deepcopy(rules)
        for r in out:
            cond = r.args.get("when")
            if isinstance(cond, str) and cond:
                r.args["when"] = f"not ({cond})"
        return out

    def force_impossible(rules):
        """把公式改成恒假，任何有值的拍都该报错。"""
        out = copy.deepcopy(rules)
        for r in out:
            if r.kind == "formula":
                r.args["expr"] = "-1"
        return out

    def disable_all(rules):
        """全部关掉——应当一处都不报（这是反向对照，不该"抓住"）。"""
        out = copy.deepcopy(rules)
        for r in out:
            r.enabled = False
        return out

    return [
        Mutation("延迟多算一拍", flip_latency, applies_to=("latency",)),
        Mutation("延迟少算一拍", drop_latency, applies_to=("latency",)),
        Mutation("适用条件取反", invert_when,
                 applies_to=("formula", "stable", "no_x"),
                 may_be_inconclusive=True),
        Mutation("公式改成不可能值", force_impossible, applies_to=("formula",)),
    ]


def inject_x(vcd: VCD, axis: CycleAxis, signal_name: str, cycle: int) -> VCD:
    """造一份「注入了不定态」的波形副本，用来验证 no_x 契约真的有效。

    为什么需要这个：no_x 契约只有在**波形里真的出现 X** 时才可能报错。
    如果原始波形干干净净，那么无论怎么篡改规格都验证不了这条契约——
    检查器可能根本没在工作，而报告照样显示"通过"。

    做法是把指定信号在指定拍的值改成 x，然后看 no_x 规则能不能抓住。
    这属于"改数据"而不是"改规格"，是验证 no_x 唯一有效的办法。
    """
    sig = vcd.find(signal_name)
    if not sig:
        raise ValueError(f"波形里找不到信号 {signal_name}")
    s = sig[0]

    import copy as _copy

    v2 = _copy.copy(vcd)
    v2.traces = dict(vcd.traces)          # 浅拷贝轨迹字典，只替换目标那一条
    key = sig_key(s)
    trace = list(vcd.traces.get(key, []))
    t = axis.time_of(cycle)
    xval = "x" * max(1, s.width)
    # 插到正确的时间位置；同一时刻已有值时替换掉
    trace = [(tt, vv) for tt, vv in trace if tt != t]
    trace.append((t, xval))
    trace.sort(key=lambda x: x[0])
    v2.traces[key] = trace
    return v2


def validate_no_x_rules(
    vcd: VCD,
    axis: CycleAxis,
    rules: list[Rule],
    *,
    constants: dict | None = None,
    signals: dict | None = None,
) -> list[MutationResult]:
    """对每条 no_x 规则做「注入 X」验证。"""
    results: list[MutationResult] = []
    no_x_rules = [r for r in rules if r.kind == "no_x" and r.enabled]
    if not no_x_rules:
        return results

    # 挑一个有效拍来注入（用中间偏后的拍，避开复位和边界）
    if len(axis) - axis.valid_from < 3:
        return results
    probe_cycle = axis.valid_from + max(1, (len(axis) - axis.valid_from) // 2)

    for r in no_x_rules:
        for name in r.args.get("signals", []):
            try:
                broken = inject_x(vcd, axis, name, probe_cycle)
            except Exception as e:                  # noqa: BLE001
                results.append(MutationResult(
                    name=f"注入 X 到 {name}", caught=False, violations=0,
                    detail=f"注入失败：{e}",
                ))
                continue
            try:
                res = run_rules(broken, axis, [r],
                                constants=constants, signals=signals)
            except Exception as e:                  # noqa: BLE001
                results.append(MutationResult(
                    name=f"注入 X 到 {name}", caught=False, violations=0,
                    detail=f"检查报错：{e}",
                ))
                continue
            caught = not res.ok
            results.append(MutationResult(
                name=f"注入 X 到 {name}", caught=caught,
                violations=len(res.violations),
                detail="" if caught else (
                    f"在第 {probe_cycle} 拍把 {name} 改成 X，"
                    f"no_x 契约却没有报错——这条契约是失效的"),
            ))
    return results


def run_selftest(
    vcd: VCD,
    axis: CycleAxis,
    rules: list[Rule],
    *,
    constants: dict | None = None,
    signals: dict | None = None,
    mutations: list[Mutation] | None = None,
) -> SelfTestReport:
    """对一组规则跑变异自检。"""
    mutations = mutations if mutations is not None else standard_mutations()
    report = SelfTestReport()
    present_kinds = {r.kind for r in rules if r.enabled}

    for m in mutations:
        # 规格里没有这类规则时，这个变异无从下手——标"不适用"而不是"漏掉"
        if m.applies_to and not (set(m.applies_to) & present_kinds):
            report.results.append(MutationResult(
                name=m.name, caught=False, violations=0, applicable=False,
                detail=f"规格里没有 {'/'.join(m.applies_to)} 类规则",
            ))
            continue

        try:
            mutated = m.apply(rules)
        except Exception as e:                      # noqa: BLE001
            report.results.append(MutationResult(
                name=m.name, caught=False, violations=0,
                detail=f"变异本身构造失败：{e}",
            ))
            continue

        # 全部关掉的变异是反向对照：它**不该**报错
        if all(not r.enabled for r in mutated):
            report.results.append(MutationResult(
                name=m.name, caught=True, violations=0,
                detail="反向对照：关掉全部规则后没有报错，符合预期",
            ))
            continue

        try:
            res = run_rules(vcd, axis, mutated,
                            constants=constants, signals=signals)
        except Exception as e:                      # noqa: BLE001
            # 变异把规格改成非法状态而报错，也算"抓住了"——
            # 至少没有被静默放过
            report.results.append(MutationResult(
                name=m.name, caught=True, violations=0,
                detail=f"变异被拒绝执行：{e}",
            ))
            continue

        caught = not res.ok
        if caught:
            report.results.append(MutationResult(
                name=m.name, caught=True, violations=len(res.violations)))
        elif m.may_be_inconclusive:
            # 这类变异的鉴别力取决于数据是否触发过该契约。
            # 原始波形里如果从未违反过，改条件也测不出差别——
            # 那是"数据不够"，不是"检查失效"，要分开报。
            report.results.append(MutationResult(
                name=m.name, caught=False, violations=0, inconclusive=True,
                detail="本数据里该契约从未被违反，改条件测不出差别；"
                       "想验证它需要用会违反契约的数据",
            ))
        else:
            report.results.append(MutationResult(
                name=m.name, caught=False, violations=0,
                detail="改错了规格却仍然报通过——该维度的断言是空的",
            ))

    # 再补一层：no_x 契约只能靠"注入 X"来验证，改规格验证不了它
    report.results.extend(
        validate_no_x_rules(vcd, axis, rules,
                            constants=constants, signals=signals)
    )

    return report
