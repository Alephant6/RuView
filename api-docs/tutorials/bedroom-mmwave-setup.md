# 卧室 mmWave 高精度部署指南

为卧室场景设计的 60 GHz mmWave + WiFi CSI 双模融合部署方案。
mmWave 提供临床级生命体征精度（呼吸 ±0.5 BPM、心率 ±1–2 BPM），
WiFi CSI 提供全房间存在/姿态覆盖。

**预计时间：** 45 分钟（采购到位后，含布线、ESPHome 烧录、融合校准）
**相关 ADR：** ADR-063（mmWave 传感器融合）、ADR-021（生命体征提取）、ADR-029（RuvSense 多基地）

---

## 1. 物料清单（BOM）

### 必需

| 数量 | 物料 | 用途 | 参考价 |
|------|------|------|--------|
| 1 | Seeed MR60BHA2 + ESP32-C6 套件 | 床区生命体征 | ~$15 |
| 1 | ESP32-S3（8 MB flash） | 全房间 WiFi CSI | ~$9 |
| 2 | USB-C 5V/1A 电源 | 长期供电 | ~$6 |
| — | 双面胶 / 3M VHB | 天花板/墙面固定 | — |

### 可选（再上一档精度）

| 数量 | 物料 | 用途 | 参考价 |
|------|------|------|--------|
| +1 | Seeed MR60BHA2（第二只） | 多基地几何分集，CRB 降一半 | ~$15 |
| 1 | HLK-LD2410（24 GHz） | 在床/离床二值门控 | ~$3 |
| 1 | ESP32-S3 SuperMini（4 MB） | 第二 CSI 视点 | ~$6 |

---

## 2. 拓扑与放置（最关键的一步）

```
                        天花板
                          |
                          | 主 MR60BHA2（俯视床面）
                          v 距床面 0.8 - 1.5 m，俯角 15-30°
              +-----------------------+
              |                       |
              |          床           |
              |                       |
              +-----------------------+
        ESP32-S3 (CSI)            可选第二 MR60BHA2
        墙上 1.5 m 高              对侧床头墙
        覆盖全屋                   距床面 1.5 m
```

### 主 MR60BHA2 位置规则

| 项目 | 推荐 | 原因 |
|------|------|------|
| 距床面距离 | **0.8–1.5 m** | 60 GHz 距离 < 0.5 m 会饱和，> 2 m 信噪比急剧下降 |
| 俯角 | **15°–30°** 向下 | 让 120° 锥形 FOV 主要覆盖胸腹部，避开头顶反射 |
| 朝向 | **正对胸部中线** | 呼吸由膈肌驱动，胸腹是最强反射体 |
| 安装方式 | 床头墙顶 或 天花板支架 | 顶装最稳，避免被人遮挡 LoS |

### 严禁位置 / 干扰源

- ❌ **窗帘、风扇、加湿器水雾** 在 FOV 内 —— 产生持续伪多普勒，会把呼吸/心率搞乱
- ❌ **金属衣柜门、镜子** 正对 —— 强镜面反射会产生鬼影目标
- ❌ **空调出风口** 直吹模块 —— 振动 + 气流多普勒
- ❌ **距墙角 < 30 cm** —— 多径混叠
- ❌ **正对窗外街道** —— 60 GHz 衰减大，但车流仍可能进入近场

### ESP32-S3 CSI 节点位置

- 墙上 **1.5 m 高**，对角线放置，覆盖整个卧室
- 与路由器**不要同一面墙**，否则直射径过强淹没多径
- USB 5V 直供，**不要走电池**（CSI 对电源噪声敏感）

### 可选第二 MR60BHA2（多基地）

放在床的**对侧**（床尾墙或对侧床头），与主节点**几何夹角 60°–120°** 最佳。
两路独立观测后，`ruvector/src/viewpoint/fusion.rs` 的 `MultistaticArray`
会自动加权融合，Fisher Information 大约翻倍，对应 CRB 下降 ~30%。

### 可选 HLK-LD2410（床下/床边）

装在床边矮柜或床下，仅作为 **在床/离床二值信号** 输入 `coherence_gate.rs`。
当 LD2410 报告 "无人在床" 时，mmWave 的心率读数会被门控拒绝，
显著减少翻身/坐起瞬间的伪峰。

---

## 3. ESPHome 配置（MR60BHA2 + ESP32-C6）

把下面这段保存为 `bedroom-mmwave.yaml`，
然后 `esphome run bedroom-mmwave.yaml` 烧录（串口默认 COM4 / `/dev/ttyACM0`）。

```yaml
esphome:
  name: bedroom-mmwave
  friendly_name: Bedroom mmWave

esp32:
  board: esp32-c6-devkitc-1
  variant: esp32c6
  framework:
    type: esp-idf

logger:
  level: DEBUG
  baud_rate: 0  # 禁用 UART0 日志，UART0 给 MR60BHA2 用

api:
  encryption:
    key: !secret api_key

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

uart:
  - id: mr60_uart
    tx_pin: GPIO16
    rx_pin: GPIO17
    baud_rate: 115200
    parity: NONE
    stop_bits: 1

external_components:
  - source: github://limengdu/MR60BHA2_ESPHome_external_components
    components: [seeed_mr60bha2]

seeed_mr60bha2:
  uart_id: mr60_uart
  id: mr60

sensor:
  - platform: seeed_mr60bha2
    seeed_mr60bha2_id: mr60
    breath_rate:
      name: "呼吸率"
      id: br
      filters:
        - sliding_window_moving_average:
            window_size: 10
            send_every: 2
    heart_rate:
      name: "心率"
      id: hr
      filters:
        - sliding_window_moving_average:
            window_size: 10
            send_every: 2
    distance:
      name: "目标距离"
    num_targets:
      name: "目标数"

binary_sensor:
  - platform: seeed_mr60bha2
    seeed_mr60bha2_id: mr60
    has_target:
      name: "床上有人"
```

