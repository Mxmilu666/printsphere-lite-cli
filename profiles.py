"""Configuration persistence and isolated ESP profiles; no hardware access."""

import copy
import json
import math
import os
import re
import tempfile
import time


BACKEND_VERSION = "cli-v0.4.71"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
HISTORY_FILE = os.path.join(DATA_DIR, "device-history.jsonl")
DEFAULT_SERIAL_PORT = "COM7" if os.name == "nt" else ""
DEFAULT_CONFIG = {
    "wifi": {"ssid": "", "password": ""},
    "cloud": {
        "region": "cn", "account": "", "access_token": "",
        "mqtt_username": "", "token_expires_at": 0,
    },
    "printer": {"serial": "", "access_code": "", "display_name": "", "model": ""},
    "display": {
        "brightness": 100, "layout": "classic", "alias": "",
        "alias_bitmap_hex": "", "alias_bitmap_w": 0, "alias_bitmap_h": 0,
    },
    "esp": {
        "host": "", "port": 8081, "serial_port": DEFAULT_SERIAL_PORT,
        "wifi_status": "unknown", "last_ip": "", "last_checked_at": 0,
        "last_error": "",
    },
    "defaults": {"wifi": {"ssid": "", "password": ""}},
    "active_esp_id": "default",
    "esp_devices": {},
}


def ensure_data():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        write_json(CONFIG_FILE, DEFAULT_CONFIG)
    if not os.path.exists(DEVICES_FILE):
        write_json(DEVICES_FILE, {"updated_at": 0, "devices": []})


def read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError, UnicodeError):
        return copy.deepcopy(fallback)


def write_json(path, value):
    """Replace atomically in the destination directory, retaining old data on error."""
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def merge_config(base, extra):
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in (extra.items() if isinstance(extra, dict) else ()):
        out[key] = merge_config(out.get(key), value) if isinstance(value, dict) else copy.deepcopy(value)
    return out


def normalize_brightness(value):
    # Number(null), Number("") and Number(false) are zero in the backend.
    try:
        if value is None or isinstance(value, str) and not value.strip():
            level = 0.0
        elif isinstance(value, str) and re.match(r"^0[xob]", value.strip(), re.I):
            level = float(int(value.strip(), 0))
        else:
            level = float(value)
    except (TypeError, ValueError, OverflowError):
        return 100
    if not math.isfinite(level):
        return 100
    return math.floor(max(0.0, min(100.0, level)) + 0.5)


def normalize_layout(value):
    return "dashboard" if str(value or "").lower() == "dashboard" else "classic"


def normalize_alias(value):
    # Python slices codepoints, so supplementary Unicode characters stay intact.
    return str(value or "").strip()[:16]


def normalize_serial_port(port):
    raw = str(port or "").strip()
    if not raw:
        return DEFAULT_SERIAL_PORT
    return raw.upper() if re.fullmatch(r"com\d+", raw, re.I) else raw


def is_private_ipv4(address):
    parts = str(address or "").split(".")
    if len(parts) != 4 or any(not re.fullmatch(r"[0-9]{1,3}", p) for p in parts):
        return False
    numbers = [int(p) for p in parts]
    if any(n > 255 for n in numbers):
        return False
    return numbers[0] == 10 or numbers[0] == 172 and 16 <= numbers[1] <= 31 or numbers[:2] == [192, 168]


def normalize_esp_host(host):
    value = str(host or "").strip()
    if not value or re.fullmatch(r"\(.*\)", value):
        return ""
    value = re.sub(r"^https?://", "", value, flags=re.I).split("/")[0].split("?")[0].strip()
    value = value.rsplit("@", 1)[-1]
    if value.startswith("[") and "]" in value:
        value = value[1:value.index("]")]
    elif value.count(":") == 1:
        value = value.split(":")[0]
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", value):
        return value if is_private_ipv4(value) else ""
    return value if re.fullmatch(r"[a-z0-9.-]+", value, re.I) and "." in value else ""


def default_esp_profile(id="default"):
    return {
        "id": id, "label": "默认 ESP" if id == "default" else id,
        "chip_id": "", "mac": "", "device_id": id, "last_seen_at": 0,
        **{key: copy.deepcopy(DEFAULT_CONFIG[key]) for key in ("wifi", "printer", "display", "esp")},
    }


