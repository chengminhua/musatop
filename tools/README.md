# GPU 验证小程序

`gpu_smoke.mu` 用于验证 musatop 的进程发现、多卡归属、显存用量和终止操作；它不是性能测试。程序直接使用 MUSA Runtime，不需要 PyTorch。musatop 本身的安装和运行不需要此程序或 MUSA SDK。

## 构建与硬件查询

以下命令在独立验证目录内执行，调整 SDK 路径使其指向实际安装位置：

```bash
MUSA_SDK=/usr/local/musa-4.3.2
mkdir -p build
"$MUSA_SDK/bin/mcc" -std=c++14 -O2 -pthread --offload-arch=mp_22 \
  tools/gpu_smoke.mu -L"$MUSA_SDK/lib" -Wl,-rpath,"$MUSA_SDK/lib" \
  -lmusart -o build/gpu_smoke
timeout --signal=TERM --kill-after=5s 15s build/gpu_smoke --info
```

`--info` 只查询属性，不分配测试缓冲区、不启动计算内核。输出每卡的型号、计算能力 `major`/`minor`、实际 `warp_size` 和总显存字节数。首次查询使用的 `mp_22` 只是探测二进制的编译目标，不代表验证机的真实架构；若编译器不支持它，先检查该版本编译器支持的目标。查询不会执行该目标的计算内核。

**启动负载前按实际计算能力重新编译**：例如 `major=3, minor=1` 使用 `--offload-arch=mp_31`；`major=2, minor=2` 使用 `--offload-arch=mp_22`。不要由型号名称推测架构，不要假设 warp 为 32。

## 有限时长验证

先检查 `mthreads-gmi` 的进程表与设备利用率；存在其他计算任务时不新增负载。空闲时从一张卡开始：

```bash
timeout --signal=TERM --kill-after=5s 30s \
  build/gpu_smoke --devices 0 --seconds 20 --mib 64
```

确认每卡额外显存（包含运行时上下文）低于约 1 GiB，再考虑八卡同进程场景，并在启动前重新检查各卡空闲状态：

```bash
timeout --signal=TERM --kill-after=5s 30s \
  build/gpu_smoke --devices 0,1,2,3,4,5,6,7 --seconds 20 --mib 64
```

每卡一个线程、一个缓冲区；每轮最多约 20 ms 连续执行小内核，随后休眠 80 ms，使利用率变化可观察。默认 20 秒和每卡 64 MiB；程序限制时长为 1–60 秒，缓冲区为 1–256 MiB。由于上下文开销与驱动有关，缓冲区上限不是总显存使用量保证，必须用 GMI 实测。设备编号使用运行时可见编号；验收时不设置设备可见性重映射，并与 GMI 编号核对。

程序输出 JSON Lines，成功准备全部设备后才输出 `event="started"`，包括 PID、设备列表及每卡缓冲区字节数；线程失败会要求全部线程停止。退出时逐卡释放缓冲区，输出 `event="completed"`。错误输出到标准错误，退出码非零。`SIGTERM`/`SIGINT` 处理器只设置信号标志，工作线程随后释放内存；对应退出码为 143/130。设备调用若卡住仍可能阻止正常退出，所以必须保留外层 `timeout --kill-after` 兜底。

终止测试只针对该程序的 `started.pid`，同时核验进程创建时间和命令，不操作其他进程。每轮结束后检查该 PID 从 GMI 消失、显存回落，且没有遗留测试进程。文档中的命令是操作说明，不构成真机验证已经通过的声明。

## 自动验收脚本

安装 musatop 后可从仓库目录执行以下命令；显式指定工作负载才会启动 GPU 计算，普通单元测试不会运行它们：

```bash
python tools/validate_host.py workload build/gpu_smoke --devices 0
python tools/validate_host.py workload build/gpu_smoke --devices 0,1,2,3,4,5,6,7 --terminate
python tools/validate_host.py soak --seconds 600
```

负载验收会首先检查所有 GPU 空闲、无进程；条件不满足则退出。它验证 GMI 对照、同 PID 多卡归属、每卡显存上限和进程清理。`--terminate` 仅向脚本刚刚启动且确认身份的测试进程发送 SIGTERM。不要用 Python `-O` 或 `PYTHONOPTIMIZE` 执行验收脚本。

持续采样验收专门针对本次八卡、每卡 80 GiB 的验证环境；其他硬件请调整验收条件，它不是监控工具的硬件限制。默认每秒采样，输出采样数、错误、间隔、RSS 和 Python 监控进程 CPU。真实执行结果见 [验证记录](../docs/VALIDATION.md)。

## 五分钟历史验收

在八卡、每卡 80 GiB 的验证机上，使用已安装当前版本的 Python 环境运行：

```bash
python tools/validate_history.py --seconds 600
```

该脚本只读取同一个 `Monitor` 后台采样器的快照与历史，不启动 GPU 负载，也不额外执行 GMI 查询。默认连续观察 600 秒，每 60 秒输出简短 JSON 进展，结束时输出 JSON 验收报告；失败退出码为 1。`--seconds` 可以延长观察时间，最短为 600 秒。

验收检查八卡容量、采集错误与过期状态、至少 90% 的预期采样数、采样间隔小于五秒、每卡及主机／GPU 汇总历史不超过 300 个秒桶、初始桶按时淘汰，以及窗口填满后每项指标至少 90% 的有效记录覆盖。报告保留 `gpus`，新增按 `host`／`aggregate` 命名的 `summaries`，包含四项趋势的数值范围。后半程 RSS 相对后半程首样本的最大增长须小于 16 MiB。检查不要求垃圾回收后 RSS 立即回落，也不要求空闲 GPU 出现负载变化。

所有报告仅包含数值统计和固定错误类别，不输出主机名、设备 UUID、账号、业务 PID、命令行或原始采集错误。采集失败与中断均停止后台采样并报告失败；真实十分钟结果应保存到验证记录，模拟时钟测试不代替真机验收。

## 文档界面图

`python tools/render_preview.py /tmp/musatop-preview.svg` 调用实际 TUI，以固定的合成主机、设备、进程和五分钟历史生成 SVG；不访问 GPU，不包含机器采样。可添加 `--width 80 --height 24` 检查紧凑布局。脚本只使用项目现有依赖和标准库；字体应支持 Braille，SVG 提供 DejaVu Sans 后备字体。生成的预览用于本地检查，不纳入 Git；README 使用脱敏后的实机截图。
