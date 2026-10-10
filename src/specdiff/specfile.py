"""规格文件读写。

把用户写的 YAML 规格翻译成 Rule 对象。规格文件长这样：

    # 信号别名：把波形里的真实路径映射成好记的短名
    signals:
      I_tvalid: dut.I_tvalid
      O_tvalid: dut.O_tvalid

    # 常量：算法参数、图像尺寸之类
    const:
      W: 32
      H: 8

    # 契约
    rules:
      - name: 输出延迟 2 拍
        kind: latency
        cycles: 2
        pairs:
          - [O_tvalid, I_tvalid]
          - [O_tdata,  I_tdata]

      - name: 窗口中心像素
        kind: formula
        target: O_center
        when: O_tvalid == 1
        expr: in.I_tdata

      - name: 握手期间数据保持
        kind: stable
        when: in.I_tvalid == 1 and in.I_tready == 0
        signals: [I_tdata]

      - name: 有效输出不得为 X
        kind: no_x
        when: O_tvalid == 1
        signals: [O_tdata, O_window]
"""

from __future__ import annotations

from pathlib import Path

from .spec import Rule, SpecError
from .yamlmini import YamlError, parse as parse_yaml

_VALID_KINDS = {"latency", "formula", "stable", "no_x"}


class SpecFile:
    """一份规格：别名表 + 常量 + 规则列表。"""

    def __init__(self, signals: dict[str, str], const: dict, rules: list[Rule],
                 path: str = ""):
        self.signals = signals
        self.const = const
        self.rules = rules
        self.path = path

    def __len__(self) -> int:
        return len(self.rules)

    def describe(self) -> str:
        lines = [f"规格文件：{self.path or '(内存)'}"]
        lines.append(f"  信号别名：{len(self.signals)} 条")
        lines.append(f"  常量：{len(self.const)} 个")
        lines.append(f"  契约：{len(self.rules)} 条")
        for r in self.rules:
            mark = "开" if r.enabled else "关"
            note = f"  # {r.note}" if r.note else ""
            lines.append(f"    [{mark}] {r.name}（{r.kind}）{note}")
        return "\n".join(lines)


def load_spec(path: str | Path) -> SpecFile:
    """读规格文件。写法有误时抛 SpecError，而不是静默用默认值。"""
    p = Path(path)
    if not p.exists():
        raise SpecError(f"规格文件不存在：{p}")
    try:
        data = parse_yaml(p.read_text(encoding="utf-8"))
    except YamlError as e:
        raise SpecError(f"规格文件 {p} 格式有误：{e}") from e

    signals: dict[str, str] = {}
    raw_signals = data.get("signals") or {}
    if isinstance(raw_signals, dict):
        for k, v in raw_signals.items():
            if v is None:
                raise SpecError(f"signals 段的 '{k}' 没有写对应的波形路径")
            signals[str(k)] = str(v)
    elif isinstance(raw_signals, list):
        # 也接受 [- name: xxx, path: yyy] 这种写法
        for item in raw_signals:
            if not isinstance(item, dict) or "name" not in item:
                raise SpecError("signals 段写成列表时，每项必须含 name 和 path")
            signals[str(item["name"])] = str(item.get("path", item["name"]))

    const = data.get("const") or {}
    if not isinstance(const, dict):
        raise SpecError("const 段必须是键值对")

    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        raise SpecError("rules 段必须是列表")

    rules: list[Rule] = []
    for i, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            raise SpecError(f"第 {i + 1} 条规则必须是键值对")
        kind = str(item.get("kind", "")).strip()
        if kind not in _VALID_KINDS:
            raise SpecError(
                f"第 {i + 1} 条规则的 kind='{kind}' 不认识。"
                f"支持的类型：{', '.join(sorted(_VALID_KINDS))}"
            )
        name = str(item.get("name") or f"{kind}_{i + 1}")

        # pairs 允许写成 [[out, in], ...] 或 [[out, in, width], ...]
        pairs = []
        for pr in item.get("pairs", []) or []:
            if isinstance(pr, (list, tuple)) and len(pr) >= 2:
                pairs.append((str(pr[0]), str(pr[1])))
            else:
                raise SpecError(
                    f"规则 '{name}' 的 pairs 每项应当是 [输出信号, 输入信号]"
                )

        args = {
            "cycles": item.get("cycles"),
            "pairs": pairs,
            "expr": item.get("expr"),
            "target": item.get("target"),
            "when": item.get("when"),
            "signals": [str(s) for s in (item.get("signals") or [])],
        }
        # 只丢掉"压根没写"的键；写成 0 / False / 空列表都是合法取值，
        # 保留下来交给求值层判断（这里曾经用真值过滤，导致 expr: 0 被吃掉）
        rules.append(Rule(
            kind=kind,
            name=name,
            args={k: v for k, v in args.items()
                  if v is not None and not (k == "pairs" and v == [])},
            enabled=bool(item.get("enabled", True)),
            note=str(item.get("note", "") or ""),
        ))

    return SpecFile(signals=signals, const=const, rules=rules, path=str(p))
