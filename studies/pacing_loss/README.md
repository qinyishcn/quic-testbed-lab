# 发包节奏与判丢专题复现

[发包节奏报告](../../REPORT.pacing.zh-CN.md)使用第三版实验的已发布数据。第三版的复现方法见[实验说明](../../v3/README.md)。[逐包时延和判丢报告](../../REPORT.loss-detection.zh-CN.md)使用本目录的新补测。两个报告使用相同提交的 picoquic BBRv1。

补测依赖 Linux 或 WSL2、Python 3.12、根目录 Python 依赖、`sudo`、`iproute2`、`ethtool`、`tcpdump`、OpenSSL、CMake 和 C 编译器。按[仓库复现说明](../../README.md#复现)准备环境变量 `PY` 和 `TESTBED`，并下载固定版本的 picoquic。以下命令从仓库根目录执行。`RAW` 指向一个尚未存在的目录。这个实验发送固定字节负载，无需模型、图片和 GPU。

```bash
PICOQUIC="$PWD/.venv/picoquic"
BUILD="$PWD/.venv/pacing-loss-build"
RAW="$PWD/.venv/pacing-loss-raw"
RESULTS="$PWD/.venv/pacing-loss-results"
mkdir -p "$BUILD"
git clone https://github.com/private-octopus/picoquic.git "$PICOQUIC"
git -C "$PICOQUIC" checkout --detach 37bb5d28aaea417649a6d4facc15f54de7d55829
bash v3/build.sh "$PICOQUIC" "$BUILD/endpoint-v3"
"$PY" studies/pacing_loss/build.py "$PICOQUIC" "$BUILD"
sudo "$PY" studies/pacing_loss/experiment.py --testbed "$TESTBED" \
  --endpoint "$BUILD/endpoint" --out "$RAW" --repeats 3
"$PY" studies/pacing_loss/analyze.py "$RAW" "$RESULTS" --archive
"$PY" studies/pacing_loss/plots.py "$RESULTS"
```

`build.py` 从固定提交生成观测版端点和判丢源文件，保留原有拥塞控制、判丢和补发逻辑。它开启完整 qlog，记录发包时的计时器，以及判丢分支运行时的真实期限。`experiment.py` 使用 6G-Testbed 的网络模拟器和流量日志，`relay.py` 对 QUIC 加密数据报实施已记录的延迟和丢弃。`analyze.py` 校验正式轮次分母，按包号关联双端日志，用加密载荷前缀和 UDP 长度关联网卡抓包，用流编号和字节区间关联补发。两张中文图使用 matplotlib，需要支持中文的字体。可通过 `--font` 指定字体文件。

也可以直接解开[公开原始数据包](../../results/pacing-loss/raw-evidence.tar.xz)，把解出的目录传给 `analyze.py`，再运行 `plots.py`。数据包附逐文件 SHA-256 清单，包括 27 轮的端点 qlog、截取前 96 B 的双端网卡抓包、探测计时、原始判丢、中继干预、任务事件和 Testbed SQLite。归档按文件白名单收集，实验生成的 TLS 证书和私钥留在本地。

| 文件 | 内容与字段 |
|---|---|
| [逐包时延 `packets.csv.gz`](../../results/pacing-loss/packets.csv.gz) | 每行一个已发送的 QUIC 包。`direction`、`packet_type`、`packet_number` 是方向、包号空间和包号。`send_ms`、`receive_ms`、`ack_send_ms`、`ack_receive_ms` 以同一轮的计时起点为零点。`one_way_ms` 是发送至对端接收的端点时间差，`pcap_one_way_ms` 是双端虚拟网卡抓包时间差。`confirmation_ms` 是发包至第一次确认该包的 ACK 到达的耗时，`ack_reverse_ms` 是该份 ACK 在回程上的耗时。`ack_delay_reported_ms` 是 ACK 帧报告的、针对该帧最大已确认包的接收端等待时间。 |
| [判丢事件 `loss-events.csv`](../../results/pacing-loss/loss-events.csv) | 包含逐包字段和判丢时的内部快照。`age_at_loss_ms` 为发出至判丢的耗时。`deadline_age_ms` 为源码计算的期限减去原包发送时刻。包号阈值分支会将期限设为当前时刻。`base_timer_at_loss_ms` 和 `effective_timer_at_loss_ms` 是判丢时的基础计时值与考虑退避后的有效计时值。`ack_gap_at_loss` 是最大已确认包号减去原包号。`first_repeat_send_ms` 和 `first_repeat_receive_ms` 表示携带重叠流数据的新包首次发出和抵达的时刻。`native_*_us` 的时间点使用系统单调时钟，单位为 µs。 |
| [逐任务结果 `tasks.csv`](../../results/pacing-loss/tasks.csv) | `upload_ms` 是提交任务至接收端完整收齐的耗时。`on_time20` 表示是否在 20 ms 内完成。预热任务不进入表内。 |
| [条件汇总 `summary.csv`](../../results/pacing-loss/summary.csv) | 按干预条件汇总正式任务。前向包时延从已抵达的包计算，真丢弃保留在发送分母。`repeated_bytes` 是与此前发送区间重叠的业务流字节数。`first_timer_*` 是每笔任务第一个前向数据包的有效计时值分布。`declarations_with_repeated_data` 统计找到实际发送重叠数据的判丢事件。 |
| [运行环境](../../results/pacing-loss/environment.json) · [验证](../../results/pacing-loss/validation.json) | 依赖提交、二进制及脚本哈希、轮次分母、逐包配对数量、抓包与端点时延的差值。 |

空白时延表示缺少相应到达、确认或判丢事件。`measured=1` 表示包内含测量任务的流帧，报告的前向分组再筛选 `direction=forward`。握手和控制事件仍可在全量表及原始数据中查看。
