"""命令行接口测试。

CLI 是对外契约，测试要保证：
- 退出码语义正确（通过 = 0，不一致 = 1，用法/环境错误 = 2）——
  这样才能在 CI 和 AI agent 的循环里当判据用
- JSON 输出稳定可解析
- **在非 UTF-8 控制台上也能正常输出中文**（Windows CI 上真实挂过）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXAMPLES = ROOT / "examples"


def cli_env(extra: dict | None = None) -> dict:
    """构造子进程环境。

    **必须在 os.environ 的基础上改，不能从零拼一个。**
    曾经这里写成 {"PYTHONPATH":..., "PATH": ""}，等于把整个环境
    丢掉了——Windows 上会连带丢掉 SYSTEMROOT。缺了它，
    子进程加载系统 DLL 会失败，于是 Windows + Python 3.9 上
    所有 CLI 测试都挂，而其他平台和其他版本却正常。

    从零拼环境还会丢掉 TEMP、USERPROFILE 这些，问题会在别的场合
    再冒出来。复制现有环境再覆盖需要的键才是正确做法。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def run_cli_raw(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """跑 CLI，可以覆盖环境变量（用于模拟各种控制台编码）。"""
    return subprocess.run(
        [sys.executable, "-m", "specdiff.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        env=cli_env(extra_env),
    )


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return run_cli_raw(*args)


@pytest.fixture(scope="module")
def example_files():
    vcd = EXAMPLES / "traffic_bug" / "counter8.vcd"
    golden = EXAMPLES / "golden" / "golden_count.json"
    cfg = EXAMPLES / "traffic_bug" / "map.yaml"
    if not vcd.exists() or not golden.exists():
        pytest.skip("示例波形尚未生成，先跑 examples 下的仿真")
    return vcd, golden, cfg


class TestInfoCommand:
    def test_info_text(self, example_files):
        vcd, _, _ = example_files
        r = run_cli("info", str(vcd))
        assert r.returncode == 0
        assert "信号数量" in r.stdout
        assert "clk" in r.stdout

    def test_info_json(self, example_files):
        vcd, _, _ = example_files
        r = run_cli("info", str(vcd), "--json")
        assert r.returncode == 0
        d = json.loads(r.stdout)
        assert d["signals"] > 0
        assert "signal_list" in d


class TestIndexCommand:
    def test_index_text(self, example_files):
        vcd, _, _ = example_files
        r = run_cli("index", str(vcd))
        assert r.returncode == 0
        assert "时钟" in r.stdout
        assert "周期" in r.stdout
        assert "复位" in r.stdout

    def test_index_json(self, example_files):
        vcd, _, _ = example_files
        r = run_cli("index", str(vcd), "--json")
        assert r.returncode == 0
        d = json.loads(r.stdout)
        assert d["clock"]["signal"].endswith("clk")
        assert d["clock"]["period_fs"] > 0
        assert d["cycles"] > 0

    def test_index_bad_clock_hint(self, example_files):
        vcd, _, _ = example_files
        r = run_cli("index", str(vcd), "--clock", "no_such_signal")
        assert r.returncode == 2
        assert "找不到" in r.stderr


class TestDiffCommand:
    def test_mismatch_exit_code_is_one(self, example_files):
        """有 bug 时退出码必须是 1，这样 CI 和 agent 循环能直接判据。"""
        vcd, golden, cfg = example_files
        r = run_cli("diff", str(vcd), str(golden), "--config", str(cfg))
        assert r.returncode == 1
        assert "不一致" in r.stdout

    def test_diff_json_structure(self, example_files):
        vcd, golden, cfg = example_files
        r = run_cli("diff", str(vcd), str(golden), "--config", str(cfg), "--json")
        d = json.loads(r.stdout)
        assert d["status"] == "mismatch"
        fm = d["first_mismatch"]
        assert fm["cycle"] > 0
        assert isinstance(fm["bits_differing"], list)
        assert fm["expected"] and fm["actual"]
        assert "minimal_window" in d

    def test_verbose_lists_mismatches(self, example_files):
        vcd, golden, cfg = example_files
        r = run_cli("diff", str(vcd), str(golden), "--config", str(cfg), "-v")
        assert "周期" in r.stdout
        assert "期望" in r.stdout

    def test_missing_golden_file(self, example_files):
        vcd, _, cfg = example_files
        r = run_cli("diff", str(vcd), "no_such_file.json", "--config", str(cfg))
        assert r.returncode != 0

    def test_autopair_without_config(self, example_files):
        """不给配置时应尝试按同名自动配对，而不是直接失败。"""
        vcd, golden, _ = example_files
        r = run_cli("diff", str(vcd), str(golden))
        assert r.returncode in (0, 1)
        assert "自动配对" in r.stderr or "不一致" in r.stdout or "通过" in r.stdout


class TestCLIContract:
    def test_version(self):
        r = run_cli("--version")
        assert r.returncode == 0
        assert "specdiff" in r.stdout

    def test_no_command_shows_help(self):
        r = run_cli()
        assert r.returncode != 0
        assert "usage" in (r.stdout + r.stderr).lower()

    def test_unknown_command(self):
        r = run_cli("frobnicate")
        assert r.returncode != 0

    def test_missing_waveform_file(self):
        r = run_cli("info", "no_such_file.vcd")
        assert r.returncode != 0


class TestNonUtf8Console:
    """非 UTF-8 控制台下必须也能正常工作。

    这是 Windows CI 上真实挂过的一类问题：Windows 默认代码页不是 UTF-8，
    输出被重定向到管道时 Python 用那个代码页编码，打印中文直接
    UnicodeEncodeError 崩掉，命令以非预期退出码结束。

    对检查工具来说这尤其致命：**退出码本身就是判据**，
    崩掉会被误读成「检查发现了问题」——比报错更危险。
    """

    @pytest.mark.parametrize("encoding", ["cp1252", "cp936", "ascii"])
    def test_info_does_not_crash(self, example_files, encoding):
        vcd, _, _ = example_files
        r = run_cli_raw("info", str(vcd), extra_env={"PYTHONIOENCODING": encoding})
        assert r.returncode == 0, (
            f"编码 {encoding} 下 info 崩了：\n{r.stderr[-500:]}"
        )

    @pytest.mark.parametrize("encoding", ["cp1252", "cp936", "ascii"])
    def test_check_exit_code_is_still_meaningful(self, example_files, encoding):
        """最重要的一条：非 UTF-8 控制台下，退出码语义不能被破坏。

        通过必须还是 0——否则 CI 会把"检查通过"误判成"检查失败"。
        """
        vcd, _, cfg = example_files
        spec = Path(vcd).parent / "counter.spec.yaml"
        fixed = Path(vcd).parent / "counter8_fixed.vcd"
        if not (spec.exists() and fixed.exists()):
            pytest.skip("规格或修复版波形不存在")

        ok = run_cli_raw("check", str(fixed), str(spec),
                         extra_env={"PYTHONIOENCODING": encoding})
        assert ok.returncode == 0, (
            f"编码 {encoding} 下退出码应为 0，实际 {ok.returncode}\n{ok.stderr[-500:]}"
        )

        bad = run_cli_raw("check", str(vcd), str(spec),
                          extra_env={"PYTHONIOENCODING": encoding})
        assert bad.returncode == 1, (
            f"编码 {encoding} 下退出码应为 1，实际 {bad.returncode}\n{bad.stderr[-500:]}"
        )

    def test_json_output_stays_parseable(self, example_files):
        """非 UTF-8 控制台下 JSON 也必须能被解析（不能混进乱码）。"""
        vcd, _, _ = example_files
        r = run_cli_raw("info", str(vcd), "--json",
                        extra_env={"PYTHONIOENCODING": "cp1252"})
        assert r.returncode == 0
        d = json.loads(r.stdout)
        assert d["signals"] > 0
