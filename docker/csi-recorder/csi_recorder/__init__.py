"""RuView raw CSI recorder (WP0.5).

Records every datagram the sensing-server copies to its loopback UDP tee
(ADR-380) into hourly gzip files, keeps the last 72 hours, and copies
requested time ranges (ground-truth sessions, anomalies) into permanent
clips.
"""

__version__ = "0.1.0"
