# GBR-CC 复现实验

使用论文作者的 MsQuic 发送和接收程序，并在本仓库的 6G-Testbed 拓扑中构造上行授权信号。作者公开的发送端由 `bbr` 和 `gbr` 两种模式共用。单独构建的 `dual` 模式在作者公开的授权比值控制上补建论文的外环切换结构。详见[复现报告](../../REPORT.gbr-cc-reproduction.zh-CN.md)。

准备 WSL2 或 Linux、CMake、Ninja、C/C++ 编译器、Perl、make、curl、git、sudo，以及原 Testbed 复现所需的网络工具和 Python 依赖。设置 `PY` 为装好依赖的 Python 3.12，`TESTBED` 为现有 6G-Testbed 源码目录。从本仓库根目录运行。

```bash
WORK="$HOME/gbr-cc-reproduction-run"
bash studies/gbr_cc/build.sh "$WORK"

sender="$WORK/build-sender/bin/Release/secnetperf"
receiver="$WORK/build-receiver/bin/Release/secnetperf"
dual="$WORK/build-dual/bin/Release/secnetperf"

sudo "$PY" studies/gbr_cc/reproduce.py --testbed "$TESTBED" --sender "$sender" \
  --receiver "$receiver" --out "$WORK/signal" --profiles signal_probe --repeats 1 --bytes 14000000
sudo "$PY" studies/gbr_cc/reproduce.py --testbed "$TESTBED" --sender "$sender" \
  --receiver "$receiver" --out "$WORK/formal"
sudo "$PY" studies/gbr_cc/reproduce.py --testbed "$TESTBED" --sender "$sender" \
  --receiver "$receiver" --dual "$dual" --out "$WORK/outer" \
  --profiles backhaul_step radio_step --modes bbr gbr dual

"$PY" studies/gbr_cc/analyze.py --formal "$WORK/formal" --outer "$WORK/outer" \
  --signal "$WORK/signal" --output results/gbr-cc-reproduction
"$PY" studies/gbr_cc/plots.py results/gbr-cc-reproduction
```

制图可通过 `--font` 指定支持简体中文的字体。每次运行的工作目录单独创建。发送端在隔离的挂载命名空间内使用作者的 `/tmp/msquic_cellular_ratio.sock`，同一台主机的其他进程使用各自原有的 `/tmp`。

- `wire.csv` 每行记录一个无线或回传中继事件。`us` 是本机单调时钟，`due_us` 是预约出队时刻，`queue_bytes` 为事件后仍在该队列中的 IP 字节。
- `grants.csv` 记录每个采样窗口起止时间、无线队列上报字节、窗口配置授权字节和通过 Unix 数据报实际发送的比值。比值为空表示信号回放中人为停止注入。
- `native.log` 由作者 MsQuic 输出。其中 CSV 形式的行按包记录，分析脚本按相同的确认处理时刻合并，避免一次确认多个包时重复计时。
- `outer.csv` 记录补建外环的进入、降速与退出。该时刻直接使用本机单调时钟。
- `rounds.csv` 每轮一行。`rtt_p50_ms` 和 `rtt_p95_ms` 对每轮发送端的平滑往返时延取分位数。以 `phase_` 开头的指标统计实验计时第 2.2–5.8 s。`summary.csv` 对同条件的每轮指标再取中位数。
