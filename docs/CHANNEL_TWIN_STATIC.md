# Channel Twin 静态基线部署与验证

本页对应第一阶段 `spaceverse-compatible-static`。它验证一颗逻辑卫星、一个逻辑地面站的双向 TCP 数据传输、持久化队列和固定网络条件。旧的 `ground/`、`satellite/` 是计算服务骨架；新增的 `channel_twin/` 是独立传输软件，二者不要混为同一个端口。

应用接口契约见 [`api/openapi-channel-twin.yaml`](../api/openapi-channel-twin.yaml)。

## 1. 当前静态参数

唯一配置源是 [`configs/channel/scenarios/spaceverse-compatible-static.json`](../configs/channel/scenarios/spaceverse-compatible-static.json)：

| 方向                |    有效速率 | 附加单向时延 | jitter | 残余丢包 |
| ------------------- | ----------: | -----------: | -----: | -------: |
| ground → satellite | 110.67 Mbps |         0 ms |   0 ms |       0% |
| satellite → ground | 110.67 Mbps |         0 ms |   0 ms |       0% |

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
sudo apt install -y openvswitch-switch iproute2 python3 python3-venv iperf3 tcpdump curl
sudo systemctl enable --now openvswitch-switch
```

本次实机配置来自 `ipinfo.txt`（DHCP 地址变化后必须重新检查）：

- 地面底层地址：`10.29.226.243`，物理接口 `wlo1`；
- Orin 底层地址：`10.29.251.126`，物理接口 `wlP1p1s0`；
- OVS 数据地址固定为地面 `10.77.0.1`、卫星 `10.77.0.2`。

### 4.1 本次实机预检查

所有命令均在对应机器的仓库根目录执行。地面端是 `/home/rog/workspace/testbed`；Orin 用 `ssh nvidia@10.29.251.126` 登录后进入其仓库目录，并同步本次修改的脚本。

2026-09-10 地面端已确认 `10.29.226.243/16`、`wlo1` MTU 1500、OVS 服务 active、Python 3.13.5、iproute2 6.15.0。到 Orin 的路由使用 `wlo1`；3 次底层 ping 为 7.25–258.50 ms，未丢包。这只证明当时 ICMP 可达，尚未验证 UDP 4789、Orin 内核 IFB/netem 支持或双端 OVS 数据面。

地面端：

```bash
ip -4 addr show dev wlo1
ip -4 route get 10.29.251.126
ping -c 20 10.29.251.126
```

Orin 端：

```bash
ip -4 addr show dev wlP1p1s0
ip -4 route get 10.29.226.243
ping -c 20 10.29.226.243
python3 --version  # 本项目要求 >= 3.10
sudo modprobe ifb
sudo modprobe sch_htb
sudo modprobe sch_netem
sudo systemctl is-active openvswitch-switch
```

若 `modprobe` 报模块不存在，检查 `zcat /proc/config.gz`（若存在）中的 `CONFIG_IFB`、`CONFIG_NET_SCH_HTB`、`CONFIG_NET_SCH_NETEM`；内建为 `y` 也可用。若功能未编译，需要与当前 Jetson 内核匹配的模块或内核，不能靠安装 Python 包解决。

两端均使用 Wi-Fi。保持物理 IP 在 Wi-Fi 接口上，OVS 仅连接内部口和 VXLAN 端口。地面存在 `SakuraiTunnel`，配置后还要确认 `ip route get 10.77.0.2` 指向 `sgt-data`，避免 VPN 策略路由捕获数据流量。脚本会检查到对端底层 IP 的路由是否使用指定物理口，并将 VXLAN 源地址固定为该路由源地址。

默认数据 MTU 为 1450，为 IPv4 VXLAN 封装预留 50 字节；可通过 `--data-mtu` 降低，两端保持一致。参考 [OVS MTU 讨论](https://mail.openvswitch.org/pipermail/ovs-discuss/2019-September/049254.html) 和 [OVS VXLAN 文档](https://docs.openvswitch.org/en/latest/faq/vxlan/)。不要把 Wi-Fi 本身的吞吐、丢包和抖动当成 netem 新增损伤；110.67 Mbps 是整形上限，底层容量不足时无法达到该值。

### 4.2 建立数据面

先在地面 Linux 执行：

```bash
sudo bash deployment/channel-twin/setup-static.sh \
  --role ground \
  --physical-iface wlo1 \
  --peer-underlay-ip 10.29.251.126
