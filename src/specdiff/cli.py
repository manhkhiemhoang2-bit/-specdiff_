"""specdiff 命令行入口。

四条命令：
    specdiff info    波形文件                  看波形里有什么
    specdiff index   波形文件                  看时钟/复位识别结果与周期轴
    specdiff diff    波形文件 标准答案 配置     和标准答案逐拍比
    specdiff check   波形文件 规格文件          按规格检查，不需要标准答案
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .clock import ClockError, build_axis
from .diff import GoldenTrace, Mapping, diff, load_mapping
from .spec import SpecError, run_rules
from .specfile import load_spec
from .selftest import run_selftest
from .vcd import parse


def _ensure_utf8_output() -> None:
    """让标准输出/错误尽量用 UTF-8，避免在非 UTF-8 控制台上打印中文时崩溃。

    这是真实踩过的坑：Windows 默认代码页不是 UTF-8（cp936 / cp1252 等）。
    一旦输出被重定向到管道（CI、`| more`、被别的程序调用），
    Python 就用那个代码页去编码，打印中文直接 UnicodeEncodeError 退出。
    Windows CI 上就是这么挂的。

    处理顺序：
      1. 能 reconfigure 就切成 UTF-8 —— 现代终端和 CI 都能正确显示；
      2. 切不了就只把错误策略改成 replace —— 宁可显示成问号，也不能崩。

    宁可显示难看，也不能让命令崩掉：这是个检查工具，
    退出码本身是判据，崩掉会被误读成「检查发现问题」。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            try:
                reconfigure(errors="replace")
            except Exception:      # noqa: BLE001 — 尽力而为，失败不影响功能
                pass


def _fmt_time(fs: int) -> str:
    for unit, scale in (("s", 10**15), ("ms", 10**12), ("us", 10**9),
                        ("ns", 10**6), ("ps", 10**3)):
        if fs >= scale:
            return f"{fs / scale:g}{unit}"
    return f"{fs}fs"


def cmd_info(args) -> int:
    vcd = parse(args.waveform)
    s = vcd.summary()
    if args.json:
        out = dict(s)
        out["signal_list"] = [
            {"path": sig.path, "width": sig.width, "kind": sig.kind,
             "changes": len(vcd.trace_of(sig))}
            for sig in sorted(vcd.signals.values(), key=lambda x: x.path)
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"文件      : {s['file']}")
    print(f"时间精度  : {_fmt_time(vcd.timescale_fs)}")
    print(f"时间范围  : {_fmt_time(vcd.time_min)} ~ {_fmt_time(vcd.time_max)}")
    print(f"信号数量  : {s['signals']}")
    print(f"真实跳变  : {s['changes']}")
    if s["duplicate_events"]:
        print(f"伪事件    : {s['duplicate_events']}（重发当前值，已忽略）")
    print()
    print(f"{'信号':<40} {'位宽':>4} {'跳变':>8}")
    print("-" * 56)
    for sig in sorted(vcd.signals.values(), key=lambda x: x.path):
        n = len(vcd.trace_of(sig))
        print(f"{sig.path:<40} {sig.width:>4} {n:>8}")
    return 0


