"""Local bridge from the XPENG iOS/macOS app's own on-disk data to MQTT.

Read-only by construction: nothing here writes to the app's container, and no
request is ever made to XPENG.
"""

__version__ = "1.0.0"
