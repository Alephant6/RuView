"""Apple Health points for the backup test (.github/workflows/backup.yml).

Line protocol in the shape docker/health-ingest writes: every measurement the
backup exports, numbers as floats, text fields and tags that need escaping.
The points lie in the last five days, inside the bucket's retention.

    python3 health_samples.py > health.lp
"""

import time

# A device name as an iPhone reports it, escaped as a tag value.
WATCH = "Alex’s\\ Apple\\ Watch"


def main() -> None:
    now = int(time.time())
    lines = []
    for i in range(240):  # every 30 minutes
        t = now - 3600 - 1800 * i
        lines.append(f"apple_health,metric=heart_rate,person=sq,source={WATCH},unit=count/min "
                     f"min={50 + i % 7}.0,avg={58 + i % 23 + 0.25},max={70 + i % 17}.0 {t}")
        lines.append(f"apple_health,metric=step_count,person=ml,source=iPhone,unit=count "
                     f"qty={i * 37 % 900}.0 {t}")
    for night in range(5):
        start = now - 86400 * (night + 1) - 4 * 3600
        for k, stage in enumerate(("Core", "Deep", "REM", "Awake")):
            lines.append(f"apple_sleep_stage,person=sq,source={WATCH},stage={stage} "
                         f"duration_h={0.5 + k * 0.25},end_ts={start + 1800 * (k + 1)}.0 {start + 1800 * k}")
        lines.append(f"apple_sleep_daily,person=sq,source={WATCH} asleep={6.5 + night / 10},"
                     f"in_bed={7.25 + night / 10},sleep_end_ts={start + 8 * 3600}.0 {start}")
    lines.append(f"apple_workout,name=Outdoor\\ Walk,person=ml "
                 f"duration_s=1800.0,active_energy=123.4,end_ts={now - 7200}.0 {now - 9000}")
    lines.append(f'apple_health,metric=state_of_mind,person=sq,source=iPhone,unit=none '
                 f'value="calm, \\"rested\\", 平静" {now - 5000}')
    print("\n".join(lines))


if __name__ == "__main__":
    main()
