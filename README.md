# Sate2GS Testbed

本项目用于遥感卫星与地面站之间的双向信道仿真。计算任务与传输解耦：地面端和卫星端计算程序只需调用本地 Channel Twin API，信道软件负责 TCP 传输、断链排队、恢复重传、完整性校验，以及 OVS/tc 网络条件施加。

当前包含：

- `spaceverse-compatible-static` 固定参数双端基线；
- canonical 动态 trace、WetLinks/LENS 转换器与实时回放器；
- Linux/amd64 与 Jetson Orin NX（Linux/arm64）共用的 Python 实现；
- OVS + VXLAN 拓扑和 tc/IFB 双向整形脚本；
- systemd 服务模板、API 契约、trace schema 和自动化测试。

目录说明：

- `channel_twin/`：双端共用的数据面、持久化队列和 trace 回放；
- `configs/channel/`：地面端、卫星端与场景配置；
- `deployment/`：OVS/tc 和 systemd 部署文件；
- `scripts/`：文件提交与 trace 编译命令；
- `datasets/`：外部数据仓库，不由本项目修改或纳入版本控制；
- `docs/`：静态与动态阶段操作指南；
- `星地信道双生模拟软件方案.md`：经课题组确认后的总体方案。

在项目根目录验证：

```powershell
python -m unittest discover -s tests -v
```

无需 OVS/tc 即可先在同一台机器启动逻辑双端：

```powershell
# 终端一
python -m channel_twin --config configs/channel/ground.local.json

# 终端二
python -m channel_twin --config configs/channel/satellite.local.json
```

随后按 [静态基线部署指南](docs/CHANNEL_TWIN_STATIC.md) 完成 PC Linux/VM 与 Orin 的互通和 OVS/tc 验收；动态 trace 见 [动态场景指南](docs/CHANNEL_TWIN_DYNAMIC.md)。

