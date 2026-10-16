# health-ingest：Apple Watch → InfluxDB（WP0.3）

每个人的 iPhone 每天用 **快捷指令**（iOS 自带，免费）把最近两天的健康数据
POST 到 NAS 上的这个服务，写进 InfluxDB 的 `sensing` bucket，和 CSI 数据在同一条时间轴上。
用哪个 token 推送就决定了数据属于谁（`person` 标签），所以每人一个 token。
历史数据用健康 App 的"导出所有健康数据"一次性回填。
（也兼容付费 App Health Auto Export 的 JSON 推送，`POST /`，目前不用。）

## NAS 端部署

1. 给每人生成一个 token（至少 16 位）：
   `python3 -c "import secrets;print(secrets.token_urlsafe(24))"`
2. 在 `docker/.env` 里加一行（名字和 RuView 的 profile 名对应）：
   `HEALTH_TOKENS=sq:<token1>,ml:<token2>`
3. 启动：
   `docker compose -f docker-compose.yml -f docker-compose.health.yml up -d --build health-ingest`
4. 检查：`curl http://192.168.1.195:8095/health` 应返回 `{"ok": true}`。

## iPhone 端：快捷指令（每人在自己手机上做一次）

先建一个快捷指令，名字叫"健康同步"：

1. 添加 **"查找健康样本"**：类型选 **心率**；条件"开始日期 在最近 2 天"；排序"开始日期"；限制关闭。
2. 添加 **"重复每一项"**，对象是上一步的"健康样本"。在循环里面添加一个 **"文本"**，内容（`|` 是竖线，变量都点"重复项目"后选属性）：
   `heart_rate|【开始日期】|【结束日期】|【值】|count/min|【来源】`
   其中两个日期变量点开后把"日期格式"设成 **ISO 8601**，并打开"包括时间"。
3. "结束重复"之后添加 **"合并文本"**，用"新行"合并"重复结果"；再添加 **"添加到变量"**，变量名 `全部`。
4. 把第 1–3 步整段复制几份（长按动作 → 复制），每份只改两处：查找的类型，和文本开头的英文名、单位。

   | 健康样本类型 | 文本开头 | 单位 |
   |---|---|---|
   | 心率 | `heart_rate` | `count/min` |
   | 静息心率 | `resting_heart_rate` | `count/min` |
   | 心率变异性 | `heart_rate_variability` | `ms` |
   | 血氧饱和度 | `blood_oxygen_saturation` | `%` |
   | 呼吸速率 | `respiratory_rate` | `count/min` |
   | 睡眠分析 | `sleep_analysis` | `hr` |

5. 最后添加 **"合并文本"**（用"新行"合并变量 `全部`），再添加 **"获取 URL 内容"**：
   - URL：`http://192.168.1.195:8095/shortcut`
   - 方法：POST
   - 头部：`Authorization` = `Bearer <自己的 token>`
   - 请求体：**文件**，选上一步的"合并文本"
6. 手动运行一次。成功时会返回 `{"ok": true, "person": "...", "points": ...}`。

然后设成每天自动运行：快捷指令 → 自动化 → 新建个人自动化 → **特定时间**，选一个你通常在用手机的时间
（比如早上 9 点），选"立即运行"，动作选"运行快捷指令：健康同步"。
iPhone 锁着时 iOS 不让读健康数据，那次就会失败，下次运行会补上（每次取最近 2 天，重复上传不会产生重复数据）。

第二个人可以直接用 iCloud 共享这个快捷指令，拿到后只改 token。
睡觉要戴手表，并在健康 App 里开启睡眠追踪，才有睡眠分期。

## 历史数据回填（每人一次）

1. iPhone 健康 App → 右上角头像 → **导出所有健康数据**，得到 `导出.zip`（可能有几百 MB），传到 NAS，比如 `/volume1/docker/RuView/health-import/sq.zip`，并执行 `chmod a+r` 让容器能读（容器以无特权用户运行）。
2. 先试运行看看解析结果，再正式导入（默认只导入心率、HRV、血氧、呼吸、体温和睡眠；`--types all` 导入全部）：

```bash
cd /volume1/docker/RuView/docker
docker compose -f docker-compose.yml -f docker-compose.health.yml run --rm \
  -v /volume1/docker/RuView/health-import:/import:ro --entrypoint python health-ingest \
  /app/import_export.py --person sq /import/sq.zip --dry-run
# 确认没问题后去掉 --dry-run 再跑一次；ml 同理
```

## 写入的数据

| measurement | tags | fields | 时间 |
|---|---|---|---|
| `apple_health` | person, metric, unit, source | `qty`（HAE 汇总模式下心率为 `min/avg/max`） | 样本时间 |
| `apple_sleep_stage` | person, stage (Core/Deep/REM/Awake/In Bed…), source | `duration_h`, `end_ts` | 分段开始 |
| `apple_sleep_daily` | person, source | `total_sleep`, `deep`, `rem`, `core`, `in_bed` (小时) | 入睡时间 |
| `apple_workout` | person, name | `duration_s` 及各项汇总数值 | 开始时间 |

常用的 metric：`heart_rate`、`resting_heart_rate`、`heart_rate_variability`、
`respiratory_rate`、`blood_oxygen_saturation`。第一次同步后可用 `schema.tagValues(bucket: "sensing", tag: "metric")` 查实际有哪些。

Flux 示例（某人夜间心率）：

```flux
from(bucket: "sensing")
  |> range(start: -1d)
  |> filter(fn: (r) => r._measurement == "apple_health" and r.metric == "heart_rate" and r.person == "sq" and r._field == "avg")
```
