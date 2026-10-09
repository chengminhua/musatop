# musatop

面向摩尔线程 GPU 的交互式终端监控工具。当前版本 **v0.2.1**，提供多卡状态、GPU 进程、主机 CPU/内存、筛选排序、进程详情和确认后的 SIGTERM 操作，也支持一次性文本及 JSON 输出。交互界面提供 UTIL／显存进度条，以及最近 **5 分钟**的主机与 GPU 点阵趋势。

软件版本以 `musatop --version` 为准；JSON 的 `schema_version=1` 是数据结构版本，与软件版本独立。驱动、GMI、MUSA Toolkit 版本由当前机器检测，也不是 musatop 的版本。

![v0.2.1 八卡监控界面（已脱敏）](https://cdn.jsdelivr.net/gh/chengminhua/musatop@main/docs/images/tui-v0.2.1-redacted.webp)

图片未加载时，可[直接查看截图](https://cdn.jsdelivr.net/gh/chengminhua/musatop@main/docs/images/tui-v0.2.1-redacted.webp)。

上图为 **8 张 X10000** 的实机运行截图，主机名、时间戳、进程标识及命令路径已脱敏。80×24 下自动改用较矮趋势图并对 GPU 分页。

## 当前支持与验证范围

- 运行环境：Linux 宿主机、Python 3.10 及以上、已安装驱动且能执行的 `mthreads-gmi`。运行时 Python 依赖只有 `psutil`；不需要 PyTorch，不需要编译 MUSA 程序。
- **X10000 真机验证通过**：验证环境为 8 张卡、每卡 80 GiB、GMI 2.3.3。各版本的测试数量、负载观测和验证范围分别记录在 [验证记录](https://github.com/chengminhua/musatop/blob/v0.2.1/docs/VALIDATION.md)，历史观测值不作为当前版本的性能指标。
- 工具展示宿主机可见的 PID；容器中的任务若能被宿主机的 GMI 和 `/proc` 看到，也会出现在宿主机进程表中。**在容器内部运行、MPC/vGPU、其他 GPU 型号及其他 GMI 版本尚未验证。**
- GPU 编号直接使用 GMI 返回的编号，不按 `MUSA_VISIBLE_DEVICES` 重排或筛选。选择显示设备请使用 `--gpu`。

进程发现与 SIGTERM 验证仅操作自建测试负载；具体执行版本、终端交互及恢复结果见 [验证记录](https://github.com/chengminhua/musatop/blob/v0.2.1/docs/VALIDATION.md)。GMI 接口参考 [摩尔线程官方 GMI 用户手册](https://docs.mthreads.com/gmc/gmc-doc-online/gmi/user_manual/)。

## 安装

使用 Python 3.10 及以上，从 [PyPI](https://pypi.org/project/musatop/) 安装即可，无需手动下载源码。建议安装到独立虚拟环境；先确认已安装的 `mthreads-gmi` 能在当前用户的终端中正常运行：

```bash
mthreads-gmi
python3 -m venv ~/.venvs/musatop
. ~/.venvs/musatop/bin/activate
python -m pip install musatop
musatop --version
musatop
```

若系统缺少 `ensurepip`，而系统 pip 支持 `--python` 参数，可改用下面的虚拟环境安装方式：

```bash
python3 -m venv --without-pip ~/.venvs/musatop
python3 -m pip --python ~/.venvs/musatop/bin/python install musatop
~/.venvs/musatop/bin/musatop --version
```

这些命令将 Python 依赖装入虚拟环境，不需要 `sudo`。驱动和 `mthreads-gmi` 由系统提供，pip 不会安装它们。未激活环境时，可以直接运行 `~/.venvs/musatop/bin/musatop` 或 `~/.venvs/musatop/bin/python -m musatop`。

### 升级

在上面创建的虚拟环境中升级：

```bash
. ~/.venvs/musatop/bin/activate
python -m pip install --upgrade musatop
musatop --version
```

若使用无 pip 的虚拟环境，升级命令为 `python3 -m pip --python ~/.venvs/musatop/bin/python install --upgrade musatop`。已有源码安装请在原虚拟环境内升级；可通过 `python -m pip show musatop` 检查当前安装位置。升级后退出旧的 musatop 实例，再重新启动。

若使用的软件源尚未同步新版本，可在安装或升级命令中添加 `--index-url https://pypi.org/simple/`，直接使用 PyPI。

### 从源码开发

需要修改代码时，克隆仓库并使用可编辑安装：

```bash
git clone https://github.com/chengminhua/musatop.git
cd musatop
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
musatop --version
```

源码环境缺少 `ensurepip` 时，同样可用 `python3 -m venv --without-pip .venv`，再执行 `python3 -m pip --python .venv/bin/python install -e .`。仅希望从源码安装而不随代码修改更新时，将 `install -e .` 改为 `install .`。

## 使用

```bash
musatop                              # 在交互式终端中启动 TUI
musatop --once                       # 输出一次文本快照
musatop --json > snapshot.json       # 输出一次 JSON 快照
musatop --gpu 0,1 --interval 2        # 显示指定 GPU，每 2 秒采样
musatop --pid 12345,12346             # 按宿主机 PID 筛选
musatop --user "$USER" --sort cpu     # 按用户筛选，CPU 使用率降序
musatop --sort pid --reverse         # PID 倒序
musatop --ascii --no-color           # 使用 ASCII 条形和趋势，关闭颜色
```

标准输入或标准输出不是 TTY 时，默认输出一次文本快照，不进入交互界面。`--json` 始终只输出一次；与 `--once` 同时使用时输出 JSON。

| 参数 | 行为 |
| --- | --- |
| `--once` | 输出一次文本快照 |
| `--json` | 输出一次 JSON，当前 `schema_version` 为 `1` |
| `--interval SECONDS` | 后台采样间隔，默认 `1` 秒，范围 `0.25–3600` 秒；采集耗时较长时实际间隔会增大 |
| `--gpu 0,1` | 按 GMI GPU 编号筛选设备与进程 |
| `--pid 123,456` | 按 PID 筛选进程，不隐藏设备概要 |
| `--user USERNAME` | 按宿主机用户名精确筛选进程 |
| `--sort FIELD` | `gpu_memory`、`cpu`、`rss`、`pid`、`user`、`gpu`；默认 `gpu_memory` |
| `--reverse` | 反转所选字段的默认排序方向 |
| `--ascii` / `--no-color` | TUI 使用 ASCII 图形 / 关闭颜色；不改变文本或 JSON 输出 |
| `--help` / `--version` | 显示帮助 / 版本，不访问 GPU |

显存、CPU 和 RSS 默认降序；PID、用户名和 GPU 编号默认升序。未知值始终放在已知值后面。多个筛选条件同时生效。

### 交互按键

主界面按设备、趋势、进程排列。80×24 终端显示四张卡、九行趋势和至少三行进程，使用 PageUp/PageDown 查看其余 GPU。大窗口增加图高和列表行数；80×34 可同时显示八卡、十三行趋势和五行进程。60–79 列时隐藏型号、使用紧凑进程列，趋势只显示 GPU 两图；主机当前指标仍在顶部。高度不足 18 行时暂时收起趋势，恢复尺寸后按原开关状态显示；小于 60×12 时显示调整尺寸提示。

| 按键 | 行为 |
| --- | --- |
| `↑` / `↓`、`Shift-Tab` / `Tab` | 选择上一 / 下一进程 |
| `Home` / `End` | 选择首个 / 最后一个进程 |
| `PageUp` / `PageDown` | 翻动设备列表；在详情、帮助和确认页中滚动内容 |
| `s` / `S` | 轮换排序字段 / 反转排序方向 |
| `/` | 编辑进程搜索词，匹配命令、PID、用户名及 GPU 编号；Enter 应用，Esc 取消；清空后 Enter 取消搜索过滤 |
| `u` | 切换只看当前用户的进程 |
| `c` | 切换紧凑进程列表 |
| `g` / `G` | 在汇总和各张可见 GPU 之间前后切换，与进程选择独立 |
| `h` | 显示 / 隐藏趋势面板，隐藏后仍收集历史 |
| `Enter` | 查看所选进程详情和完整命令；长内容可上下滚动 |
| `k` | 打开所选进程的 SIGTERM 确认页 |
| `r` / `R` / `F5` | 请求重新采样 |
| `?` | 帮助 |
| `q` / `Q` | 主界面退出；详情和帮助页返回；确认页取消 |
| `Ctrl-C` | 从任意界面退出工具，不向选中进程发送信号 |

搜索输入期间普通字符作为搜索文本。启动时的 `--gpu`、`--pid` 和 `--user` 条件仍然生效，交互搜索与 `u` 是附加筛选。

### 进度条与五分钟趋势

UTIL 进度条表示 GPU 利用率；VRAM 进度条表示显存已用容量占总容量的比例，同时保留已用／总量。它不是 GMI 的显存带宽利用率。颜色表达负载程度，高利用率本身不代表故障；数字始终保留，未知值显示 `N/A`，过期值变暗并带有 `STALE` 提示。

进度条使用实心块和部分填充字符，支持彩色渐变。非 UTF-8 环境自动使用 ASCII；字体显示不正常时可显式添加 `--ascii`，无色终端仍可读。

趋势参考 nputop 的多行点阵面积图，左侧 CPU 向上、主机内存向下，右侧 GPU 显存向上、UTIL 向下。上下图共用中间时间轴，中央为 0%，外缘为 100%；图高表达占用率，固定颜色只区分指标。无色模式仍可凭高度、标签和数字阅读。`Now` 是当前值，图中每个时间区段是观测峰值，二者可能不同。

默认 `ALL` 汇总 `--gpu` 范围内的可见设备，包含当前设备分页以外的卡。UTIL 是同一轮采样各卡利用率的算术平均；显存是所有参与卡的已用容量之和除以总容量之和。某张卡缺少某项指标时，该项汇总为 `N/A`，不会只平均有数据的卡。用 `g/G` 切换单卡；设备分页、进程选择或进程筛选不会改变汇总范围。主机内存比例使用顶部 RAM 同一组已用／总量数据。

四图始终保留最近 300 秒，左旧右新。每秒记录峰值，再按固定单调时间边界压缩到屏幕宽度；已结束的内部区段只随时间平移，不会每秒重新分组。最新区段可以加入新观测，最左端严格淘汰超时数据；缩放时按新宽度重新投影。有限的字符列仍会带来离散移动，点阵提高精度，不通过补造采样制造平滑动画。

启动不足五分钟、没有采样或指标未知的区段留空，不插值、不补零、不重复缓存值；已知零值有基线点。较大的 `--interval` 会使趋势更稀疏。一个显示区段里存在部分有效采样时只取这些观测的峰值，压缩后的亚像素缺口无法逐一显示。`STALE` 表示 GPU 采集过期，主机数据独立更新。

历史复用原有后台采样，不增加 GMI 查询。单卡按 UUID／PCI 身份关联；参与汇总的设备身份集合变化时重置汇总历史。历史只在当前进程内存中保存，退出清空，不写入 JSON 或磁盘；没有可靠设备标识时不拼接 GPU 历史。

### 进程终止

`k` 确认页显示捕获的 PID、用户、完整命令和进程信息；**只有按 `y` 才会发送 SIGTERM**，Enter、`n`、Esc 或 `q` 都取消。没有 SIGKILL 快捷键，也不会自动提权。

确认目标固定为打开确认页时的进程。进程数据过期、身份未验证、权限不足、PID 创建时间或设备关联变化时，操作会被拒绝或取消。发送前再次检查 PID 与创建时间，拒绝 PID 1 及 musatop 自身；支持时使用 Linux pidfd 绑定进程实例，否则使用 psutil 的进程重用检查。当前用户没有权限时显示错误，不调用 `sudo`。

## 指标含义与 JSON

设备状态来自 `mthreads-gmi -q --json`，GPU 进程归属及显存来自 `mthreads-gmi` 的进程表；用户、完整命令、CPU、RSS、创建时间及主机信息由 `psutil` 补全。GMI 版本单独查询。MUSA Toolkit 版本读取安装目录的 `version.json`：依次检查 `MUSA_HOME` / `MUSA_PATH`、PATH 中 `mcc` 的真实位置、`/usr/local/musa`、唯一的 `/usr/local/musa-*` 安装。明确选择的目录优先；环境变量冲突、多版本未选择或元数据不可用时为未知，并记录原因。探测不执行编译器，也不从驱动或目录名推测版本。

- **未知不等于零**：终端显示 `N/A`，JSON 使用 `null`。进程 CPU 的首次采样和主机 CPU 的首帧为未知，需要下一帧才能计算；一次性文本和 JSON 输出中的这些 CPU 值也通常为 `null`。
- 进程 CPU 以单个逻辑 CPU 为 100%，多线程进程可以超过 100%。GPU 利用率是设备指标，不推断每个进程的 GPU 利用率。
- 同一 PID 使用多张卡时，每张卡各占一行，GPU 显存按卡展示；这些行复用同一份宿主机 CPU 和 RSS，**不能将重复的 CPU/RSS 相加**。
- 显存已用/总量与 GMI 的内存利用率是不同指标。`Power(W)` 的斜杠前是实时功耗，后是当前功率上限；实时功耗可读不代表上限可读。上限仅接受 GMI 的有效正数，未知或零值保留为 `N/A` 并附原因，不使用默认上限或其他 GPU 的数值替代。
- 设备和进程来自两次查询，不保证同一瞬间。每个数据源分别记录成功时间；失败时保留上次成功结果并标记过期，首轮失败则没有可用数据。

JSON 顶层包含以下字段；完整字段定义见 [`musatop/models.py`](https://github.com/chengminhua/musatop/blob/v0.2.1/musatop/models.py)：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 当前为 `1` |
| `sampled_at` | 本轮采样时间，带时区的 ISO 8601 UTC 时间字符串 |
| `devices_sampled_at` / `processes_sampled_at` | 对应来源最近一次成功采样时间；从未成功为 `null` |
| `devices_stale` / `processes_stale` | 对应来源本轮查询或解析是否失败；为 `true` 时不要把缓存值当作实时数据 |
| `driver_version` / `gmi_version` / `musa_version` | 可获取的版本信息，未知为 `null` |
| `musa_version_source` / `musa_version_reason` | Toolkit 元数据文件路径 / 无法确定版本的原因；无对应值为 `null` |
| `host` | 主机名、主机 CPU 百分比、内存已用与总量 |
| `devices` | GPU 编号、UUID、名称、PCI 地址、利用率、显存、温度、功耗与时钟 |
| `processes` | 每个 GPU/PID 的显存及宿主机进程信息；`status` 可为 `ok`、`access_denied`、`exited`、`unverified` |
| `errors` | 本轮采集错误说明；设备与进程来源分别标识 |

设备的 `power_limit_reason` 说明上限缺失或无效的原因；上限可用时为 `null`。单个可选指标不可用不会将整轮采集标为失败。字段变更的历史背景见 [N/A 定位记录](https://github.com/chengminhua/musatop/blob/v0.2.1/docs/NA_DIAGNOSIS.md)。

字段名称直接包含单位：`*_bytes` 为字节，`*_w` 为瓦特，`*_c` 为摄氏度，`*_mhz` 为 MHz，`*_percent` 为百分比，`running_seconds` 为秒；进程 `create_time` 为 Unix 时间戳秒数。JSON 中保留数值单位，终端再转换为 MiB/GiB。应用筛选后，时间、错误及过期标记仍描述本轮实际采集状态。

一次性输出的退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 采集成功；有效的零 GPU / 零进程结果也成功 |
| `1` | 任一采集错误或数据过期，或运行环境错误；发生采集错误时 JSON 仍包含 `errors` 和过期标记 |
| `2` | 命令行参数错误 |
| `130` | 一次性采集被 Ctrl-C 中断；TUI 的正常退出返回 `0` |

## 故障排查

| 现象 | 检查方式 |
| --- | --- |
| 找不到 `mthreads-gmi` | 在同一用户和同一环境运行 `command -v mthreads-gmi`；按厂商说明安装驱动/GMI，或将已安装程序所在目录加入 PATH |
| JSON 或进程表解析失败 | 分别运行 `mthreads-gmi -q --json` 与 `mthreads-gmi`，检查驱动报错或输出格式变化；当前验证版本为 GMI 2.3.3 |
| 功率显示 `410W/N/A` 等 | 前者是实时功耗，后者是当前上限。检查 GMI 原始 `Current Power Limit` 和 JSON `power_limit_reason`；若 GMI 本身返回 `N/A`，需排查驱动能力。已发现的驱动查询差异见 [N/A 定位记录](https://github.com/chengminhua/musatop/blob/v0.2.1/docs/NA_DIAGNOSIS.md) |
| Toolkit 显示 `N/A` | 查看 JSON `musa_version_reason` / `musa_version_source`。多版本安装时通过 `MUSA_HOME` 选择实际使用的安装目录；重启工具后重新检测 |
| 出现 `STALE` 或数据不可用 | 检查底部错误或 JSON `errors`；GMI 单次查询默认 3 秒超时；`r` 可请求刷新，成功后恢复更新 |
| 有 PID，但用户名/命令缺失 | 进程可能已经退出，或当前用户无权读取对应 `/proc` 信息；查看进程 `status` |
| TUI 无法初始化 | 确认在真实终端运行、`TERM` 设置有效且 Python 支持 curses；自动化任务使用 `--once` 或 `--json` |
| 列表为空 | 区分“数据不可用”“没有 GPU/进程”和“筛选后无匹配”；检查 `--gpu`、`--pid`、`--user`、`u` 及搜索条件 |
| 看不到整条命令 | 选中进程后按 Enter，在详情页滚动查看 |

提交问题时附上 musatop/GMI/驱动版本、错误信息和去除敏感信息后的输出片段；命令行、用户名、主机名及 GPU UUID 可能包含环境信息。

## 开发与测试

| 模块 | 职责 |
| --- | --- |
| `musatop/backend.py`、`models.py` | GMI 查询、解析、独立过期状态及有单位的快照类型 |
| `musatop/processes.py`、`monitor.py` | 宿主机进程补全、身份校验、SIGTERM、后台采集 |
| `musatop/history.py`、`bars.py` | 按设备身份保存五分钟秒桶、占用率、进度条及趋势绘图 |
| `musatop/view.py`、`cli.py`、`tui.py` | 公共筛选/格式化、命令行、curses 交互界面 |
| `tests/` | 合成 GMI fixtures、单元测试及不依赖 GPU 的真实 PTY 测试 |
| `tools/` | 显式启动的真机验收脚本和可选 MUSA 验证负载 |

在已激活的开发虚拟环境中：

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python -m pip install build
python -m build
```

单元测试不会启动 GPU 负载。真机验证需要单独检查空闲状态，并显式运行工具；构建和使用方式见 [GPU 验证小程序](https://github.com/chengminhua/musatop/blob/v0.2.1/tools/README.md)，实际结果见 [验证记录](https://github.com/chengminhua/musatop/blob/v0.2.1/docs/VALIDATION.md)。不要把一次已验证的硬件组合视作所有摩尔 GPU 的兼容性保证。

维护者的构建、版本标签与 PyPI 发布步骤见 [发布维护说明](https://github.com/chengminhua/musatop/blob/v0.2.1/docs/PUBLISHING.md)。

## 许可证与致谢

本项目采用 **GNU General Public License v3.0 only（GPL-3.0-only）**，完整条款见 [LICENSE](https://github.com/chengminhua/musatop/blob/v0.2.1/LICENSE)。感谢 [nputop](https://github.com/youyve/nputop) 和 [nvitop](https://github.com/XuehaiPan/nvitop) 提供的产品与交互参考。
