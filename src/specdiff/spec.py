"""规格层：把「设计应该怎样」写成可执行的规则，直接算出每拍的期望值。

这是和"两份波形互比"最本质的区别。

**为什么需要它**：两份波形互比有个前提——你得先有一份**确认正确**的波形。
但设计的第一版没有参照物；而且"上次跑通过"不等于"这次是对的"
（可能两次都错，只是错得一样）。

规格层走的是另一条路：**从设计契约推导期望值**，而不是从另一份实现抄。
契约来自需求文档、接口协议、算法公式——独立于任何 RTL 实现。
所以它能抓住"实现和规格不符"，这是拿两份实现互比永远抓不到的。

内置四类规则，覆盖同步电路最常见的契约：

    latency   握手信号必须逐拍延迟 N 拍
    formula   某个输出必须逐拍等于某个表达式（用输入和状态算）
    stable    某信号在两拍之间不得变化（用于协议稳定性）
    no_x      输出不得出现不定态 x/z

规则用 Python 表达式描述，环境里可以引用：
    cur.<信号名>    该拍的值（当前拍采样到的值）
    prev.<信号名>   上一拍的值；第一拍取不到，返回 None
    out.<信号名>    同 cur，写成 out 更贴合"断言输出"的语感
    inp.<信号名>    同 cur，写成 inp 更贴合"引用输入"的语感
    cyc             当前拍号（从 0 开始，已按有效起点重编号）
    const.<名字>    spec 文件里定义的常量

**注意**：当前拍的值不能写成 `in.xxx`——`in` 是 Python 关键字，
`in.count` 是语法错误，永远解析不了。所以用 `cur` / `out` / `inp`。

安全说明：表达式用受限的 eval 执行，只开放数学运算和比较，
不开放 import、属性访问之外的任何能力。规格文件是**你自己写的配置**，
不是从外部下载的，但即便如此也不放开危险操作。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .clock import CycleAxis
from .vcd import VCD


class SpecError(Exception):
    """规格文件本身写错了（不是设计错了）。"""


# ---------------------------------------------------------------------------
# 数值解析：波形里的值是二进制串，规格里写的是数字
# ---------------------------------------------------------------------------
def to_int(value: Any, width: int | None = None) -> int | None:
    """把波形值统一成整数。遇到 x/z 返回 None（不定态无从判断对错）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip().lower()
    if s in ("x", "z", ""):
        return None
    if s.startswith("0x"):
        try:
            return int(s, 16)
        except ValueError:
            return None
    if s.startswith("0b"):
        try:
            return int(s, 2)
        except ValueError:
            return None
    if set(s) <= set("01"):
        try:
            return int(s, 2)
        except ValueError:
            return None
    if any(c in s for c in "xz"):
        return None
    try:
        return int(s, 10)
    except ValueError:
        return None


