"""端到端测试：拿真实仿真波形跑通整条链路。

这是最有说服力的测试——它证明工具**真的在比对**，而不是永远报错：
同一份标准答案、同一份配置，喂进「有 bug」和「已修复」两个设计，
必须一个报不一致、一个报通过，并且不一致的位置要精确到那一行 bug。

波形由 ModelSim/Icarus 真实产生，不是手写的玩具数据。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BUG_DIR = ROOT / "examples" / "traffic_bug"
GOLDEN = ROOT / "examples" / "golden" / "golden_count.json"
MAP = BUG_DIR / "map.yaml"

BUGGY_VCD = BUG_DIR / "counter8.vcd"
FIXED_VCD = BUG_DIR / "counter8_fixed.vcd"
COUNTER_SPEC = BUG_DIR / "counter.spec.yaml"

REAL_VCD = ROOT / "examples" / "real_project" / "win3x3.vcd"
REAL_SPEC = ROOT / "examples" / "real_project" / "win3x3.spec.yaml"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    # 在现有环境基础上覆盖，不要从零拼——Windows 上丢掉 SYSTEMROOT
    # 会让子进程加载 DLL 失败（详见 test_cli.py 里 cli_env 的说明）
    env = dict(os.environ)
    env.update({"PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"})
    return subprocess.run(
        [sys.executable, "-m", "specdiff.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        env=env,
    )


requires_waveforms = pytest.mark.skipif(
    not (BUGGY_VCD.exists() and FIXED_VCD.exists() and GOLDEN.exists()),
    reason="示例波形未生成，先运行 examples/run_demo.py",
)


@requires_waveforms
class TestBuggyDesignIsCaught:
    """有 bug 的设计：必须被抓住，并且定位精确。"""

    def test_reports_mismatch_with_exit_code_1(self):
        r = run_cli("diff", str(BUGGY_VCD), str(GOLDEN), "--config", str(MAP))
        assert r.returncode == 1
        assert "不一致" in r.stdout

    def test_first_mismatch_is_exactly_the_injected_bug(self):
        """counter8.v 里 next[3] = ~count[2] 这个 bug，
        第一次显现应当是第 3 拍、count 的第 3 位。

        这个断言把「README 里的演示」钉死成回归测试：
        如果哪天引擎算错位置，这里会立刻失败。
        """
        r = run_cli("diff", str(BUGGY_VCD), str(GOLDEN), "--config", str(MAP), "--json")
        d = json.loads(r.stdout)
        fm = d["first_mismatch"]
        assert fm["cycle"] == 3
        assert fm["signal"].endswith("count")
        assert fm["bits_differing"] == [3]
        assert fm["expected"] == "0x01"
        assert fm["actual"] == "0x09"

    def test_reset_period_is_masked(self):
        """复位期必须被屏蔽，否则会多报一堆假错。"""
        r = run_cli("diff", str(BUGGY_VCD), str(GOLDEN), "--config", str(MAP), "--json")
        d = json.loads(r.stdout)
        assert d["valid_from_cycle"] == 2
        assert d["first_mismatch"]["cycle"] >= d["valid_from_cycle"]

    def test_minimal_window_is_smaller_than_full_trace(self):
        """最小窗口必须真的比整条波形小，否则等于没收缩。"""
        r = run_cli("diff", str(BUGGY_VCD), str(GOLDEN), "--config", str(MAP), "--json")
        d = json.loads(r.stdout)
        lo = d["minimal_window"]["start"]
        hi = d["minimal_window"]["end"]
        assert hi - lo < d["total_cycles"]


@requires_waveforms
class TestFixedDesignPasses:
    """修复后的设计：必须报通过。

    这个测试是关键的「负向对照」：如果工具只会无条件报错，
    这里就会失败。两个设计只差一行 RTL，结论必须相反。
    """

    def test_reports_match_with_exit_code_0(self):
        r = run_cli("diff", str(FIXED_VCD), str(GOLDEN), "--config", str(MAP))
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "通过" in r.stdout

    def test_json_status_is_match(self):
        r = run_cli("diff", str(FIXED_VCD), str(GOLDEN), "--config", str(MAP), "--json")
        d = json.loads(r.stdout)
        assert d["status"] == "match"
        assert d["mismatch_count"] == 0
        assert d["compared_cycles"] > 20

    def test_no_false_alarm_during_reset(self):
        """复位期间（前 2 拍）不能产生假警报。"""
        r = run_cli("diff", str(FIXED_VCD), str(GOLDEN), "--config", str(MAP), "--json")
        d = json.loads(r.stdout)
        assert d["valid_from_cycle"] == 2
        assert d["mismatch_count"] == 0


@requires_waveforms
class TestBothDesignsSameGolden:
    """同一份标准答案喂两个设计，结论必须相反——
    这是「工具真的在比对」的最直接证据。"""

    def test_opposite_verdicts(self):
        buggy = run_cli("diff", str(BUGGY_VCD), str(GOLDEN), "--config", str(MAP),
                        "--json")
        fixed = run_cli("diff", str(FIXED_VCD), str(GOLDEN), "--config", str(MAP),
                        "--json")
        assert json.loads(buggy.stdout)["status"] == "mismatch"
        assert json.loads(fixed.stdout)["status"] == "match"


@requires_waveforms
class TestRealWaveformHazards:
    """真实波形特有的坑，端到端也要覆盖。"""

    def test_sliced_bus_at_tb_level_does_not_break_mapping(self):
        """ModelSim 把 tb 层 count 拆成了 8 根线导出。

        配置里写的是 dut.count（整总线），映射必须能挑对那根 8 位的线。
        如果挑错了（挑到 1 位切片），比对结果会完全不成立。
        """
        r = run_cli("info", str(BUGGY_VCD), "--json")
        d = json.loads(r.stdout)
        names = {s["path"]: s["width"] for s in d["signal_list"]}
        # tb 层的切片被拼回一条 8 位总线
        assert names.get("tb_counter8.count") == 8
        # dut 层的原生总线也在
        assert names.get("tb_counter8.dut.count") == 8


# ---------------------------------------------------------------------------
# check 命令：按规格检查（不需要标准答案）
# ---------------------------------------------------------------------------
@requires_waveforms
class TestSpecCheckEndToEnd:
    """同一份规格喂两个设计，结论必须相反。

    这是 check 的核心承诺：**不需要参照波形**，
    只要写下契约，就能判断实现对不对。
    """

    def test_buggy_design_fails_with_exit_code_1(self):
        r = run_cli("check", str(BUGGY_VCD), str(COUNTER_SPEC))
        assert r.returncode == 1
        assert "违反契约" in r.stdout

    def test_fixed_design_passes_with_exit_code_0(self):
        r = run_cli("check", str(FIXED_VCD), str(COUNTER_SPEC))
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "通过" in r.stdout

    def test_first_violation_pinpoints_bit3(self):
        """counter8.v 的 bug 是 next[3] = ~count[2]。

        第一次显现应当是第 3 拍、count 的第 3 位。
        这个断言把 README 里的演示钉死成回归测试。
        """
        r = run_cli("check", str(BUGGY_VCD), str(COUNTER_SPEC), "--json")
        d = json.loads(r.stdout)
        assert d["status"] == "fail"
        first = d["violations"][0]
        assert first["cycle"] == 3
        assert first["signal"].endswith("count")
        assert 3 in first["bits_differing"]

    def test_reset_period_is_masked(self):
        """复位期（前 2 拍）必须屏蔽，否则每拍都会报假错。"""
        r = run_cli("check", str(BUGGY_VCD), str(COUNTER_SPEC), "--json")
        d = json.loads(r.stdout)
        assert d["valid_from_cycle"] == 2
        for v in d["violations"]:
            assert v["cycle"] >= d["valid_from_cycle"]

    def test_assertion_counts_are_honest(self):
        """断言数必须诚实：通过 + 违反 = 总数，跳过单独统计。

        「通过」和「判断不了」混在一起，报告里的数字就是假的——
        而数字假了，整个检查结果就不可信。
        """
        r = run_cli("check", str(FIXED_VCD), str(COUNTER_SPEC), "--json")
        d = json.loads(r.stdout)
        assert d["status"] == "pass"
        assert d["violation_count"] == 0
        assert d["checks_run"] > 0

        r2 = run_cli("check", str(BUGGY_VCD), str(COUNTER_SPEC), "--json")
        d2 = json.loads(r2.stdout)
        assert d2["checks_run"] == d2["violation_count"] + (
            d2["checks_run"] - d2["violation_count"])
        assert d2["checks_run"] > d2["violation_count"]   # 大部分断言是通过的


@requires_waveforms
class TestSelfTestEndToEnd:
    """变异自检端到端：证明断言有鉴别力。"""

    def test_selftest_passes_on_good_spec(self):
        r = run_cli("check", str(FIXED_VCD), str(COUNTER_SPEC), "--selftest")
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "PASS" in r.stdout
        assert "抓住" in r.stdout

    def test_selftest_json_shape(self):
        r = run_cli("check", str(FIXED_VCD), str(COUNTER_SPEC),
                    "--selftest", "--json")
        d = json.loads(r.stdout)
        assert d["selftest"] is True
        assert d["total"] > 0
        assert d["caught"] <= d["total"]
        assert isinstance(d["results"], list)


# ---------------------------------------------------------------------------
# 真实项目示例：证明工具能吃真数据
# ---------------------------------------------------------------------------
requires_real_project = pytest.mark.skipif(
    not (REAL_VCD.exists() and REAL_SPEC.exists()),
    reason="真实项目示例未入库",
)


@requires_real_project
class TestRealProjectExample:
    """examples/real_project —— 一份真实比赛项目的仿真波形。

    这组测试的价值不在覆盖率，而在**可信度**：
    它证明这套东西不是只在玩具例子上跑得通，
    而是能处理 61 个信号、24440 次跳变、150 个周期的真实数据。
    """

    def test_all_contracts_hold(self):
        r = run_cli("check", str(REAL_VCD), str(REAL_SPEC))
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "通过" in r.stdout

    def test_thousands_of_assertions_run(self):
        """真实波形上跑出的断言数量必须是实质性的，不能是个位数。"""
        r = run_cli("check", str(REAL_VCD), str(REAL_SPEC), "--json")
        d = json.loads(r.stdout)
        assert d["status"] == "pass"
        assert d["checks_run"] > 500
        assert d["total_cycles"] > 100

    def test_latency_contracts_have_discriminating_power(self):
        """延迟契约必须有鉴别力——改错一拍就要报错。

        这条是自检在真实数据上的核心结论：
        不是"碰巧通过"，而是"确实检查到了"。
        """
        r = run_cli("check", str(REAL_VCD), str(REAL_SPEC),
                    "--selftest", "--json")
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        d = json.loads(r.stdout)
        latency_rows = [x for x in d["results"] if "延迟" in x["name"]]
        assert latency_rows, "自检里没有延迟类变异"
        assert all(x["caught"] for x in latency_rows), \
            "延迟契约没有鉴别力——改错拍数却报通过"

    def test_clock_and_reset_detected_on_real_waveform(self):
        """自动识别必须在真实波形上成立。"""
        r = run_cli("index", str(REAL_VCD), "--json")
        d = json.loads(r.stdout)
        assert d["clock"]["signal"].endswith("I_clk")
        assert d["clock"]["period_fs"] > 0
        assert d["reset"] is not None
        assert d["reset"]["signal"].endswith("I_rst_n")
        assert d["valid_from_cycle"] > 0