### 接线（MR60BHA2 → ESP32-C6）

| MR60BHA2 引脚 | ESP32-C6 引脚 |
|--------------|--------------|
| 5V           | 5V           |
| GND          | GND          |
| TX           | GPIO17 (RX)  |
| RX           | GPIO16 (TX)  |

---

## 4. 与 RuView 主管道融合

烧录完成后，在 RuView 配置里注册新节点：

```bash
# 1. 注册 mmWave 节点（mmWave 数据通过 ESPHome API / MQTT 进入）
npx @claude-flow/cli@latest memory store \
  --key "node-mmwave-bedroom" \
  --value '{"type":"mr60bha2","room":"bedroom","host":"bedroom-mmwave.local","fov_deg":120,"max_range_m":3}' \
  --namespace nodes

# 2. 注册 CSI 节点
npx @claude-flow/cli@latest memory store \
  --key "node-csi-bedroom" \
  --value '{"type":"esp32-s3","room":"bedroom","port":"COM7","mode":"csi"}' \
  --namespace nodes

# 3. 启动融合管道（自动启用 ADR-063 双模融合）
cd v2
cargo run -p wifi-densepose-cli -- sense \
  --room bedroom \
  --fusion mmwave-csi \
  --output /tmp/bedroom-vitals.jsonl
```

融合规则（在 `wifi-densepose-signal/src/ruvsense/` 中已实现）：

| 信号 | 主信号源 | 交叉验证 |
|------|---------|---------|
| 呼吸率 | mmWave (`breathing.rs`) | CSI 相位（粗筛） |
| 心率 | mmWave (`bvp.rs`) | CSI 振幅包络 |
| 在床/离床 | LD2410（如启用）→ `coherence_gate.rs` | mmWave `has_target` |
| 全房间姿态 | CSI (`pose_tracker.rs`) | — |
| 跌倒确认 | mmWave 速度 + CSI 相位加速度 双确认 | `adversarial.rs` 一致性 |

---

## 5. 校准与验收

部署完成后跑一遍校准：

```bash
# 1. 静坐 5 分钟，对比 mmWave 读数与同步佩戴的 Apple Watch / Polar H10
#    呼吸目标：±0.5 BPM，心率目标：±2 BPM
python archive/v1/scripts/calibrate_vitals.py \
  --node bedroom-mmwave --duration 300 --reference polar

# 2. 跑确定性证明，确保 fused 管道未破坏 CSI 基线
python archive/v1/data/proof/verify.py
# 期望：VERDICT: PASS

# 3. Rust 全测试
cd v2 && cargo test --workspace --no-default-features
# 期望：1,031+ passed, 0 failed
```

### 验收清单

- [ ] MR60BHA2 在静止人体下 60 秒内**呼吸率稳定** ±1 BPM
- [ ] 离床时 `has_target` 在 **5 秒内**变为 OFF
- [ ] CSI 节点同一时间段无 packet loss（`bedroom-vitals.jsonl` 中 `csi_drop_rate < 1%`）
- [ ] 风扇/窗帘抖动情况下未产生伪心率（在 `adversarial.rs` 日志中应被拒绝）
- [ ] Apple Watch / Polar 对比误差 RMSE 心率 < 3 BPM，呼吸 < 1 BPM

---

## 6. 常见问题

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| 心率读数在 40–120 之间跳变 | 模块距床面 > 2 m，或被被子完全盖住 | 降低距离至 1 m；用透气棉被 |
| `has_target` 一直 ON 即使无人 | 风扇/窗帘在 FOV 内 | 调整模块朝向，或缩窄 `detection_range` |
| 呼吸率始终 0 | 模块朝向错误（对着头/脚） | 重新对准胸腹中线 |
| CSI 与 mmWave 严重不一致 | 时钟未同步 | 两个节点都接同一个 NTP，启用 `chrony` |
| 第二只 MR60BHA2 没生效 | 几何夹角 < 30° | 移到对侧墙，达到 60°–120° |

---

## 7. 升级路径

如果还想要更高精度：

1. **第三只 mmWave 节点 + 1 ESP32-S3** → 三基地几何，CRB 再降 ~20%
2. **NV-diamond 磁强计**（ADR-089 `nvsim`）→ 实验性，可做心磁图级生命体征
3. **rvCSI 边缘运行时**（`vendor/rvcsi`）→ 把 CSI 事件检测下沉到节点本地，减少融合延迟

---

**参考文档**

- `docs/adr/ADR-063-mmwave-sensor-fusion.md` —— 融合架构决策
- `docs/adr/ADR-021-vital-sign-extraction.md` —— 生命体征算法
- `docs/adr/ADR-029-ruvsense-multistatic.md` —— 多基地传感
- `v2/crates/wifi-densepose-signal/src/ruvsense/` —— RuvSense 14 模块源码