def _unwrap(value: Any) -> Any:
    """把波形里的值转成算术友好的类型：位串 → 整数，实数 → 浮点。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.lower() in ("x", "z") or any(c in s.lower() for c in "xz"):
        return None
    # 纯 01 串按二进制整数处理（VCD 里总线就是这个形式）
    if set(s) <= set("01"):
        return int(s, 2)
    try:
        return float(s)
    except ValueError:
        return None


class _Bag:
    """按信号名取值的小容器，支持属性式访问 `in.clk`，也支持下标 `in['clk']`。

    两种取值语义要分清：
    - `in` / `out`：名字必须是规格里声明过的，写错要**明确报错**，
      否则拼错信号名会静默跳过检查，那比报错危险得多。
    - `prev`：第一拍还没有"上一拍"，取不到是**正常情况**，返回 None，
      让规格里的 `when` 判断去处理。
    """

    def __init__(self, values: dict[str, Any], *, strict: bool = True):
        self._v = values
        self._strict = strict

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._v:
            if not self._strict:
                return None
            raise SpecError(
                f"表达式引用了不存在的信号 '{name}'。"
                f"可用信号：{', '.join(sorted(self._v)) or '（无）'}"
            )
        return _unwrap(self._v[name])

    def __getitem__(self, name: str) -> Any:
        return self.__getattr__(name)

    def get(self, name: str, default: Any = None) -> Any:
        return _unwrap(self._v[name]) if name in self._v else default

    def __contains__(self, name: str) -> bool:
        return name in self._v

    def __repr__(self) -> str:
        return f"Bag({sorted(self._v)})"


# 只放行安全的数学与比较函数
_SAFE_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    "abs": abs, "min": min, "max": max, "int": int, "float": float,
    "bool": bool, "len": len, "range": range, "round": round,
    "sum": sum, "pow": pow, "divmod": divmod,
    "any": any, "all": all, "sorted": sorted,
}


def safe_eval(expr: Any, env: dict[str, Any]) -> Any:
    """受限求值。规格文件是本地配置，但仍不开放危险能力。

    表达式可以不是字符串：YAML 里写 `expr: 0` 会被解析成整数 0，
    这是合法的（就是"恒等于 0"的意思），直接返回它即可。
    """
    if not isinstance(expr, str):
        return expr
    try:
        return eval(expr, _SAFE_GLOBALS, env)  # noqa: S307 — 已限制全局命名空间
    except SpecError:
        raise
    except NameError as e:
        raise SpecError(f"表达式 {expr!r} 引用了未定义的名称：{e}") from e
    except Exception as e:
        raise SpecError(f"表达式 {expr!r} 求值失败：{type(e).__name__}: {e}") from e


# ---------------------------------------------------------------------------
# 规则定义
# ---------------------------------------------------------------------------
@dataclass
class Violation:
    """一条契约被违反。"""

    cycle: int
    rule: str
    detail: str
    signal: str = ""
    expected: str = ""
    actual: str = ""
    bits: list[int] = field(default_factory=list)


@dataclass
class Rule:
    """一条契约。

    kind 决定语义，args 是它的参数。所有规则都在「周期轴」上逐拍求值，
    所以复位期已经被屏蔽，不需要每条规则自己处理。
    """

    kind: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    note: str = ""


@dataclass
class SpecResult:
    violations: list[Violation] = field(default_factory=list)
    checks_run: int = 0         # 真正做出判断的断言数（通过 + 违反）
    checks_skipped: int = 0     # 因不定态/数据不足而无法判断的断言数
    rules: list[Rule] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def checks_passed(self) -> int:
        return self.checks_run - len(self.violations)


# 「这一拍判断不了」的哨兵。
# 必须和 None（= 通过）区分开，否则统计会把"通过"和"跳过"混为一谈，
# 报告出来的断言数就是错的——数字错了，整个检查的可信度就没了。
SKIP = object()


def _hex(v: int, width: int) -> str:
    digits = max(1, (width + 3) // 4)
    return f"0x{v:0{digits}X}"


def run_rules(
    vcd: VCD,
    axis: CycleAxis,
    rules: list[Rule],
    *,
    constants: dict[str, Any] | None = None,
    signals: dict[str, str] | None = None,
    max_violations: int = 50,
) -> SpecResult:
    """在周期轴上逐拍执行所有规则。

    signals: 逻辑名 → 波形里的实际信号路径。
             规格里写 `O_tvalid` 比写 `tb.dut.O_tvalid` 顺眼，
             这个映射负责把前者翻成后者。
    """
    const = dict(constants or {})
    alias = dict(signals or {})
    result = SpecResult(rules=rules)

    # 把用到的信号一次性取出来，避免在循环里反复查找
    needed: set[str] = set()
    for r in rules:
        needed.update(r.args.get("signals", []))
        for key in ("target", "source", "signal"):
            if key in r.args:
                needed.add(r.args[key])
        # latency 规则的信号写在 pairs 里，别漏了
        for pair in r.args.get("pairs", []) or []:
            needed.update(pair[:2])
        # 表达式里引用的信号：靠正则粗提取 cur./prev./out./inp. 后面的名字
        for text in (r.args.get("expr", ""), r.args.get("when", "")):
            if text is None:
                continue
            for m in re.finditer(r"\b(?:cur|prev|out|inp)\.([A-Za-z_]\w*)", str(text)):
                needed.add(m.group(1))

    resolved: dict[str, str] = {}
    for name in needed:
        path = alias.get(name, name)
        hits = vcd.find(path)
        if not hits:
            raise SpecError(
                f"规格里用到的信号 '{name}' 在波形里找不到"
                f"（实际查找的路径是 '{path}'）。"
                f"可以用 signals 段把它映射到波形里的真实路径。"
            )
        resolved[name] = hits[0].path

    sig_objs = {n: vcd.signals[p] for n, p in resolved.items()}

    def value_at(name: str, t: int) -> Any:
        return vcd.values_at(sig_objs[name], t)

    prev_values: dict[str, Any] = {}

    for cyc in range(axis.valid_from, len(axis)):
        t = axis.time_of(cyc)

        in_vals = {n: value_at(n, t) for n in resolved}
        cur_bag = _Bag(in_vals)
        prev_bag = _Bag(prev_values, strict=False)
        # cur / out / inp 指向同一个容器，只是语感不同：
        # 断言输出时写 out.xxx，引用输入时写 inp.xxx，都指"这一拍的值"。
        env = {
            "cur": cur_bag, "out": cur_bag, "inp": cur_bag,
            "prev": prev_bag,
            "cyc": cyc,
            "const": _Bag(const),
        }

        for r in rules:
            if not r.enabled:
                continue
            try:
                v = _eval_rule(r, env, cyc, sig_objs, value_at, t)
            except SpecError:
                raise
            if v is SKIP:
                result.checks_skipped += 1
                continue
            result.checks_run += 1
            if isinstance(v, Violation):
                result.violations.append(v)
                if len(result.violations) >= max_violations:
                    prev_values = {n: value_at(n, t) for n in resolved}
                    return result

        prev_values = {n: value_at(n, t) for n in resolved}

    return result


def _eval_rule(r: Rule, env: dict, cyc: int, sig_objs: dict,
               value_at: Callable, t: int):
    """求值一条规则。

    返回三种结果，必须严格区分：
        Violation  —— 违反契约
        None       —— 通过
        SKIP       —— 判断不了（不定态、数据不足、还没到起始拍）
    把"通过"和"判断不了"混为一谈，报告里的断言数就是假的。
    """
    kind = r.kind

    if kind == "no_x":
        # 输出不得出现不定态
        for name in r.args.get("signals", []):
            raw = value_at(name, t)
            if raw is None:
                return SKIP
            s = str(raw).lower()
            if "x" in s or "z" in s:
                return Violation(
                    cycle=cyc, rule=r.name, signal=name,
                    detail=f"{name} 出现不定态 {raw!r}（有效拍上不允许）",
                    expected="确定值", actual=str(raw),
                )
        return None

    if kind == "stable":
        # 某信号在两拍之间不得变化（协议稳定性、握手保持）
        cond = r.args.get("when")
        if cond is not None and not safe_eval(cond, env):
            return SKIP
        for name in r.args.get("signals", []):
            cur = value_at(name, t)
            if cur is None:
                return SKIP
            prev = env["prev"].get(name)
            if prev is None:
                return SKIP
            # 两边都按数值比较：波形里是 "00000101"，prev 已经是 5。
            # 直接比字符串会把"没变"误判成"变了"。
            c_val = _unwrap(cur)
            p_val = _unwrap(prev)
            same = (c_val == p_val) if (c_val is not None and p_val is not None) \
                else (str(cur) == str(prev))
            if not same:
                return Violation(
                    cycle=cyc, rule=r.name, signal=name,
                    detail=f"{name} 在约束期内不应变化，但上一拍 {prev!r} → 本拍 {cur!r}",
                    expected=str(prev), actual=str(cur),
                )
        return None

    if kind == "latency":
        # out 信号必须逐拍等于 N 拍之前的 in 信号
        n = int(r.args.get("cycles", 1))
        pairs = r.args.get("pairs", [])
        src_cyc = cyc - n
        if src_cyc < 0:
            return SKIP
        t_src = None
        # 用周期轴回推源时刻
        axis_times = r.args.get("_axis_times")
        if axis_times and src_cyc < len(axis_times):
            t_src = axis_times[src_cyc]
        if t_src is None:
            return SKIP
        for out_name, in_name in pairs:
            got = value_at(out_name, t)
            want = value_at(in_name, t_src)
            if got is None or want is None:
                return SKIP
            g, w = to_int(got), to_int(want)
            if g is None or w is None:
                if str(got) != str(want):
                    return Violation(
                        cycle=cyc, rule=r.name, signal=out_name,
                        detail=f"{out_name} 应等于 {n} 拍前的 {in_name}",
                        expected=str(want), actual=str(got),
                    )
                continue
            if g != w:
                width = sig_objs[out_name].width
                xor = g ^ w
                bits = [i for i in range(width) if (xor >> i) & 1]
                return Violation(
                    cycle=cyc, rule=r.name, signal=out_name,
                    expected=_hex(w, width), actual=_hex(g, width), bits=bits,
                    detail=(f"{out_name} 应在第 {cyc} 拍等于 {n} 拍前"
                            f"（第 {src_cyc} 拍）的 {in_name}"),
                )
        return None

    if kind == "formula":
        # 某个目标信号必须逐拍满足一个表达式
        target = r.args.get("target")
        if not target:
            raise SpecError(f"规则 '{r.name}' 是 formula 类型，但没写 target")
        expr = r.args.get("expr")
        # 注意用 is None 判断：expr 写成 0 或 False 都是合法的表达式
        if expr is None:
            raise SpecError(f"规则 '{r.name}' 是 formula 类型，但没写 expr")

        cond = r.args.get("when")
        if cond is not None and not safe_eval(cond, env):
            return SKIP      # 这一拍不适用该契约，不是"通过"

        raw = value_at(target, t)
        if raw is None:
            return SKIP
        actual = _unwrap(raw)
        if actual is None:
            return SKIP

        want = safe_eval(expr, env)
        if want is None:
            return SKIP
        if isinstance(want, (int, float)) and isinstance(actual, (int, float)):
            if abs(float(actual) - float(want)) < 1e-9:
                return None
        elif actual == want:
            return None

        width = sig_objs[target].width
        bits: list[int] = []
        if isinstance(actual, int) and isinstance(want, int):
            xor = actual ^ want
            bits = [i for i in range(width) if (xor >> i) & 1]
        exp_s = _hex(int(want), width) if isinstance(want, int) else str(want)
        act_s = _hex(int(actual), width) if isinstance(actual, int) else str(actual)
        return Violation(
            cycle=cyc, rule=r.name, signal=target,
            expected=exp_s, actual=act_s, bits=bits,
            detail=f"{target} 不满足规格表达式 {expr}",
        )

    raise SpecError(
        f"未知的规则类型 '{kind}'。"
        f"支持：latency / formula / stable / no_x"
    )