```

再在 Orin 执行：

```bash
sudo bash deployment/channel-twin/setup-static.sh \
  --role satellite \
  --physical-iface wlP1p1s0 \
  --peer-underlay-ip 10.29.226.243
```

脚本从静态场景 JSON 读取本机接收方向的参数，不另外维护一份速率常量。它建立 `br-sgt`、内部端口 `sgt-data`、VXLAN 端口 `sgt-vx`、`ifb-sgt`，并仅匹配来自对端、目标 UDP 4789 的包。若局域网防火墙存在，需要仅对受信任对端开放 UDP 4789；不要把 VXLAN 暴露到公网。

在两端确认：

```bash
ping -c 3 10.77.0.2  # 地面执行
ping -c 3 10.77.0.1  # 卫星执行
sudo ovs-vsctl show
sudo tc -s -d qdisc show dev ifb-sgt
```

地面再执行 `ping -M do -s 1422 -c 3 10.77.0.2`，Orin 对 `10.77.0.1` 执行同样测试，验证 1450 字节内层 IPv4 包可通过。
若小 ping 也不通，两端分别抓包：

```bash
sudo tcpdump -ni wlo1 'host 10.29.251.126 and udp port 4789'       # 地面
sudo tcpdump -ni wlP1p1s0 'host 10.29.226.243 and udp port 4789'  # Orin
```

并检查 `sudo ovs-vsctl list Interface sgt-vx` 的 `error` 字段、两端 `sudo nft list ruleset` 和 Wi-Fi 客户端隔离策略。只有发送端看到封装包而接收端没有时，先排查底层路径/防火墙；不要通过关闭整个防火墙来排障。

### 4.3 启动 Gateway

首次运行前，在两端各自的仓库根目录执行。地面端设置：

```bash
export TWIN_ROLE=ground
export TWIN_API_IP=10.29.226.243
```

Orin 端设置：

```bash
export TWIN_ROLE=satellite
export TWIN_API_IP=10.29.251.126
```

只在地面端生成一次 token：`python3 -c 'import secrets; print(secrets.token_hex(32))'`。
将它复制到两端下述交互输入中（不要提交到 Git）。运行配置放到已忽略的 `results/`；数据 IP 保留为 OVS 地址，API 绑定本机管理地址：

```bash
read -rsp 'Shared channel token: ' TWIN_TOKEN; echo
export TWIN_TOKEN
python3 - <<'PYCONFIG'
import json, os
from pathlib import Path
root = Path.cwd()
role = os.environ['TWIN_ROLE']
config = json.loads((root / f'configs/channel/{role}.example.json').read_text())
config.update(
    api_listen_host=os.environ['TWIN_API_IP'],
    auth_token=os.environ['TWIN_TOKEN'],
    spool_dir=str(root / f'results/channel-twin/{role}'),
    scenario_file=str(root / 'configs/channel/scenarios/spaceverse-compatible-static.json'),
)
out = root / f'results/channel-twin-config/{role}.json'
out.parent.mkdir(parents=True, exist_ok=True)
with open(out, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
    json.dump(config, stream, indent=2)
out.chmod(0o600)
print(out)
PYCONFIG
unset TWIN_TOKEN
```

然后以前台运行：

```bash
python3 -m channel_twin --config results/channel-twin-config/ground.json
python3 -m channel_twin --config results/channel-twin-config/satellite.json
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

在对应机器保留上面设置的 `TWIN_ROLE`，安装已验证的运行配置和本机网络参数：

```bash
sudo install -m 640 "results/channel-twin-config/$TWIN_ROLE.json" "/etc/sate2gs-testbed/channel-twin/$TWIN_ROLE.json"
if [ "$TWIN_ROLE" = ground ]; then
  printf 'PHYSICAL_IFACE=wlo1\nPEER_UNDERLAY_IP=10.29.251.126\n'
else
  printf 'PHYSICAL_IFACE=wlP1p1s0\nPEER_UNDERLAY_IP=10.29.226.243\n'
fi | sudo tee "/etc/sate2gs-testbed/channel-twin/$TWIN_ROLE-network"
sudo python3 - "$TWIN_ROLE" <<'PYSERVICE'
import json, sys
from pathlib import Path
role = sys.argv[1]
p = Path(f'/etc/sate2gs-testbed/channel-twin/{role}.json')
c = json.loads(p.read_text())
c.update(spool_dir=f'/var/lib/sate2gs-testbed/channel-twin/{role}',
         scenario_file='/opt/sate2gs-testbed/configs/channel/scenarios/spaceverse-compatible-static.json')
p.write_text(json.dumps(c, indent=2))
PYSERVICE
```

安装后的 JSON 路径字段如下（保留其余字段）：

```json
{
  "spool_dir": "/var/lib/sate2gs-testbed/channel-twin/ground",
  "scenario_file": "/opt/sate2gs-testbed/configs/channel/scenarios/spaceverse-compatible-static.json"
}
```

卫星将目录末尾改为 `satellite`。停止前台 Gateway（Ctrl+C），释放 8090/9000 端口。创建 `spaceverse` 用户并把对应 `/var/lib` 目录交给它后启动：

```bash
id spaceverse >/dev/null 2>&1 || sudo useradd --system --user-group --home /opt/sate2gs-testbed --shell /usr/sbin/nologin spaceverse
sudo chown root:spaceverse "/etc/sate2gs-testbed/channel-twin/$TWIN_ROLE.json"
sudo chown -R spaceverse:spaceverse /var/lib/sate2gs-testbed/channel-twin
sudo systemctl daemon-reload
sudo systemctl enable --now "channel-twin@$TWIN_ROLE"
```

[`channel-twin-network@.service`](../deployment/systemd/channel-twin-network@.service) 以 root 恢复 OVS/IFB/tc；[`channel-twin@.service`](../deployment/systemd/channel-twin@.service) 以普通 `spaceverse` 用户运行 Gateway。后者显式依赖前者，因此重启后不会绕过静态信道直接启动。

清理时，若已启用 systemd，先在对应机器执行 `sudo systemctl disable --now "channel-twin@$TWIN_ROLE"` 和 `sudo systemctl stop "channel-twin-network@$TWIN_ROLE"`。手动清理在两端分别执行，并明确传入本机物理接口：

```bash
sudo bash deployment/channel-twin/teardown-static.sh --physical-iface wlo1
sudo bash deployment/channel-twin/teardown-static.sh --physical-iface wlP1p1s0
```

清理脚本只删除本项目的 filter 优先级、`ifb-sgt` 和 `br-sgt`，保留可能被其他程序共用的物理接口 `clsact`。

## 5. 静态阶段验收清单

1. 两端 `health` 正常，`10.77.0.1 ↔ 10.77.0.2` 可达。
2. 上行和下行各传至少一个真实文件，远端下载结果的 SHA-256 与原文件一致。
3. `tc -s` 计数随对应方向传输增长；物理 SSH/API 流量不进入该 filter。
4. 独立标定 run 使用 `iperf3` 检查大文件稳定速率，不把应用净 goodput 强行等同于 110.67 Mbps。
5. 传输中暂时停止对端 Gateway 或阻断 VXLAN，发送状态进入 `RETRY_WAIT`；恢复后最终进入 `DELIVERED`，远端只出现一份交付。
6. 保存 `ovs-vsctl show`、`tc -s -d`、两端 `events.jsonl`、内核/OVS/iproute2 版本和本底 RTT。

本次地面端是 Debian Linux，可执行 OVS/tc 实机验收；本机协议自动测试通过仍不能代替两端数据面验收。

### 5.1 实际双向文件验收（地面终端）

两端 Gateway 已启动后，在地面新终端执行。这里使用运行配置中的 token，不需要再次手工输入。若已经迁移至 systemd，改为从受权限保护的安装配置取值。

```bash
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}10.29.226.243,10.29.251.126,10.77.0.1,10.77.0.2"
export no_proxy="$NO_PROXY"
TWIN_TOKEN=$(python3 -c 'import json; print(json.load(open("results/channel-twin-config/ground.json"))["auth_token"])')
RUN_ID="static-$(date +%Y%m%d-%H%M%S)"
UP_ID="$RUN_ID-up"
DOWN_ID="$RUN_ID-down"
curl --noproxy '*' --fail http://10.29.226.243:8090/health
curl --noproxy '*' --fail http://10.29.251.126:8090/health
python3 scripts/channel_transfer.py --api http://10.29.226.243:8090 --token "$TWIN_TOKEN" submit --id "$UP_ID" --file README.md
python3 scripts/channel_transfer.py --api http://10.29.226.243:8090 --token "$TWIN_TOKEN" status --id "$UP_ID"
# 等上面的状态变为 DELIVERED 后下载；尚未到达时重复 status。
python3 scripts/channel_transfer.py --api http://10.29.251.126:8090 --token "$TWIN_TOKEN" download --id "$UP_ID" --output "results/$UP_ID.md"
cmp README.md "results/$UP_ID.md"
sha256sum README.md "results/$UP_ID.md"

# 通过 Orin 的管理 API 提交，再由 Orin Gateway 经受控数据面发回地面。
python3 scripts/channel_transfer.py --api http://10.29.251.126:8090 --token "$TWIN_TOKEN" submit --id "$DOWN_ID" --file README.md
python3 scripts/channel_transfer.py --api http://10.29.251.126:8090 --token "$TWIN_TOKEN" status --id "$DOWN_ID"
# 等 DELIVERED 后执行：
python3 scripts/channel_transfer.py --api http://10.29.226.243:8090 --token "$TWIN_TOKEN" download --id "$DOWN_ID" --output "results/$DOWN_ID.md"
cmp README.md "results/$DOWN_ID.md"
sha256sum README.md "results/$DOWN_ID.md"
unset TWIN_TOKEN
```

`cmp` 无输出且退出码为 0 表示一致。管理 API 上传/下载本身不计入模拟链路传输时间。

### 5.2 独立吞吐标定与重试

暂停业务提交，Orin 执行 `iperf3 -s -B 10.77.0.2`；地面依次执行：

```bash
iperf3 -c 10.77.0.2 -B 10.77.0.1 -t 30 -O 3
iperf3 -c 10.77.0.2 -B 10.77.0.1 -t 30 -O 3 -R
sudo tc -s filter show dev wlo1 ingress pref 49100
sudo tc -s -d qdisc show dev ifb-sgt
```

Orin 对 `wlP1p1s0` 查看同一优先级的 filter 和 `ifb-sgt` 计数。上下行顺序测量，保留发送/接收结果及重传计数。HTB 当前统计的是封装后的接收包，应用 TCP goodput 还会扣除协议开销。另开独立 run，用 Orin `iperf3 -s -B 10.29.251.126`、地面 `iperf3 -c 10.29.251.126 -t 30 -O 3`（再加 `-R`）记录 Wi-Fi 未整形基线；更换绑定地址前停止前一个 iperf3 server。

初次重试检查：停止 Orin 前台 Gateway（Ctrl+C）或 `sudo systemctl stop channel-twin@satellite`；地面用新 ID 提交文件，等待 `RETRY_WAIT`；再以原配置启动 Orin Gateway，确认最终 `DELIVERED`、`deliveries --id` 只有该交付且下载校验通过。正式清单第 5 项还要在一个较大文件正在传输时重复中断，保留事件日志证明中途失败和恢复。

保存每端 `uname -a`、`ovs-vsctl --version`、`/usr/sbin/tc -V`、`sudo ovs-vsctl show`、filter/qdisc 统计、上述测量输出，以及各自 spool 内的 `events.jsonl`。此处命令是验收步骤，不代表已经完成实机验收。

## 6. 第二阶段接口边界

动态 trace 阶段保留同一 Gateway/TCP/spool，不改计算程序接口。新增组件只负责按 1× 时间轴将每个方向的 `available/rate/delay/jitter/loss` 更新到 OVS/tc，并记录计划值与实际生效值。

WetLinks 和 LENS 是 Starlink 端到端服务测量，字段、时间粒度和测量口径不同，不能直接拼接，也不能称为遥感卫星直连实测。两个 clone 已在外部只读路径找到，第二阶段实现与当前可用数据状态见 [`CHANNEL_TWIN_DYNAMIC.md`](./CHANNEL_TWIN_DYNAMIC.md)。
