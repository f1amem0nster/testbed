# Channel Twin 动态 trace 编译与回放

动态阶段复用第一阶段的 Gateway、TCP、持久化 outbox/inbox 和 OVS/VXLAN 拓扑。新增代码只负责两件事：把异构测量数据转换为统一 trace；在 1× 时间轴上把本机接收方向的状态更新到 IFB/tc 和 OVS。

## 1. 数据位置与只读原则

当前数据源位于：

```text
D:\ProgramSpace\Sate2GS_testbed\datasets\
├── WetLinks\
└── LENS\
```

转换器只读取该目录，生成结果默认写入当前项目的 `results/channel-traces/`。每份 trace 保存源文件绝对路径、SHA-256、Git commit、许可证和局限；不要修改 clone 内文件来“清洗”数据。

本次核对的 commit：

- WetLinks：`3ed54f99765cf13100e9f502cc96a05d52c7247f`
- LENS：`75d9befd4da77c25b3e7181f200ddda232873bbb`

运行时仍应以生成 trace 中记录的 commit 为准，而不是依赖本文常量。

## 2. canonical trace

格式由 [`schemas/channel-trace.schema.json`](../schemas/channel-trace.schema.json) 描述，运行期还有更严格的连续性校验。每个状态包含：

```text
state_seq
effective_at_offset_ms
duration_ms
links.uplink/downlink:
  available, rate_bps, one_way_delay_ms,
  jitter_ms, loss_pct, queue_limit_packets, provenance
```

状态必须从 offset 0 开始、序号连续且时间无空洞。数据缺测不能自动转为 `available=false`；只有数据明确表示不可用，或场景被明确标为合成 outage 时才能关闭 OVS 链路。

## 3. WetLinks：动态速率辅助场景

WetLinks 的秒级文件来自 UDP iperf。`download/upload` 是测得的端到端 goodput，不是卫星 RF 容量。ping 测量与每秒 iperf 不严格同步，因此首版不强行合并：

- `download` 驱动 downlink `rate_bps`；
- `upload` 驱动 uplink `rate_bps`；
- 时延和丢包由编译参数明确给出，默认 0；
- 遇到空 upload/download、站点变化或超过阈值的采样间隔即结束片段，不解释成 outage。

生成一个 60 秒以内的连续片段：

```powershell
python scripts/compile_channel_trace.py wetlinks `
  --dataset-root 'D:\ProgramSpace\Sate2GS_testbed\datasets\WetLinks' `
  --site Enschede `
  --max-states 60 `
  --fixed-one-way-delay-ms 0 `
  --fixed-loss-pct 0 `
  --output results/channel-traces/wetlinks-enschede.json
```

可用 `--start-at 2023-10-12T15:50:46` 选择片段。生成物称为 `WetLinks dynamic auxiliary service profile`，不能称为遥感卫星直连实测 trace。

仓库已用真实数据验证过一个 15 秒片段：源文件哈希为 `16ec0af7d9e74566217b9eb303cbfd16b86a9aaa2a73f6ecb57ca441ad0c042e`，实际编译结果写在 `results/channel-traces/wetlinks-enschede-15s.json`。`results/` 默认不纳入版本控制；正式实验应将选定 trace 连同 manifest 归档。

## 4. LENS：动态时延、丢包与 outage 辅助场景

LENS Git clone 约 20 MB，只包含说明、下载清单和转换脚本，不包含月度 RAW/CSV 快照。转换器需要从 LENS 数据发布页取得并解压一份 processed IRTT CSV，然后显式传入文件：

```powershell
python scripts/compile_channel_trace.py lens `
  --dataset-root 'D:\ProgramSpace\Sate2GS_testbed\datasets\LENS' `
  --source 'D:\path\to\processed\irtt-file.csv' `
  --tick-ms 1000 `
  --max-states 60 `
  --fixed-rate-bps 110670000 `
  --output results/channel-traces/lens-irtt-60s.json
```

只需要目标站点/时段的一份 processed IRTT CSV，不要为首轮验证下载全部 RAW 月份。转换规则：

