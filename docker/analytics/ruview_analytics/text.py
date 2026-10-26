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
    "calibration_fault": {
        "zh": "节点 {nodes} 的 CSI 序号倒退过（多半是重启了），这次标定已不能放行 CSI 心率呼吸",
        "en": "The CSI sequence of node {nodes} went backwards (usually a reboot); this calibration no longer authorizes CSI vitals",
    },
    "calibration_auto_hint": {
        "zh": "。家里没人时会自动重新标定",
        "en": ". It recalibrates automatically when nobody is home",
    },
    "presence_contradiction": {
        "zh": "所有手机都离家 {away}，但 CSI 过去 {minutes} 分钟有 {pct}% 的时间判断有人",
        "en": "All phones away for {away}, yet CSI saw someone {pct}% of the last {minutes} min",
    },
    "recorder_offline": {
        "zh": "原始 CSI 录制器（csi-recorder）已离线",
        "en": "The raw CSI recorder (csi-recorder) is offline",
    },
    "recorder_silent": {
        "zh": "已有 {age} 没收到原始 CSI 录制器的状态",
        "en": "No status from the raw CSI recorder for {age}",
    },
    "recorder_disk_low": {
        "zh": "数据盘只剩 {free} GB，原始 CSI 录制已暂停",
        "en": "Only {free} GB free on the data volume; raw CSI recording is paused",
    },
    "recorder_write": {
        "zh": "录制器收到数据但写不进磁盘：{error}",
        "en": "The recorder receives data but cannot write it: {error}",
    },
    "recorder_no_data": {
        "zh": "节点在发数据，但录制器已有 {age} 没收到数据包（检查 sensing-server 的 RUVIEW_UDP_TEE）",
        "en": "Nodes are sending, but the recorder has had no datagram for {age} (check RUVIEW_UDP_TEE on the sensing-server)",
    },
    "recorder_no_data_never": {
        "zh": "节点在发数据，但录制器一个数据包都没收到（检查 sensing-server 的 RUVIEW_UDP_TEE）",
        "en": "Nodes are sending, but the recorder has not received a single datagram (check RUVIEW_UDP_TEE on the sensing-server)",
    },
    "backup_old": {
        "zh": "距上次成功的异地备份已有 {age}",
        "en": "The last successful offsite backup was {age} ago",
    },
    "backup_never": {
        "zh": "异地备份服务已运行 {age}，还没有成功过一次",
        "en": "The offsite backup service has run for {age} without a single success",
    },
    "backup_error": {
        "zh": "（最近一次在 {step} 这一步出错：{error}）",
        "en": " (the last run failed at {step}: {error})",
    },
    "autocal_wait_server": {"zh": "感知服务器连不上", "en": "sensing server unreachable"},
    "autocal_wait_old_server": {
        "zh": "感知服务器版本太旧，不支持自动标定",
        "en": "the sensing server is too old for automatic calibration",
    },
    "autocal_wait_phones_unknown": {"zh": "不知道手机在不在家", "en": "phone locations unknown"},
    "autocal_wait_home": {"zh": "{who} 在家", "en": "{who} at home"},
    "autocal_wait_away": {"zh": "离家才 {away}，要满 {need}", "en": "away for {away} of {need}"},
    "autocal_wait_no_state": {"zh": "CSI 状态数据不够", "en": "not enough CSI state data"},
    "autocal_wait_moving": {
        "zh": "房间里有动静（{pct}% 的时间在动）",
        "en": "movement in the room ({pct}% of the time)",
    },
    "autocal_wait_node": {"zh": "节点 {node} 没在正常发数据", "en": "node {node} is not streaming"},
    "autocal_wait_retry": {"zh": "上次{result}，{left}后再试", "en": "the last capture {result}; next try in {left}"},
    "autocal_wait_manual": {"zh": "有一个手动标定正在进行", "en": "a manual calibration is running"},
    "autocal_wait_bootstrap": {
        "zh": "重置会删掉服务器上保存的启动基线，等这次标定过期（满 12 小时）再重新标定",
        "en": "a reset would delete the stored startup baseline; recalibrating once this one is stale (12 h)",
    },
    "autocal_result_cancelled": {"zh": "被取消", "en": "was cancelled"},
    "autocal_result_failed": {"zh": "失败", "en": "failed"},
    "autocal_started": {
        "zh": "自动空房标定开始（节点 {node}）",
        "en": "Automatic empty-room calibration started (node {node})",
    },
    "autocal_reset_age": {
        "zh": "；先清除了 {age}前的旧标定",
        "en": "; cleared the previous calibration from {age} ago",
    },
    "autocal_reset_fault": {
        "zh": "；先清除了序号出错的旧标定",
        "en": "; cleared the previous calibration after a CSI sequence fault",
    },
    "autocal_done": {
        "zh": "自动空房标定完成：{minutes} 分钟，{frames} 帧。接下来 12 小时，房间里恰好一个人时会发布 CSI 心率呼吸",
        "en": "Automatic calibration done: {minutes} min, {frames} frames. For the next 12 h, CSI vitals are published while exactly one person is in the room",
    },
    "autocal_cancelled": {"zh": "自动空房标定已取消：{reason}", "en": "Automatic calibration cancelled: {reason}"},
    "autocal_failed": {"zh": "自动空房标定失败：{reason}", "en": "Automatic calibration failed: {reason}"},
    "autocal_failed_after_reset": {
        "zh": "旧标定已清除，但新的标定没能开始：{reason}",
        "en": "The previous calibration was cleared, but a new one could not start: {reason}",
    },
    "autocal_reason_home": {"zh": "{who} 回家了", "en": "{who} came home"},
    "autocal_reason_moving": {
        "zh": "房间里有动静（最近 2 分钟 {pct}% 的时间在动）",
        "en": "movement in the room ({pct}% of the last 2 min)",
    },
    "autocal_reason_fault": {"zh": "节点 {nodes} 的 CSI 序号出错", "en": "CSI sequence fault on node {nodes}"},
    "autocal_reason_timeout": {"zh": "{minutes} 分钟还没采完", "en": "not finished after {minutes} min"},
    "autocal_reason_no_frames": {"zh": "节点 {node} 一直没有数据进来", "en": "no frames from node {node}"},
    "autocal_reason_restarted": {"zh": "感知服务器重启了", "en": "the sensing server restarted"},
    "autocal_reason_lost": {
        "zh": "标定在别处被停止或替换了",
        "en": "the capture was stopped or replaced elsewhere",
    },
    "autocal_reason_refused": {"zh": "服务器拒绝：{error}", "en": "the server refused: {error}"},
    "autocal_state_ok": {"zh": "标定有效", "en": "calibration fresh"},
    "autocal_state_ok_age": {"zh": "标定有效（{age}前）", "en": "calibration fresh ({age} old)"},
    "autocal_state_collecting": {"zh": "采集中 {done}/{need} 分钟", "en": "collecting {done}/{need} min"},
    "autocal_state_waiting": {"zh": "需要标定，等待：{why}", "en": "due, waiting: {why}"},
    "autocal_state_unknown": {"zh": "状态未知：{why}", "en": "unknown: {why}"},
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
