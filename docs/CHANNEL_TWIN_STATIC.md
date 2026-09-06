# Channel Twin 静态基线部署与验证

本页对应第一阶段 `spaceverse-compatible-static`。它验证一颗逻辑卫星、一个逻辑地面站的双向 TCP 数据传输、持久化队列和固定网络条件。旧的 `ground/`、`satellite/` 是计算服务骨架；新增的 `channel_twin/` 是独立传输软件，二者不要混为同一个端口。

应用接口契约见 [`api/openapi-channel-twin.yaml`](../api/openapi-channel-twin.yaml)。

## 1. 当前静态参数

唯一配置源是 [`configs/channel/scenarios/spaceverse-compatible-static.json`](../configs/channel/scenarios/spaceverse-compatible-static.json)：

| 方向 | 有效速率 | 附加单向时延 | jitter | 残余丢包 |
|---|---:|---:|---:|---:|
| ground → satellite | 110.67 Mbps | 0 ms | 0 ms | 0% |
| satellite → ground | 110.67 Mbps | 0 ms | 0 ms | 0% |

SpaceVerse 明确报告的是默认**下行**带宽 110.67 Mbps。上行使用相同速率是本项目用于第一阶段互通的对称工程假设；0 ms/0% 是校准基线，不是论文测量值。实际 LAN、VM 与软件仍会产生本底时延。实验材料中必须保留这个区别。

## 2. 数据流程

```text
计算程序
  → 本端 HTTP API（管理/应用接口）
  → 持久化 outbox，返回 ACCEPTED
  → 串行发送器
  → 本端 OVS 内部数据 IP
  → VXLAN（物理局域网）
  → 对端物理口 ingress → IFB + tc 固定损伤
  → 对端 OVS 内部数据 IP
  → 对端 TCP Receiver → SHA-256 校验 → inbox
  → 同一受控数据路径返回回执
  → 发送端状态 DELIVERED
```

每台机器只整形**自己接收的 VXLAN 流量**：地面端承载下行 profile，卫星端承载上行 profile。TCP ACK 会走反方向并受到相应 profile 约束。SSH、NTP 和 HTTP 管理 API 使用物理局域网地址，不进入 VXLAN 过滤器。

## 3. 不带信道损伤的本机协议测试

该模式可在 Windows/Linux 上运行，用于先验证双端框架，但不构成 OVS/tc 验收。

终端一：

```powershell
python -m channel_twin --config configs/channel/ground.local.json
```

终端二：

```powershell
python -m channel_twin --config configs/channel/satellite.local.json
```

准备任意文件并从地面上行：

```powershell
python scripts/channel_transfer.py `
  --api http://127.0.0.1:8090 `
  --token static-local-only-change-me `
  submit --id uplink-demo-001 --file .\README.md --artifact-type text/markdown
```

查询发送状态和卫星收件箱：

```powershell
python scripts/channel_transfer.py --api http://127.0.0.1:8090 --token static-local-only-change-me status --id uplink-demo-001
python scripts/channel_transfer.py --api http://127.0.0.1:8091 --token static-local-only-change-me deliveries --id uplink-demo-001
```

将 `--api` 改为卫星端 `8091` 后提交，即测试下行。相同 `transfer_id` 和 SHA-256 重复提交具有幂等性；不同内容不得复用 ID。

自动测试：

```powershell
python -m unittest discover -s tests -v
```

## 4. 两台 Linux 的 OVS/tc 静态数据面

前提：PC 地面端也必须有 Linux 网络环境。若大模型运行在 Windows，Ground Twin 与 OVS/tc 放到桥接网络的 Ubuntu VM；Windows 计算程序调用 VM 的 API。首次执行网络脚本前保持本地控制台可用，并记录真实物理接口和两台机器的局域网 IPv4。

Ubuntu/Jetson 安装：

```bash
sudo apt update
sudo apt install -y openvswitch-switch iproute2 python3
```

以下示例假设：

- 地面底层地址：`192.168.1.100`，物理接口 `eno1`；
- Orin 底层地址：`192.168.1.101`，物理接口 `eth0`；
- OVS 数据地址固定为地面 `10.77.0.1`、卫星 `10.77.0.2`。

先在地面 Linux 执行：

```bash
sudo bash deployment/channel-twin/setup-static.sh \
  --role ground \
  --physical-iface eno1 \
  --peer-underlay-ip 192.168.1.101
```

再在 Orin 执行：

```bash
sudo bash deployment/channel-twin/setup-static.sh \
  --role satellite \
  --physical-iface eth0 \
  --peer-underlay-ip 192.168.1.100
```

脚本从静态场景 JSON 读取本机接收方向的参数，不另外维护一份速率常量。它建立 `br-sgt`、内部端口 `sgt-data`、VXLAN 端口 `sgt-vx`、`ifb-sgt`，并仅匹配来自对端、目标 UDP 4789 的包。若局域网防火墙存在，需要仅对受信任对端开放 UDP 4789；不要把 VXLAN 暴露到公网。

