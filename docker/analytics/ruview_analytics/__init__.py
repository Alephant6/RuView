"""RuView analytics service.

v0 runs the data watchdog: it watches the MQTT stream and the
sensing-server REST API, and publishes problems to Home Assistant. With
AUTOCAL_SOURCE_NODE set it also renews the empty-room calibration while
nobody is home (autocal.py).
"""

__version__ = "0.2.0"
