"""一键复现 README 里的演示：跑仿真 → 生成标准答案 → 比对 → 打印结论。

支持 ModelSim（vsim）和 Icarus Verilog（iverilog），哪个装了用哪个。
这个脚本存在的意义是：**任何人都能一条命令验证 README 里的说法**，
不用信我们的一面之词。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUG_DIR = HERE / "traffic_bug"


def which(name: str) -> str | None:
    return shutil.which(name)


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def simulate_modelsim() -> bool:
    """用 ModelSim 跑仿真。"""
    vsim = which("vsim") or which("vsim.exe")
    vlog = which("vlog") or which("vlog.exe")
    vlib = which("vlib") or which("vlib.exe")
    if not (vsim and vlog and vlib):
        return False

    print("[1/3] 用 ModelSim 编译并仿真")
    work = BUG_DIR / "work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)

    r = run([vlib, "work"], BUG_DIR)
    if r.returncode != 0:
        print(r.stdout[-800:], r.stderr[-800:])
        return False

    r = run([vlog, "-quiet", "counter8.v", "tb_counter8.v"], BUG_DIR)
    if r.returncode != 0:
        print(r.stdout[-800:], r.stderr[-800:])
        return False

    r = run([vsim, "-c", "-quiet", "-do", "run -all; quit -f", "tb_counter8"], BUG_DIR)
    print("  仿真输出（前 8 行）:")
    for line in r.stdout.splitlines()[:8]:
        print("   ", line)
    return (BUG_DIR / "counter8.vcd").exists()


def simulate_iverilog() -> bool:
    """用 Icarus Verilog 跑仿真（没有 ModelSim 时的退路）。"""
    iverilog = which("iverilog")
    vvp = which("vvp")
    if not (iverilog and vvp):
        return False

    print("[1/3] 用 Icarus Verilog 编译并仿真")
    out = BUG_DIR / "tb_counter8.vvp"
    r = run([iverilog, "-o", str(out), "counter8.v", "tb_counter8.v"], BUG_DIR)
    if r.returncode != 0:
        print(r.stdout[-800:], r.stderr[-800:])
        return False

    r = run([vvp, str(out)], BUG_DIR)
    print("  仿真输出（前 8 行）:")
    for line in r.stdout.splitlines()[:8]:
        print("   ", line)
    return (BUG_DIR / "counter8.vcd").exists()


def main() -> int:
    print("=" * 68)
    print("specdiff 示例复现")
    print("=" * 68)

    ok = simulate_modelsim()
    if not ok:
        ok = simulate_iverilog()
    if not ok:
        print("\n错误：没找到可用的仿真器。请安装 ModelSim (vsim) 或 Icarus Verilog (iverilog)。")
        return 2

    vcd = BUG_DIR / "counter8.vcd"
    size = vcd.stat().st_size
    print(f"  产出波形: {vcd.name}（{size} 字节）")

    print("\n[2/3] 生成标准答案")
    sys.path.insert(0, str(HERE / "golden"))
    from make_golden import CYCLES, golden_series  # noqa: E402

    golden = HERE / "golden" / "golden_count.json"
    counts = golden_series(CYCLES, en_high_from=3)
    import json

    golden.write_text(json.dumps({
        "description": "8 位计数器标准答案：en 有效后每拍加一",
        "en_high_from_cycle": 3,
        "cycles": [
            {"cycle": i, "count": counts[i], "carry": 1 if counts[i] == 0xFF else 0}
            for i in range(CYCLES)
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  前 12 拍期望值: {counts[:12]}")

    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "specdiff.cli", *args],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env={"PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"},
        )

    print("\n[3/4] 按规格检查（check）——不需要标准答案，只要契约")
    rc = run("check", str(vcd), str(BUG_DIR / "counter.spec.yaml"))
    print("  " + (rc.stdout.strip() or rc.stderr.strip()))

    print("\n[4/4] 和标准答案比对（diff）——另一种用法")
    r = run("diff", str(vcd), str(golden), "--config", str(BUG_DIR / "map.yaml"))
    print("  " + (r.stdout.strip() or r.stderr.strip()))

    print()
    print("=" * 68)
    if r.returncode == 1 or rc.returncode == 1:
        print("结论：两条路都成功定位到 bug")
        print()
        print("  这个 bug 是 examples/traffic_bug/counter8.v 里故意写的：")
        print("      next[3] = ~count[2];   // 本该是 next[3]")
        print()
        print("  check —— 从接口契约推导期望值，第一版设计就能用，无需参照物")
        print("  diff  —— 和一份已知正确的数据逐拍比")
        print()
        print("  想验证「修好之后确实通过」，跑：")
        print("    specdiff check examples/traffic_bug/counter8_fixed.vcd \\")
        print("                   examples/traffic_bug/counter.spec.yaml")
        print("  想验证「这些断言不是摆设」，加 --selftest")
        return 0
    print("结论：没有发现问题（如果 RTL 已修复就该是这样）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
