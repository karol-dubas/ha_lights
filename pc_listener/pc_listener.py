"""MQTT listener that adjusts monitor brightness and contrast via DDC/CI.

Subscribes to (Home Assistant's) MQTT topics, converts percentage values
to monitor-specific ranges, and applies them using monitorcontrol.
Designed to run continuously as a startup script.
"""

import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any

import numpy as np
import paho.mqtt.client as mqtt
import yaml
from dotenv import load_dotenv
from monitorcontrol import get_monitors
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from win_nightlight import NightLight

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
TOPIC_COLOR_TEMP = "homeassistant/light/color_temp_pct"
TOPIC_REFRESH = "homeassistant/light/refresh"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.yaml")

night_light = NightLight()
if not night_light.enabled():
    night_light.enable()


@dataclass(frozen=True)
class ValueRange:
    min: int
    max: int
    power: float = 1.0


@dataclass(frozen=True)
class MonitorConfig:
    name: str
    brightness: ValueRange
    contrast: ValueRange


@dataclass(frozen=True)
class GlobalConfig:
    monitors: list[MonitorConfig]
    color_temp: ValueRange


def load_config() -> GlobalConfig:
    """Load monitor configurations from config.yaml."""
    with open(CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    configs = []
    for m in data.get("monitors", []):
        configs.append(
            MonitorConfig(
                name=m.get("name", "Unknown"),
                brightness=ValueRange(**m["brightness"]),
                contrast=ValueRange(**m["contrast"]),
            )
        )

    ct_data = data.get("color_temp", {"min": 0, "max": 100, "power": 1.0})
    ct_cfg = ValueRange(**ct_data)

    log.info("Loaded config for %d monitor(s) and global color temp", len(configs))
    return GlobalConfig(monitors=configs, color_temp=ct_cfg)


# Global mutable config (reloaded on file change)
app_config: GlobalConfig = None
last_values: dict[str, Any] = {}
_config_lock = threading.Lock()


def reload_config(client: mqtt.Client = None) -> None:
    """Reload config from YAML file."""
    global app_config
    try:
        new = load_config()
        with _config_lock:
            app_config = new

        if client and client.is_connected():
            client.publish(TOPIC_REFRESH)
            log.info("Refresh request sent after config reload")
    except Exception:
        log.exception("Failed to reload config, keeping previous values")


class ConfigFileHandler(FileSystemEventHandler):
    """Watches for changes to config.yaml and reloads it."""

    def __init__(self, client: mqtt.Client):
        self.client = client

    def on_modified(self, event):
        if os.path.basename(event.src_path) == "config.yaml":
            log.info("config.yaml changed, reloading...")
            reload_config(self.client)


def percent_to_monitor_value(
    min_val: int, max_val: int, level: int, power: float, invert: bool = False
) -> int:
    """Map a 0-100 percentage to a monitor-specific [min_val, max_val] range."""
    normalized = level / 100.0
    curved = np.power(normalized, power)
    if invert:
        scaled_value = max_val - (curved * (max_val - min_val))
    else:
        scaled_value = min_val + (curved * (max_val - min_val))
    return int(round(scaled_value))


def apply_color_temperature(level: int) -> None:
    """Set Windows Night Light strength based on percentage."""
    with _config_lock:
        if not app_config:
            return
        ct_cfg = app_config.color_temp

    strength_raw = percent_to_monitor_value(
        ct_cfg.min, ct_cfg.max, level, ct_cfg.power, invert=True
    )
    strength_dst = max(ct_cfg.min, min(ct_cfg.max, strength_raw))

    try:
        if last_values.get("color_temp") != strength_dst:
            night_light.set_strength(strength_dst)
            last_values["color_temp"] = strength_dst
            log.info(
                "Color temp %d%% -> %d%%",
                level,
                strength_dst,
            )
    except Exception:
        log.exception(
            "Failed to set color temp %d%% -> %d%%",
            level,
            strength_dst,
        )


def apply_brightness_contrast(light_level: int) -> None:
    """Set brightness and contrast on all connected monitors."""
    with _config_lock:
        if not app_config:
            return
        configs = list(app_config.monitors)

    monitors = get_monitors()

    for i, monitor in enumerate(monitors):
        # Use per-monitor config if available, otherwise fall back to the last one
        cfg = configs[min(i, len(configs) - 1)]

        brightness_raw = percent_to_monitor_value(
            cfg.brightness.min, cfg.brightness.max, light_level, cfg.brightness.power
        )
        brightness_dst = max(
            cfg.brightness.min, min(cfg.brightness.max, brightness_raw)
        )

        contrast_raw = percent_to_monitor_value(
            cfg.contrast.min, cfg.contrast.max, light_level, cfg.contrast.power
        )
        contrast_dst = max(cfg.contrast.min, min(cfg.contrast.max, contrast_raw))

        try:
            with monitor:
                changed = False
                if last_values.get(f"mon_{i}_brightness") != brightness_dst:
                    monitor.set_luminance(brightness_dst)
                    last_values[f"mon_{i}_brightness"] = brightness_dst
                    changed = True

                if last_values.get(f"mon_{i}_contrast") != contrast_dst:
                    monitor.set_contrast(contrast_dst)
                    last_values[f"mon_{i}_contrast"] = contrast_dst
                    changed = True

            if changed:
                log.info(
                    "[%s] Brightness %d%% -> %d%%, Contrast %d%% -> %d%%",
                    cfg.name,
                    light_level,
                    brightness_dst,
                    light_level,
                    contrast_dst,
                )
        except Exception:
            log.exception(
                "Failed to set monitor %d (%s): brightness=%d, contrast=%d",
                i,
                cfg.name,
                brightness_dst,
                contrast_dst,
            )


def on_connect(client: mqtt.Client, _userdata, _flags, reason_code, _properties):
    if reason_code == 0:
        log.info("Connected to MQTT broker")
    else:
        log.warning("Connection returned code %s", reason_code)
        return

    client.subscribe(TOPIC_BRIGHTNESS)
    client.subscribe(TOPIC_COLOR_TEMP)
    log.info("Subscribed to %s, %s", TOPIC_BRIGHTNESS, TOPIC_COLOR_TEMP)

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
        apply_brightness_contrast(value)
    elif msg.topic == TOPIC_COLOR_TEMP:
        apply_color_temperature(value)
    else:
        log.debug("Ignored topic %s", msg.topic)


def main():
    # Load config on startup
    reload_config()

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
    delay = 1
    while True:
        try:
            log.info("Connecting to %s ...", host)
            client.connect(host)
            break
        except OSError:
            log.warning("Connection failed, retrying in %ds...", delay)
            import time

            time.sleep(delay)
            delay = min(delay := delay + 1, 60)

    # Watch config.yaml for live changes (started after client so we can refresh)
    observer = Observer()
    observer.schedule(ConfigFileHandler(client), path=SCRIPT_DIR, recursive=False)
    observer.start()

    # Graceful shutdown on Ctrl+C or system signal.
    def shutdown(_sig, _frame):
        log.info("Shutting down...")
        observer.stop()
        client.disconnect()
        client.loop_stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Blocks forever; handles reconnects automatically.
    client.loop_forever()


if __name__ == "__main__":
    main()
