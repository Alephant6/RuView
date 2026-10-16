"""User-facing strings (Home Assistant states and push notifications)."""

from __future__ import annotations

MESSAGES: dict[str, dict[str, str]] = {
    "ok": {
        "zh": "一切正常",
        "en": "All good",
    },
    "bridge_offline": {
        "zh": "数据桥（mqtt-bridge）已离线",
        "en": "The MQTT bridge is offline",
    },
    "bridge_silent": {
        "zh": "已有 {age} 没收到数据桥（mqtt-bridge）的消息",
        "en": "No message from the MQTT bridge for {age}",
    },
    "bridge_never": {
        "zh": "看门狗启动后一直没收到数据桥（mqtt-bridge）的消息",
        "en": "No message from the MQTT bridge since the watchdog started",
    },
    "bridge_ws_down": {
        "zh": "数据桥连不上感知服务器的 WebSocket",
        "en": "The MQTT bridge cannot reach the sensing-server WebSocket",
    },
    "server_down": {
        "zh": "感知服务器连不上：{error}",
        "en": "Sensing server unreachable: {error}",
    },
    "api_auth": {
        "zh": "感知服务器拒绝了看门狗的 API token，节点和标定无法检查",
        "en": "The sensing server rejected the API token; nodes and calibration cannot be checked",
    },
    "source_offline": {
        "zh": "CSI 数据源离线（{source}），服务器只在重发旧数据",
        "en": "CSI source offline ({source}); the server is only re-sending old data",
    },
    "source_state": {
        "zh": "CSI 数据源状态异常：{state}",
        "en": "CSI source state is {state}",
    },
    "source_stalled": {
        "zh": "已有 {age} 没有新的 CSI 数据，服务器只在重发旧数据",
        "en": "No new CSI data for {age}; the server is only re-sending old data",
    },
    "node_down": {
        "zh": "节点 {node} 掉线{age}",
        "en": "Node {node} is down{age}",
    },
    "node_slow": {
        "zh": "节点 {node} 帧率只有 {fps} fps（正常约 20）",
        "en": "Node {node} is at {fps} fps (normally about 20)",
    },
    "vitals_frozen": {
        "zh": "{metric}过去 {minutes} 分钟只有 {count} 个不同的值（{values}），疑似冻结",
        "en": "{metric} had only {count} distinct value(s) in {minutes} min ({values}); looks frozen",
    },
    "vitals_range": {
        "zh": "{metric}有 {pct}% 的读数超出 {lo}–{hi}",
        "en": "{pct}% of {metric} readings are outside {lo}-{hi}",
    },
    "calibration_stale": {
        "zh": "空房标定已超过 12 小时，满 24 小时后 CSI 心率呼吸会停发",
        "en": "Empty-room calibration is over 12 h old; CSI vitals stop at 24 h",
    },
    "calibration_missing": {
        "zh": "没有有效的空房标定（{status}），CSI 心率呼吸不会发布",
        "en": "No valid empty-room calibration ({status}); CSI vitals are withheld",
    },
    "calibration_bootstrap": {
        "zh": "只有重启时恢复的基线，没有新的空房标定，CSI 心率呼吸不会发布",
        "en": "Only a restored bootstrap baseline, no explicit calibration; CSI vitals are withheld",
    },
    "presence_contradiction": {
        "zh": "所有手机都离家 {away}，但 CSI 过去 {minutes} 分钟有 {pct}% 的时间判断有人",
        "en": "All phones away for {away}, yet CSI saw someone {pct}% of the last {minutes} min",
    },
    "suppressed": {
        "zh": "数据源离线，暂不单独判断",
        "en": "Not evaluated while the source is down",
    },
    "metric_hr": {"zh": "心率", "en": "heart rate"},
    "metric_br": {"zh": "呼吸率", "en": "breathing rate"},
    "vitals_published": {"zh": "正在发布", "en": "published"},
    "vitals_withheld": {"zh": "未发布", "en": "withheld"},
    "vitals_unknown": {"zh": "未知", "en": "unknown"},
}

ABSTENTION_REASONS: dict[str, dict[str, str]] = {
    "bootstrap_empty_background": {"zh": "只有重启恢复的基线", "en": "bootstrap baseline only"},
    "explicit_calibration_required": {"zh": "需要空房标定", "en": "needs empty-room calibration"},
    "exactly_one_occupant_required": {"zh": "房间里不是恰好一个人", "en": "not exactly one person"},
    "vital_quality_gate_failed": {"zh": "信号质量不够", "en": "signal quality too low"},
}


def t(lang: str, key: str, **kwargs: object) -> str:
    entry = MESSAGES[key]
    template = entry.get(lang) or entry["en"]
    return template.format(**kwargs)


def reason(lang: str, code: str | None) -> str | None:
    if not code:
        return None
    entry = ABSTENTION_REASONS.get(code)
    return (entry.get(lang) or entry["en"]) if entry else code


def duration(lang: str, seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} 秒" if lang == "zh" else f"{seconds} s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes} 分钟" if lang == "zh" else f"{minutes} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} 小时" if lang == "zh" else f"{hours:.1f} h"
    return f"{hours / 24:.1f} 天" if lang == "zh" else f"{hours / 24:.1f} days"