def cmd_index(args) -> int:
    vcd = parse(args.waveform)
    try:
        axis = build_axis(vcd, clock_hint=args.clock, reset_hint=args.reset,
                          sample_edge=args.edge)
    except ClockError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    info = {
        "clock": {
            "signal": axis.clock.signal.path,
            "period_fs": axis.clock.period_fs,
            "period_human": _fmt_time(axis.clock.period_fs),
            "sample_edge": axis.clock.sample_edge,
            "edge_count": len(axis.clock.rising_edges) + len(axis.clock.falling_edges),
            "duty": axis.clock.duty,
        },
        "reset": None if axis.reset is None else {
            "signal": axis.reset.signal.path,
            "active": "low" if axis.reset.active_low else "high",
            "asserted_windows": axis.reset.asserted,
        },
        "cycles": len(axis),
        "valid_from_cycle": axis.valid_from,
        "first_sample_fs": axis.sample_times[0] if axis.sample_times else None,
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    c = info["clock"]
    print(f"时钟      : {c['signal']}")
    print(f"周期      : {c['period_human']}（{c['period_fs']} fs）")
    print(f"采样边沿  : {'上升沿' if c['sample_edge'] == 'posedge' else '下降沿'}")
    if c["duty"] is not None:
        print(f"占空比    : {c['duty'] * 100:.1f}%")
    if info["reset"]:
        r = info["reset"]
        print(f"复位      : {r['signal']}（{'低' if r['active'] == 'low' else '高'}电平有效，"
              f"{len(r['asserted_windows'])} 段有效区间）")
    else:
        print("复位      : 未识别到（比对时不做屏蔽）")
    print(f"总周期数  : {info['cycles']}")
    print(f"有效起始  : 第 {info['valid_from_cycle']} 拍"
          f"（此前的采样点处于复位期，不参与比对）")
    return 0


def cmd_diff(args) -> int:
    vcd = parse(args.waveform)
    golden = GoldenTrace.from_json(args.golden)

    if args.config:
        mapping = load_mapping(args.config)
    else:
        # 没给配置就按同名自动配对，宽度从波形里取
        mapping = []
        for name in golden.fields:
            hits = vcd.find(name)
            if hits:
                mapping.append(Mapping(rtl=name, golden=name, width=hits[0].width))
        if not mapping:
            print("错误：无法自动配对信号，请用 --config 指定映射文件", file=sys.stderr)
            return 2
        print("（未指定 --config，已按同名自动配对）", file=sys.stderr)

    try:
        result = diff(
            args.waveform, golden, mapping, vcd=vcd,
            clock_hint=args.clock, reset_hint=args.reset, sample_edge=args.edge,
        )
    except ClockError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.to_text())
        if result.status == "mismatch" and args.verbose:
            print()
            print(f"{'周期':>8} {'信号':<24} {'期望':>10} {'实际':>10}")
            print("-" * 56)
            for m in result.mismatches:
                print(f"{m.cycle:>8} {m.signal:<24} {m.expected:>10} {m.actual:>10}")

    return 0 if result.status == "match" else 1


