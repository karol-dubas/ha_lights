"""MQTT listener that adjusts monitor brightness and contrast via DDC/CI.

Subscribes to (Home Assistant's) MQTT topics, converts percentage values
to monitor-specific ranges, and applies them using monitorcontrol.
Designed to run continuously as a startup script.
"""

import logging
import os
import signal
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from monitorcontrol import get_monitors

load_dotenv()

# Log directory should be the same as the script directory
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pc_listener.log")

# Setup logging to both console (for debug) and file (for Task Scheduler)
log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

# File handler (1MB max, keep 5 backups)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5)
file_handler.setFormatter(log_formatter)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
log = logging.getLogger(__name__)

TOPIC_BRIGHTNESS = "homeassistant/light/brightness_pct"
# TOPIC_COLOR_TEMP = "homeassistant/light/color_temp_k"
TOPIC_REFRESH = "homeassistant/light/refresh"


@dataclass(frozen=True)
class ValueRange:
    min: int
    max: int
    offset: int = 0


@dataclass(frozen=True)
class MonitorConfig:
    brightness: ValueRange
    contrast: ValueRange


MONITOR_CONFIG = MonitorConfig(
    brightness=ValueRange(min=3, max=100),
    contrast=ValueRange(min=60, max=92),
)


def percent_to_monitor_value(min_val: int, max_val: int, percent: int) -> int:
    """Map a 0-100 percentage to a monitor-specific [min_val, max_val] range."""
    value = (max_val - min_val) * (percent / 100)
    return min_val + round(value)


def apply_brightness(percent: int) -> None:
    """Set brightness on all connected monitors."""
    cfg = MONITOR_CONFIG.brightness
    raw = percent_to_monitor_value(cfg.min, cfg.max, percent) + cfg.offset
    raw = max(cfg.min, min(cfg.max, raw))

    try:
        for monitor in get_monitors():
            with monitor:
                monitor.set_luminance(raw)
        log.info("Brightness set to %d%% (converted to %d%%)", percent, raw)
    except Exception:
        log.exception("Failed to set brightness")


def apply_contrast(percent: int) -> None:
    """Set contrast on all connected monitors."""
    cfg = MONITOR_CONFIG.contrast
    raw = percent_to_monitor_value(cfg.min, cfg.max, percent) + cfg.offset
    raw = max(cfg.min, min(cfg.max, raw))  # clamp

    try:
        for monitor in get_monitors():
            with monitor:
                monitor.set_contrast(raw)
        log.info("Contrast set to %d%% (converted to %d%%)", percent, raw)
    except Exception:
        log.exception("Failed to set contrast")


def on_connect(client: mqtt.Client, _userdata, _flags, reason_code, _properties):
    if reason_code == 0:
        log.info("Connected to MQTT broker")
    else:
        log.warning("Connection returned code %s", reason_code)
        return

    client.subscribe(TOPIC_BRIGHTNESS)
    log.info("Subscribed to %s", TOPIC_BRIGHTNESS)

    # Ask for current values right away (useful after boot / wake).
    client.publish(TOPIC_REFRESH)
    log.info("Refresh request sent")


def on_disconnect(client: mqtt.Client, _userdata, _flags, reason_code, _properties):
    if reason_code == 0:
        log.info("Disconnected cleanly")
    else:
        log.warning("Unexpected disconnect (code %s), will reconnect...", reason_code)


def on_message(_client: mqtt.Client, _userdata, msg: mqtt.MQTTMessage):
    try:
        value = int(msg.payload.decode())
    except (ValueError, UnicodeDecodeError):
        log.warning("Invalid payload on %s: %r", msg.topic, msg.payload)
        return

    if msg.topic == TOPIC_BRIGHTNESS:
        apply_brightness(value)
        apply_contrast(value)
    else:
        log.debug("Ignored topic %s", msg.topic)


def main() -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    client.username_pw_set(
        username=os.environ["HA_MQTT_Username"],
        password=os.environ["HA_MQTT_Password"],
    )

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # Exponential backoff: 1 s -> 60 s
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    host = os.environ["HA_MQTT_Address"]
    log.info("Connecting to %s ...", host)
    client.connect(host)

    # Graceful shutdown on Ctrl+C or system signal.
    def shutdown(_sig, _frame):
        log.info("Shutting down...")
        client.disconnect()
        client.loop_stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Blocks forever; handles reconnects automatically.
    client.loop_forever()


if __name__ == "__main__":
    main()