- 每 `tick-ms` 聚合方向性 one-way delay，标准差作为该 tick 的 jitter；
- `true_up/true_down` 形成方向性 loss；无法定位方向的 loss 同时计入两个方向，并在 provenance 中计数；
- tick 达到 100% loss 时 `available=false`；阈值可通过 `--outage-loss-pct` 调整并记录；
- LENS 不测容量，所以两方向 rate 使用显式固定控制值；
- 缺少整个聚合 tick 时结束 trace，不把缺测当中断。

IRTT 时延仍包含终端、Starlink、PoP/地面路径和测量服务器，不能改名为纯传播时延。

## 5. 回放前检查

不等待、不修改网络地查看两个接收方向：

```powershell
python -m channel_twin.trace_player --trace TRACE.json --role ground --backend print --no-wait
python -m channel_twin.trace_player --trace TRACE.json --role satellite --backend print --no-wait
```

地面角色只应用 `downlink`，卫星角色只应用 `uplink`。这与静态阶段“在接收侧 IFB 整形”的分工一致。

先用 [`dynamic-outage-demo.json`](../configs/channel/scenarios/dynamic-outage-demo.json) 验证通断、排队和恢复；它是合成测试，不是论文数据。

## 6. 两端实时 Linux 回放

先完成 [`CHANNEL_TWIN_STATIC.md`](./CHANNEL_TWIN_STATIC.md) 的 OVS/tc 初始化和双端 Gateway 联调。把完全相同的 trace 复制到两端并核对 SHA-256。两端 Gateway 运行 JSON 中的 `scenario_id` 必须改为该 trace 的 `scenario_id`，`scenario_file` 必须指向本机这份 trace；Gateway 会记录 `scenario_sha256`，对端 ID 或哈希不一致时拒绝业务传输。使用 NTP/chrony 同步时钟，然后选取一个至少晚 30 秒的共同 UTC 时刻，例如 `2026-09-06T14:00:00+08:00`。

地面 Linux：

```bash
sudo python3 -m channel_twin.trace_player \
  --trace /path/to/trace.json \
  --role ground \
  --backend linux \
  --start-at 2026-09-06T14:00:00+08:00 \
  --audit /var/log/channel-twin/ground-trace.jsonl
```

卫星 Orin：

```bash
sudo python3 -m channel_twin.trace_player \
  --trace /path/to/trace.json \
  --role satellite \
  --backend linux \
  --start-at 2026-09-06T14:00:00+08:00 \
  --audit /var/log/channel-twin/satellite-trace.jsonl
```

播放器用本机 monotonic 时钟执行相对 offset，`--start-at` 只用于两端建立共同起点。它执行受限参数数组，不通过 shell 拼接命令：

- `tc class change` 更新 HTB rate；
- `tc qdisc change` 更新 netem delay/jitter/loss；
- `available=false` 时给 VXLAN ingress ofport 安装带固定 cookie 的 OVS drop flow；恢复时只删除该 cookie 的 flow。

每次应用记录计划 offset、实际 lag、profile 和命令。播放器结束后保留最后一个状态，便于检查；若要恢复静态基线，重新运行 `setup-static.sh`。

## 7. 动态阶段验收

1. 两端使用同一 trace 哈希、共同起点和相同状态数。
2. audit 中 `state_seq` 连续，报告 apply lag 分布；明显超过 tick 的 run 判为无效。
3. `tc -s` 与 OVS flow/port counters 随对应状态变化；每个方向只施加一次。
4. outage 期间已 `ACCEPTED` 样本保持 `RETRY_WAIT`，恢复后进入 `DELIVERED`，远端只有一个已验证交付。
5. 对 WetLinks 场景只声称速率代理随 trace 变化；对 LENS 场景只声称时延/丢包服务测量回放。
6. 主论文遥感直连场景仍需目标卫星、地面站和任务链路数据；这两类 Starlink 场景定位为辅助压力/可重复性场景。

当前开发机不能执行 Linux 的 OVS/tc backend，所以这里已完成编译器、schema、调度逻辑和 print 回放；Linux 命令的真实生效误差仍需在 PC Linux/VM 与 Orin 上按本节验收。