def cmd_check(args) -> int:
    """按规格检查设计，不需要标准答案。

    这是和 diff 最本质的区别：diff 需要你先有一份"正确数据"，
    check 只需要你写下"设计应该怎样"。第一版设计没有参照物时，
    这是唯一可行的方法。
    """
    vcd = parse(args.waveform)

    try:
        spec = load_spec(args.spec)
    except SpecError as e:
        print(f"规格文件错误：{e}", file=sys.stderr)
        return 2

    try:
        axis = build_axis(vcd, clock_hint=args.clock, reset_hint=args.reset,
                          sample_edge=args.edge)
    except ClockError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    # latency 规则需要回推源周期，把采样时刻表交给求值器
    axis_times = list(axis.sample_times)
    for r in spec.rules:
        if r.kind == "latency":
            r.args["_axis_times"] = axis_times

    # 变异自检：故意改错规格，验证检查器有鉴别力
    if args.selftest:
        try:
            report = run_selftest(vcd, axis, spec.rules,
                                  constants=spec.const, signals=spec.signals)
        except SpecError as e:
            print(f"规格错误：{e}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({
                "selftest": True,
                "total": report.total,
                "caught": report.caught,
                "status": "pass" if report.ok else "fail",
                "results": [
                    {"name": r.name, "caught": r.caught,
                     "violations": r.violations, "detail": r.detail}
                    for r in report.results
                ],
            }, ensure_ascii=False, indent=2))
        else:
            print(report.to_text())
        return 0 if report.ok else 1

    try:
        result = run_rules(
            vcd, axis, spec.rules,
            constants=spec.const,
            signals=spec.signals,
            max_violations=args.max,
        )
    except SpecError as e:
        print(f"规格错误：{e}", file=sys.stderr)
        return 2

    if args.json:
        out = {
            "status": "pass" if result.ok else "fail",
            "spec_file": spec.path,
            "valid_from_cycle": axis.valid_from,
            "total_cycles": len(axis),
            "rules": len(spec.rules),
            "checks_run": result.checks_run,
            "checks_skipped": result.checks_skipped,
            "violation_count": len(result.violations),
            "violations": [
                {
                    "cycle": v.cycle,
                    "rule": v.rule,
                    "signal": v.signal,
                    "expected": v.expected,
                    "actual": v.actual,
                    "bits_differing": v.bits,
                    "detail": v.detail,
                }
                for v in result.violations
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    # 人读输出
    if args.verbose:
        print(spec.describe())
        print()

    if result.ok:
        print(f"通过：{len(spec.rules)} 条契约、"
              f"{result.checks_run} 次断言全部满足"
              f"（{len(axis) - axis.valid_from} 个有效周期，"
              f"前 {axis.valid_from} 拍因复位屏蔽）")
        if result.checks_skipped:
            print(f"注意：{result.checks_skipped} 次断言因不定态或数据不足被跳过"
                  f"（这些拍没有被验证到）")
        return 0

    print(f"不满足：{len(result.violations)} 处违反契约"
          f"（共 {len(spec.rules)} 条契约、{result.checks_run} 次断言，"
          f"其中 {result.checks_passed} 次通过）")
    print()
    print(f"{'周期':>8} {'契约':<20} {'信号':<18} {'期望':>10} {'实际':>10}")
    print("-" * 72)
    for v in result.violations[:args.max]:
        bits = ""
        if v.bits:
            bits = " 第 " + "、".join(str(b) for b in v.bits[:6]) + " 位不同"
        print(f"{v.cycle:>8} {v.rule[:19]:<20} {v.signal[:17]:<18} "
              f"{v.expected:>10} {v.actual:>10}{bits}")
    if len(result.violations) > args.max:
        print(f"...（其余 {len(result.violations) - args.max} 处省略）")
    return 1


def main(argv: list[str] | None = None) -> int:
    # 先解决输出编码，否则在非 UTF-8 控制台上打印中文会直接崩
    _ensure_utf8_output()

    ap = argparse.ArgumentParser(
        prog="specdiff",
        description="把仿真波形与标准答案逐拍比对，定位到「第几拍、哪个信号、哪几位」",
    )
    ap.add_argument("--version", action="version", version=f"specdiff {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="查看波形里有哪些信号")
    p_info.add_argument("waveform")
    p_info.add_argument("--json", action="store_true")
    p_info.set_defaults(func=cmd_info)

    p_idx = sub.add_parser("index", help="识别时钟/复位，查看周期轴")
    p_idx.add_argument("waveform")
    p_idx.add_argument("--clock")
    p_idx.add_argument("--reset")
    p_idx.add_argument("--edge", choices=["posedge", "negedge"])
    p_idx.add_argument("--json", action="store_true")
    p_idx.set_defaults(func=cmd_index)

    p_diff = sub.add_parser("diff", help="逐拍比对，输出结论")
    p_diff.add_argument("waveform")
    p_diff.add_argument("golden", help="标准答案 JSON")
    p_diff.add_argument("--config", help="信号映射配置（YAML 或 JSON）")
    p_diff.add_argument("--clock")
    p_diff.add_argument("--reset")
    p_diff.add_argument("--edge", choices=["posedge", "negedge"])
    p_diff.add_argument("--json", action="store_true")
    p_diff.add_argument("--verbose", "-v", action="store_true")
    p_diff.set_defaults(func=cmd_diff)

    p_chk = sub.add_parser(
        "check",
        help="按规格检查设计（不需要标准答案）",
        description="从规格推导每拍的期望值并逐拍检查，定位到具体哪几位不符",
    )
    p_chk.add_argument("waveform")
    p_chk.add_argument("spec", help="规格文件（YAML）")
    p_chk.add_argument("--clock")
    p_chk.add_argument("--reset")
    p_chk.add_argument("--edge", choices=["posedge", "negedge"])
    p_chk.add_argument("--json", action="store_true")
    p_chk.add_argument("--verbose", "-v", action="store_true",
                       help="先打印规格摘要")
    p_chk.add_argument("--max", type=int, default=30,
                       help="最多列出多少处违反（默认 30）")
    p_chk.add_argument("--selftest", action="store_true",
                       help="变异自检：故意改错规格，验证检查器有鉴别力")
    p_chk.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
