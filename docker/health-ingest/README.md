# health-ingest：Apple Watch → InfluxDB

每个人的 iPhone 用 **Health Auto Export** 的 REST API 自动化，把 Apple 健康数据
POST 到 NAS 上的这个服务，再写进 InfluxDB 的 `sensing` bucket，和 CSI 数据在同一条时间轴上。
用哪个 token 推送就决定了数据属于谁（`person` 标签），所以每人一个 token。

## NAS 端部署

1. 给每人生成一个 token（至少 16 位）：
   `python3 -c "import secrets;print(secrets.token_urlsafe(24))"`
2. 在 `docker/.env` 里加一行（名字和 RuView 的 profile 名对应）：
   `HEALTH_TOKENS=peter:<token1>,ml:<token2>`
3. 启动：
   `docker compose -f docker-compose.yml -f docker-compose.health.yml up -d --build health-ingest`
4. 检查：`curl http://192.168.1.195:8095/health` 应返回 `{"ok": true}`。

## iPhone 端设置（每人各自的手机）

1. App Store 安装 **Health Auto Export - JSON+CSV**。自动化功能需要付费版。
2. 打开时允许读取健康数据（建议全部允许）。
3. 侧边栏 → Automations → New Automation → 类型选 **REST API**：
   - URL：`http://192.168.1.195:8095/`
   - Headers → Add Headers：key `Authorization`，value `Bearer <自己的 token>`
   - Data Type：Health Metrics（全部）；可另建一个 Workouts 自动化，URL 和 header 相同
   - Export Format：JSON，Export Version：2
   - **Summarize Data：关**（要逐条原始样本，心率才有每个测量点，睡眠才有分段）
   - Date Range：Since Last Sync
   - Batch Requests：开
   - 同步频率：每 1 小时
4. 设置 → Health Auto Export → 打开"后台 App 刷新"，不要长期开低电量模式。
   iPhone 锁屏时 iOS 不允许读健康数据，所以数据会在解锁后补传，时间戳不受影响。
5. 第一次可以把 Date Range 改成 Previous 7 Days 手动跑一次，回填历史，之后改回 Since Last Sync。
   重复上传同一时间段不会产生重复数据（同一时间点会覆盖）。
6. 睡觉时戴着手表，并在 iPhone 健康 App 里设置好睡眠计划（开启"用 Apple Watch 追踪睡眠"），才会有睡眠分期和夜间呼吸率。

## 写入的数据

| measurement | tags | fields | 时间 |
|---|---|---|---|
| `apple_health` | person, metric, unit, source | `qty`，心率为 `min/avg/max`，血压为 `systolic/diastolic` | 样本时间 |
| `apple_sleep_stage` | person, stage (Core/Deep/REM/Awake/In Bed…), source | `duration_h`, `end_ts` | 分段开始 |
| `apple_sleep_daily` | person, source | `total_sleep`, `deep`, `rem`, `core`, `in_bed` (小时) | 入睡时间 |
| `apple_workout` | person, name | `duration_s` 及各项汇总数值 | 开始时间 |

常用的 metric：`heart_rate`、`resting_heart_rate`、`heart_rate_variability`、
`respiratory_rate`、`blood_oxygen_saturation`。第一次同步后可用 `schema.tagValues(bucket: "sensing", tag: "metric")` 查实际有哪些。

Flux 示例（某人夜间心率）：

```flux
from(bucket: "sensing")
  |> range(start: -1d)
  |> filter(fn: (r) => r._measurement == "apple_health" and r.metric == "heart_rate" and r.person == "peter" and r._field == "avg")
```