def normalize_esp_id(value):
    return re.sub(r"[^a-z0-9_-]", "", str(value or "").strip().lower()) or "default"


def esp_id_from_status(status):
    status = status if isinstance(status, dict) else {}
    for field in ("device_id", "mac", "chip_id"):
        identity = normalize_esp_id(status.get(field))
        if identity != "default":
            return identity if field == "device_id" else "esp-" + identity
    return "default"


def esp_label(profile):
    if not profile:
        return "未选择 ESP"
    esp = profile.get("esp") or {}
    printer = profile.get("printer") or {}
    state = "离线" if esp.get("last_error") else "已识别" if profile.get("last_seen_at") else ""
    parts = [esp.get("serial_port"), profile.get("label"), printer.get("display_name") or printer.get("serial"), state]
    return " / ".join(str(p) for p in parts if p) or profile.get("id") or "ESP"


def _ensure_defaults(cfg):
    cfg["defaults"] = merge_config(DEFAULT_CONFIG["defaults"], cfg.get("defaults"))
    wifi = merge_config(DEFAULT_CONFIG["wifi"], cfg.get("wifi"))
    for key in ("ssid", "password"):
        if not cfg["defaults"]["wifi"].get(key) and wifi.get(key):
            cfg["defaults"]["wifi"][key] = wifi[key]


def _normalize_profile(profile, identity):
    profile = merge_config(default_esp_profile(identity), profile)
    profile["id"] = identity
    profile["device_id"] = normalize_esp_id(profile.get("device_id") or identity)
    for key in ("wifi", "printer", "display", "esp"):
        profile[key] = merge_config(DEFAULT_CONFIG[key], profile.get(key))
    display = profile["display"]
    for obsolete in ("active_brightness", "dimmed", "brightness_active", "brightness_dimmed"):
        display.pop(obsolete, None)
    display["brightness"] = normalize_brightness(display["brightness"])
    display["layout"] = normalize_layout(display["layout"])
    display["alias"] = normalize_alias(display["alias"])
    esp = profile["esp"]
    esp["serial_port"] = normalize_serial_port(esp["serial_port"])
    try:
        port = int(esp.get("port") or 8081)
    except (TypeError, ValueError, OverflowError):
        port = 8081
    esp["port"] = 8081 if port in (0, 80, 8080) else port
    esp["host"] = normalize_esp_host(esp["host"])
    esp["last_ip"] = normalize_esp_host(esp["last_ip"])
    return profile


def _restore_profile(cfg, profile):
    for key in ("wifi", "printer", "display", "esp"):
        cfg[key] = copy.deepcopy(profile[key])
    return cfg


def ensure_esp_profiles(cfg):
    _ensure_defaults(cfg)
    profiles = cfg.get("esp_devices")
    profiles = profiles if isinstance(profiles, dict) else {}
    profiles = {normalize_esp_id(k): _normalize_profile(v, normalize_esp_id(k)) for k, v in profiles.items()}
    identity = normalize_esp_id(cfg.get("active_esp_id"))
    if identity not in profiles:
        if profiles and identity != "default":
            seed = {"wifi": cfg["defaults"]["wifi"]}
        else:
            seed = {key: cfg.get(key) for key in ("wifi", "printer", "display", "esp")}
        profiles[identity] = _normalize_profile(seed, identity)
    cfg["active_esp_id"] = identity
    cfg["esp_devices"] = profiles
    return _restore_profile(cfg, profiles[identity])


def persist_active_profile(cfg):
    _ensure_defaults(cfg)
    profiles = cfg.get("esp_devices")
    cfg["esp_devices"] = profiles if isinstance(profiles, dict) else {}
    identity = normalize_esp_id(cfg.get("active_esp_id"))
    cfg["active_esp_id"] = identity
    current = merge_config(default_esp_profile(identity), cfg["esp_devices"].get(identity))
    for key in ("wifi", "printer", "display", "esp"):
        current[key] = merge_config(DEFAULT_CONFIG[key], cfg.get(key))
    current = _normalize_profile(current, identity)
    cfg["esp_devices"][identity] = current
    if current["wifi"]["ssid"] or current["wifi"]["password"]:
        cfg["defaults"]["wifi"] = copy.deepcopy(current["wifi"])
    return _restore_profile(cfg, current)


