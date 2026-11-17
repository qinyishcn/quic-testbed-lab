# 空口与回传双环实验复现

[中文报告](../../REPORT.cross-layer-loops.zh-CN.md) · [已发表数据](../../results/cross-layer-loops)

在 Linux 或 WSL2 中准备原项目的 Python 和网络工具，安装 BBRv3 构建依赖。设置 `PY` 指向装有仓库依赖与 `dpkt` 的 Python，`TESTBED` 指向检出的 6G-Testbed 源码。构建和复跑使用独立临时目录。网络实验需要 sudo 权限以创建命名空间和抓包。

```bash
WORK="$(mktemp -d)"
git clone https://github.com/private-octopus/picoquic.git "$WORK/picoquic"
git -C "$WORK/picoquic" checkout --detach 37bb5d28aaea417649a6d4facc15f54de7d55829
git -C "$TESTBED" rev-parse HEAD
# 6G-Testbed 的提交为 7231ae8e5670b67564cf28d397c2820d9403ecdc
"$PY" bbr3/build.py "$WORK/picoquic" "$WORK/bbr3"
"$PY" studies/cross_layer/build_endpoint.py "$WORK/picoquic" "$WORK/bbr3/cmake" "$WORK/endpoint"

sudo "$PY" studies/cross_layer/experiment.py --revision --testbed "$TESTBED" \
  --endpoint "$WORK/endpoint/endpoint" --out "$WORK/raw" --repeats 3
for mode in bbr radio bbr_radio; do
  "$PY" studies/pacing_loss/analyze.py "$WORK/raw/$mode" "$WORK/publish/$mode" --archive
done
"$PY" studies/cross_layer/analyze.py --revision --data "$WORK/publish"
"$PY" studies/cross_layer/plots.py --revision --data "$WORK/publish"
```

最新结果见 [cross-layer-bbr-radio](../../results/cross-layer-bbr-radio)。分析和制图也可直接读取已上传的三组数据。保留 `--revision`、省略 `--data` 时，两个脚本会读取这个结果目录。制图可用 `--font` 指定中文字体。此前使用回传直接遥测的四组对照可按[原提交的说明](https://github.com/qinyishcn/quic-testbed-lab/blob/0f2683472fa9660180b0af6e92710914cfade8bc/studies/cross_layer/README.md)复现。

- `summary.csv` 按网络条件、控制方案和阶段聚合。`runs.csv` 保留逐轮统计和端点实际收到的两类反馈计数。`tasks.csv` 保留每笔正式请求的开始时刻、完整上传耗时、回复耗时以及两者之和。分位数使用线性插值。
- `timeline.csv.gz` 每种网络条件和方案记录第一轮的 2 ms 采样，包含空口容量、回传容量、目标发包速率、拥塞窗口、在途数据、终端候发数据和两段中继队列。其中 `pending_kB` 只统计应用已提交且尚未首次发送的字节。两段队列包含正在占用链路服务的 IP 数据报。在联合方案中，`bbr_outer_mbps` 是空口限速生效之前的 BBRv3 目标，`radio_cap_mbps` 是延迟后的空口反馈上限，实际目标是二者的较小值。
- 每个控制方案的原始归档包含双端 QUIC 日志、双端网卡抓包、网络计划、端点采样和 Testbed 流量数据库。归档内 `wire.csv` 记录逐包等待和中继进出，`segments.csv` 记录每个上行数据报进入两段队列、获准开始服务和实际离开队列的时刻。`feedback.csv` 记录中继采样、发布时刻、容量、待发字节和反馈上限。`applied.csv` 记录发送端接收反馈后使用的速率，仅带遥测的方案会产生该文件。
- 每个控制方案另有逐包 `packets.csv.gz`、`loss-events.csv`、端点和依赖版本的 `environment.json` 以及验证结果 `validation.json`。逐包记录里的确认耗时从发送端发出数据报开始，计到发送端收到能确认该数据报的 ACK。判丢事件包括发送端因包号或时限作出的判定。中继完整转发与协议层判丢是两个独立事件。

在发布数据中，所有实测请求都收到回复。交叉检查逐包日志、双端抓包、计划和中继记录确认，各上行数据报按顺序经过两段服务，保持同一数据报内容。每组有 14 个在抓包可见但未纳入 QUIC 日志匹配的握手数据报。抓包统计显示内核无丢包。