在两端确认：

```bash
ping -c 3 10.77.0.2  # 地面执行
ping -c 3 10.77.0.1  # 卫星执行
sudo ovs-vsctl show
sudo tc -s -d qdisc show dev ifb-sgt
```

复制 example 配置为运行配置，两端设置相同的随机 token。数据地址保持 `10.77.0.1/10.77.0.2`；API 绑定物理管理网络。然后以前台运行：

```bash
python3 -m channel_twin --config configs/channel/ground.example.json
python3 -m channel_twin --config configs/channel/satellite.example.json
```

两条命令分别在对应机器执行。两台机器可都使用 API 端口 8090，因为 IP 不同。

首次联调通过后再安装 systemd 服务。每端至少完成以下操作：

```bash
sudo mkdir -p /opt/sate2gs-testbed /etc/sate2gs-testbed/channel-twin
sudo mkdir -p /var/lib/sate2gs-testbed/channel-twin/ground
sudo mkdir -p /var/lib/sate2gs-testbed/channel-twin/satellite
sudo cp -r channel_twin configs deployment scripts pyproject.toml /opt/sate2gs-testbed/
sudo python3 -m venv /opt/sate2gs-testbed/.venv
sudo cp deployment/systemd/channel-twin@.service /etc/systemd/system/
sudo cp deployment/systemd/channel-twin-network@.service /etc/systemd/system/
```

只在地面复制 `ground.example.json → /etc/sate2gs-testbed/channel-twin/ground.json` 和 `ground-network.example.env → .../ground-network`，卫星同理使用 satellite 文件。运行 JSON 还需改为：

```json
{
  "spool_dir": "/var/lib/sate2gs-testbed/channel-twin/ground",
  "scenario_file": "/opt/sate2gs-testbed/configs/channel/scenarios/spaceverse-compatible-static.json"
}
```

卫星将目录末尾改为 `satellite`。同步修改 token、物理接口和对端底层 IP，创建 `spaceverse` 用户并把对应 `/var/lib` 目录交给它后启动：

```bash
sudo useradd --system --home /opt/sate2gs-testbed --shell /usr/sbin/nologin spaceverse
sudo chown -R spaceverse:spaceverse /var/lib/sate2gs-testbed/channel-twin
sudo systemctl daemon-reload
sudo systemctl enable --now channel-twin@ground   # 地面
sudo systemctl enable --now channel-twin@satellite  # 卫星
```

[`channel-twin-network@.service`](../deployment/systemd/channel-twin-network@.service) 以 root 恢复 OVS/IFB/tc；[`channel-twin@.service`](../deployment/systemd/channel-twin@.service) 以普通 `spaceverse` 用户运行 Gateway。后者显式依赖前者，因此重启后不会绕过静态信道直接启动。

清理时在两端分别执行，并明确传入本机物理接口：

```bash
sudo bash deployment/channel-twin/teardown-static.sh --physical-iface eno1
sudo bash deployment/channel-twin/teardown-static.sh --physical-iface eth0
```

清理脚本只删除本项目的 filter 优先级、`ifb-sgt` 和 `br-sgt`，保留可能被其他程序共用的物理接口 `clsact`。

## 5. 静态阶段验收清单

1. 两端 `health` 正常，`10.77.0.1 ↔ 10.77.0.2` 可达。
2. 上行和下行各传至少一个真实文件，远端下载结果的 SHA-256 与原文件一致。
3. `tc -s` 计数随对应方向传输增长；物理 SSH/API 流量不进入该 filter。
4. 独立标定 run 使用 `iperf3` 检查大文件稳定速率，不把应用净 goodput 强行等同于 110.67 Mbps。
5. 传输中暂时停止对端 Gateway 或阻断 VXLAN，发送状态进入 `RETRY_WAIT`；恢复后最终进入 `DELIVERED`，远端只出现一份交付。
6. 保存 `ovs-vsctl show`、`tc -s -d`、两端 `events.jsonl`、内核/OVS/iproute2 版本和本底 RTT。

当前 Windows 开发环境只能完成协议与持久化自动测试，不能代替第 4、5 节的 Linux OVS/tc 实机验收。

## 6. 第二阶段接口边界

动态 trace 阶段保留同一 Gateway/TCP/spool，不改计算程序接口。新增组件只负责按 1× 时间轴将每个方向的 `available/rate/delay/jitter/loss` 更新到 OVS/tc，并记录计划值与实际生效值。

WetLinks 和 LENS 是 Starlink 端到端服务测量，字段、时间粒度和测量口径不同，不能直接拼接，也不能称为遥感卫星直连实测。两个 clone 已在外部只读路径找到，第二阶段实现与当前可用数据状态见 [`CHANNEL_TWIN_DYNAMIC.md`](./CHANNEL_TWIN_DYNAMIC.md)。
