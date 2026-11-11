# QUIC BBRv3 Testbed 复现

本目录提供 BBRv3 的独立构建、实验入口和图表。它读取原第三版的任务生成器、网络中继和统计代码，以便对照相同的容量时序与负载。BBRv1 的端点源文件、脚本和实验输出见[原运行说明](../v3/README.md)和[原判丢补测说明](../studies/pacing_loss/README.md)。

## 构建

准备 Linux 或 WSL2、Python 3.12、仓库依赖、`dpkt`、C 编译器、CMake、OpenSSL 开发库、`iproute2`、`ethtool`、`tcpdump` 和配置独立网络命名空间所需的 sudo 权限。用以下命令创建独立工作目录。设置 `PY` 为装好依赖的 Python，`TESTBED` 为 6G-Testbed 的本地检出目录。

```bash
BBR3_WORK="$(mktemp -d)"
git clone https://github.com/private-octopus/picoquic.git "$BBR3_WORK/picoquic"
git -C "$BBR3_WORK/picoquic" checkout --detach 37bb5d28aaea417649a6d4facc15f54de7d55829
"$PY" bbr3/build.py "$BBR3_WORK/picoquic" "$BBR3_WORK/build"
```

`endpoint` 用于 A1–D1 正式统计，`endpoint-trace` 单独采集 BBRv3 内部更新，`endpoint-loss` 开启完整 QUIC 日志和逐包判丢采集。在线推理另用 `endpoint-online` 及其 BBRv1 参照 `endpoint-online-reference`。所有 CMake 产物和生成的 C 文件均放在构建输出目录。

## A1–D1、逐次调速和判丢

`FIXTURE_DIR` 指向按[原第三版流程](../v3/README.md)生成的输入目录，含 `fixtures.json`、24 个请求文件和独立模型评分 `model.json`。新实验入口会把 24 个请求的大小及 SHA-256 与原第三版公开清单逐一核对。

```bash
sudo "$PY" bbr3/experiment.py bursts --testbed "$TESTBED" --endpoint "$BBR3_WORK/build/endpoint" \
  --fixtures "$FIXTURE_DIR/fixtures.json" --out "$BBR3_WORK/bursts"
sudo "$PY" bbr3/experiment.py trace --testbed "$TESTBED" --endpoint "$BBR3_WORK/build/endpoint-trace" \
  --out "$BBR3_WORK/trace"
sudo "$PY" bbr3/experiment.py loss --testbed "$TESTBED" --endpoint "$BBR3_WORK/build/endpoint-loss" \
  --out "$BBR3_WORK/loss"
"$PY" v3/report_data.py "$BBR3_WORK/bursts" "$FIXTURE_DIR" "$BBR3_WORK/burst-results"
"$PY" bbr3/archive_bursts.py "$BBR3_WORK/bursts" "$BBR3_WORK/burst-results/raw-evidence.tar.xz"
"$PY" studies/pacing_loss/analyze.py "$BBR3_WORK/loss" "$BBR3_WORK/loss-results" --archive
```

`bursts` 默认覆盖 21 个条件各三轮。每个条件第一轮采集双端 QUIC 日志，所有轮次采集端点状态和中继数据。`trace` 默认单独执行一次 A1 长驻留条件，便于判断每次控制量变化是发生在确认、丢包通知还是其他事件上。`loss` 默认执行 9 个条件各三轮，每轮六笔正式任务，每轮都有完整双端 QUIC 日志和双端网卡抓包。归档包含所有已采集轮次的记录，任务计划中的输入路径缩减为文件名。

内部事件表中，`state` 为 BBRv3 主状态编号。编号 0 为启动，1 为排空，2、3、4、5 分别为探测带宽的降速、巡航、补充在途数据、提速阶段，6 为最小往返时延探测，7、8 是 picoquic 的长往返时延启动和恢复启动。常规 `samples.csv` 的状态列由同一主状态加高位标志构成，读取主状态时取最低 8 位。带宽上限、在途数据上限字段取 `18446744073709551615` 时表示算法尚未施加该上限。表头的 `bps` 是 bit/s，`Bps` 是 byte/s。[BBRv3 状态和观测函数](https://github.com/private-octopus/picoquic/blob/37bb5d28aaea417649a6d4facc15f54de7d55829/picoquic/bbr.c)

## 在线 MiniCPM-o 配对补测

按[第二版素材说明](../v2/README.md)准备 `ASSETS`、`LLAMA` 和 `LLAMA_LIB_PATH`。发送端上传第二版同样的四图请求，接收端用本地模型生成文本并沿同一条可靠 QUIC 流返回。两种算法使用同一模型进程、端点实现和发送计划，按网络配置交替执行顺序。每个配置重新建立连接，任务间隔 3 s。

```bash
sudo "$PY" bbr3/online.py --testbed "$TESTBED" --assets "$ASSETS" --llama "$LLAMA" \
  --lib-path "$LLAMA_LIB_PATH" --build "$BBR3_WORK/build" --out "$BBR3_WORK/online"
```

推理进程应独立于传输基准运行，避免资源竞争影响毫秒级观测。在线端点用本地文件把 QUIC 服务端与模型调用进程相连，两端约每 1 ms 检查一次。这个时间包括在在线端到端测量里，可以通过逐请求记录查看模型调用等待、模型生成首段文本、首段文本返回三个分量。公开在线表只包含题号、请求字节数、哈希、耗时和选择题结果。

## 生成报告数据

```bash
"$PY" bbr3/compare.py --bursts "$BBR3_WORK/burst-results" --loss "$BBR3_WORK/loss-results" \
  --trace "$BBR3_WORK/trace" --online "$BBR3_WORK/online" --out results/bbr3
```

制图需要简体中文字体，可以用 `--font` 指定。正式实验摘要可分别放到 `results/bbr3/bursts` 和 `results/bbr3/loss`。对照图读取已经公开的 BBRv1 数据。复现在线对照时，在线脚本会现场运行两种算法。
