# 发布与维护手册

这份文档记录把 specdiff 发到 GitHub 的完整步骤，以及发布后要做什么。
首次发布已经完成，下面标了当前状态，以后再发版本时照着走。

---

## 当前状态

| 项目 | 状态 |
|---|---|
| 仓库地址 | https://github.com/manhkhiemhoang2-bit/-specdiff_ |
| 可见性 | 公开 |
| 默认分支 | `main` |
| 许可证 | MIT |
| CI | 12 个组合全绿（Windows/macOS/Linux × Python 3.9/3.11/3.12/3.13） |
| 标签 | `v0.1.0` |
| 话题标签 | 待填 |
| Release | 待建 |

---

## 一、仓库地址占位符

README、`pyproject.toml`、`CHANGELOG.md` 里各有一处仓库地址，首次发布时是 `yourname`，已经替换成 `manhkhiemhoang2-bit`。

以后如果改仓库名或换账号，三处都要同步改：

```powershell
cd C:\Users\29072\deepseek_harness_workspace\specdiff
$me = "新的用户名"
(Get-Content README.md)      -replace 'manhkhiemhoang2-bit', $me | Set-Content README.md
(Get-Content pyproject.toml) -replace 'manhkhiemhoang2-bit', $me | Set-Content pyproject.toml
(Get-Content CHANGELOG.md)   -replace 'manhkhiemhoang2-bit', $me | Set-Content CHANGELOG.md
git add -A; git commit -m "docs: 更新仓库地址"
```

注意 PowerShell 的 `Set-Content` 默认会写入 BOM，`tomllib` 读到 BOM 会报错。
真要用上面这段，跑完检查一下 `pyproject.toml` 还能不能解析。

## 二、在 GitHub 上建仓库

网页建最直观：打开 https://github.com/new，填 `specdiff`，选 Public。
**不要**勾 "Add a README file"、".gitignore"、"license"，本地已经有了，勾了会冲突。

装了 gh CLI 的话可以用命令：

```powershell
gh repo create specdiff --public --source=. --remote=origin --push
```

## 三、推代码

```powershell
cd C:\Users\29072\deepseek_harness_workspace\specdiff
git remote add origin https://github.com/manhkhiemhoang2-bit/-specdiff_.git
git branch -M main
git push -u origin main
```

首次推送会要求登录。Git for Windows 会弹浏览器窗口，点一下就行。
如果要求输入密码，去 https://github.com/settings/tokens 生成一个勾了 `repo` 权限的 token 当密码粘进去。

国内网络下 `github.com:443` 经常连不上（`api.github.com` 却是通的，容易误判成"网络没问题"）。
推不上去先看这个，开代理再试。

## 四、发布后要做的事

### 1. 话题标签

进仓库页面，点右上角 About 的齿轮，逐个添加：

```
verilog  systemverilog  rtl  verification  vcd  waveform  eda  fpga
chip-design  debug  testing  formal-verification  assertion  ai-agent  ic-design
```

GitHub 的搜索和推荐主要靠它，别省这一步。

### 2. 建 Release

```powershell
git tag -a v0.1.0 -m "首个版本：按规格检查仿真波形"
git push origin v0.1.0
```

然后进仓库 → Releases → Draft a new release → 选 `v0.1.0` → 标题填 `v0.1.0` →
说明把 `CHANGELOG.md` 里 0.1.0 那段复制进去 → Publish。

### 3. 看 CI

推上去后进 Actions 页，`tests` 工作流会在 12 个组合上跑测试。
红叉点进去看日志。跨平台容易踩的坑（Windows 控制台编码、pwsh 的续行符、
子进程环境变量）都已经在 workflow 和测试里处理过了。

### 4. README 配图

README 开头就是那段结论输出。想更直观可以补一张终端截图：

```powershell
specdiff check examples/traffic_bug/counter8.vcd examples/traffic_bug/counter.spec.yaml -v
```

截图存成 `docs/demo.png`，在 README 标题下面加一行 `![演示](docs/demo.png)`。

## 五、发布到 PyPI

让别人能 `pip install specdiff`。先去 https://pypi.org/account/register/ 注册并开两步验证。

```powershell
pip install build twine
python -m build                    # 产出 dist/*.whl 和 dist/*.tar.gz
twine check dist/*                 # 检查元数据
twine upload dist/*                # 上传，会要 PyPI token
```

PyPI 的包名全局唯一。`specdiff` 被占了就得改名，改完同步改 `pyproject.toml`
里的 `name` 和 `[project.scripts]`。

## 六、推广

发完不算完，几个去处：

- awesome 列表：搜 `awesome-verilog`、`awesome-eda`、`awesome-chip-design`，提 PR 加进去，这是最精准的曝光。
- Reddit：r/FPGA、r/chipdesign。发帖讲清解决什么痛点，带一段演示输出，别只丢链接。
- Hacker News：Show HN，标题写清做什么。
- 中文社区：知乎、V2EX、电子发烧友。国产 FPGA 工具链支持是个好切入点。
- 给相关项目提 issue 或 PR，比如在 duck-rtl、VCD_ANALYZER 的 README 里补一句"相关工具"。

对外介绍可以直接用这段：

> 大模型会写 RTL，但它看不见波形。仿真报错时它只知道"不匹配"，
> 不知道错在第几拍、哪根线、哪一位。specdiff 把百万级的波形数据
> 压缩成一句精确定位。

## 七、发布检查清单

- [ ] 仓库地址三处都改好了
- [ ] `pytest -q` 全部通过（当前 139 个测试）
- [ ] `python examples/run_demo.py` 能跑出结论
- [ ] `specdiff --version` 正常
- [ ] `git status` 干净
- [ ] `.gitignore` 生效（`work/`、`*.wlf` 没被提交）
- [ ] 仓库描述和话题标签填了
- [ ] 打了版本标签并建了 Release
- [ ] Actions 里 CI 是绿的

## 八、维护守则

README 里承诺的东西必须是真的。现在文档和代码对得上，以后加功能记得同步改文档。

开源项目烂掉，多数是因为文档和代码对不上，代码差反而是少数。