def read_config():
    return ensure_esp_profiles(merge_config(DEFAULT_CONFIG, read_json(CONFIG_FILE, DEFAULT_CONFIG)))


def save_config(next_cfg):
    cfg = merge_config(DEFAULT_CONFIG, next_cfg)
    persist_active_profile(cfg)
    ensure_esp_profiles(cfg)
    write_json(CONFIG_FILE, cfg)
    return cfg


def apply_status_to_config(status, serial_port, host=""):
    if not isinstance(status, dict):
        raise ValueError("ESP 状态必须是 JSON 对象")
    cfg = read_config()
    detected = esp_id_from_status(status)
    port = normalize_serial_port(serial_port or cfg["esp"]["serial_port"])
    if detected == "default" and not port:
        raise ValueError("ESP 未报告设备身份；请指定串口并重新检测")
    identity = detected if detected != "default" else normalize_esp_id("port-" + port.lower())
    profiles = cfg["esp_devices"]
    current = profiles.get(identity)
    if current is None:
        # Keep the legacy default snapshot until a genuine hardware identity is
        # learned, even if old firmware has first been identified only by port.
        real_profiles = [p for k, p in profiles.items() if k != "default" and not k.startswith("port-")]
        seed = profiles.get("default") if not real_profiles else None
        if detected != "default" and not real_profiles:
            port_profile = profiles.get(normalize_esp_id("port-" + port.lower())) if port else None
            seed = port_profile or seed
        current = merge_config(default_esp_profile(identity), seed)
        current["id"] = identity
        if not seed:
            current["wifi"] = copy.deepcopy(cfg["defaults"]["wifi"])
    else:
        current = copy.deepcopy(current)
    current["device_id"] = normalize_esp_id(status.get("device_id") or identity)
    current["chip_id"] = str(status.get("chip_id") or current.get("chip_id") or "").strip()
    current["mac"] = str(status.get("mac") or current.get("mac") or "").strip()
    current["label"] = "ESP " + (current["mac"] or current["chip_id"] or port)
    now = int(time.time() * 1000)
    current["last_seen_at"] = now
    current["esp"]["serial_port"] = port
    if host:
        current["esp"]["host"] = normalize_esp_host(host)
    if "ip" in status:
        current["esp"]["host"] = normalize_esp_host(status["ip"])
        current["esp"]["last_ip"] = current["esp"]["host"]
    if "mqtt_connected" in status:
        current["esp"]["last_error"] = ""
    if normalize_esp_host(status.get("ip")):
        current["esp"]["wifi_status"] = "configured"
    elif status.get("ap_ssid"):
        current["esp"]["wifi_status"] = "ap_only"
    elif "ip" in status:
        current["esp"]["wifi_status"] = "unknown"
    current["esp"]["last_checked_at"] = now
    for source, target in (("serial", "serial"), ("name", "display_name"), ("model", "model")):
        if source in status:
            current["printer"][target] = str(status[source] or "")
    if "brightness" in status:
        current["display"]["brightness"] = normalize_brightness(status["brightness"])
    if "layout" in status:
        current["display"]["layout"] = normalize_layout(status["layout"])
    current = _normalize_profile(current, identity)
    profiles[identity] = current
    cfg["active_esp_id"] = identity
    _restore_profile(cfg, current)
    return save_config(cfg)


def activate_esp_profile(id):
    cfg = read_config()
    identity = normalize_esp_id(id)
    if identity not in cfg["esp_devices"]:
        raise ValueError("没有找到这个 ESP 设备档案，请先检测当前 ESP")
    cfg["active_esp_id"] = identity
    _restore_profile(cfg, cfg["esp_devices"][identity])
    cfg = save_config(cfg)
    return {
        "ok": True, "active_esp_id": identity,
        "active_esp_label": esp_label(cfg["esp_devices"][identity]),
        **{key: cfg[key] for key in ("wifi", "esp", "printer", "display")},
    }


def ensure_active_esp_selected(cfg):
    identity = normalize_esp_id(cfg.get("active_esp_id")) if isinstance(cfg, dict) else "default"
    if identity == "default" or identity not in cfg.get("esp_devices", {}):
        raise RuntimeError("请先连接当前要配置的一台 ESP，并检测当前 ESP。")
