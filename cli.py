#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PrintSphere Lite CLI
"""

import base64
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    import serial  # pyserial
    from serial.tools import list_ports as serial_list_ports
except ImportError:  # 友好提示
    sys.stderr.write(
        "缺少依赖 pyserial，请先安装：\n    pip install pyserial\n"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# 常量与默认配置
# ---------------------------------------------------------------------------

BACKEND_VERSION = "cli-v0.4.57-printer-sync"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")

DEFAULT_SERIAL_PORT = "COM4" if os.name == "nt" else ""

DEFAULT_CONFIG = {
    "wifi": {"ssid": "", "password": ""},
    "cloud": {
        "region": "cn",
        "account": "",
        "access_token": "",
        "mqtt_username": "",
        "token_expires_at": 0,
    },
    "printer": {"serial": "", "access_code": "", "display_name": "", "model": ""},
    "display": {"brightness": 100, "active_brightness": 100, "dimmed": False},
    "esp": {
        "host": "",
        "port": 8081,
        "serial_port": DEFAULT_SERIAL_PORT,
        "wifi_status": "unknown",
        "last_ip": "",
        "last_checked_at": 0,
        "last_error": "",
    },
}

# 串口操作串行化，避免多个动作同时抢占 COM 口
_serial_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def ensure_data():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        write_json(CONFIG_FILE, DEFAULT_CONFIG)
    if not os.path.exists(DEVICES_FILE):
        write_json(DEVICES_FILE, {"updated_at": 0, "devices": []})


def merge_config(base, extra):
    """深合并：extra 覆盖 base，对象递归合并。"""
    out = json.loads(json.dumps(base))
    for key, value in (extra or {}).items():
        if isinstance(value, dict):
            out[key] = merge_config(out.get(key) or {}, value)
        else:
            out[key] = value
    return out


def read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.loads(fh.read().lstrip("﻿"))
    except Exception:
        return fallback


def write_json(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    tmp = "{}.{}.{}.tmp".format(path, os.getpid(), int(time.time() * 1000))
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(path):
                os.unlink(path)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def normalize_brightness(value):
    try:
        level = float(value)
    except (TypeError, ValueError):
        return 100
    if level <= 25:
        return 25
    if level <= 50:
        return 50
    if level <= 75:
        return 75
    return 100


def normalize_serial_port(port):
    raw = str(port or "").strip()
    if not raw:
        return DEFAULT_SERIAL_PORT
    if re.match(r"^com\d+$", raw, re.IGNORECASE):
        return raw.upper()
    return raw  # 非 Windows 端口名（/dev/ttyUSB0 等）原样保留


def is_private_ipv4(address):
    parts = str(address or "").split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    if any(n < 0 or n > 255 for n in nums):
        return False
    return (
        nums[0] == 10
        or (nums[0] == 172 and 16 <= nums[1] <= 31)
        or (nums[0] == 192 and nums[1] == 168)
    )


def normalize_esp_host(host):
    value = str(host or "").strip()
    if not value or re.match(r"^\(.*\)$", value):
        return ""
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = value.split("/")[0].split("?")[0].strip()
    if "@" in value:
        value = value.split("@")[-1]
    if value.startswith("[") and "]" in value:
        value = value[1:value.index("]")]
    elif value.count(":") == 1:
        value = value.split(":")[0]
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", value):
        return value if is_private_ipv4(value) else ""
    if re.match(r"^[a-z0-9.-]+$", value, re.IGNORECASE) and "." in value:
        return value
    return ""


def find_deep(value, keys):
    if not isinstance(value, dict):
        if isinstance(value, list):
            for child in value:
                found = find_deep(child, keys)
                if found:
                    return found
        return ""
    for key in keys:
        if key in value and value[key] not in (None, ""):
            return str(value[key])
    for child in value.values():
        if isinstance(child, (dict, list)):
            found = find_deep(child, keys)
            if found:
                return found
    return ""


# ---------------------------------------------------------------------------
# 配置读写（单 ESP 模型：CLI 每次只配置一台 ESP）
# ---------------------------------------------------------------------------

def read_config():
    cfg = merge_config(DEFAULT_CONFIG, read_json(CONFIG_FILE, DEFAULT_CONFIG))
    if not cfg["esp"]["port"] or int(cfg["esp"]["port"]) in (80, 8080):
        cfg["esp"]["port"] = 8081
    cfg["esp"]["serial_port"] = normalize_serial_port(cfg["esp"]["serial_port"])
    cfg["esp"]["host"] = normalize_esp_host(cfg["esp"]["host"])
    cfg["esp"]["last_ip"] = normalize_esp_host(cfg["esp"]["last_ip"])
    cfg["display"]["brightness"] = normalize_brightness(cfg["display"]["brightness"])
    return cfg


def save_config(next_cfg):
    cfg = merge_config(DEFAULT_CONFIG, next_cfg)
    cfg["esp"]["serial_port"] = normalize_serial_port(cfg["esp"]["serial_port"])
    cfg["esp"]["host"] = normalize_esp_host(cfg["esp"]["host"])
    cfg["esp"]["last_ip"] = normalize_esp_host(cfg["esp"]["last_ip"])
    cfg["display"]["brightness"] = normalize_brightness(cfg["display"]["brightness"])
    write_json(CONFIG_FILE, cfg)
    return cfg


# ---------------------------------------------------------------------------
# Bambu 云服务（仅访问官方域名）
# ---------------------------------------------------------------------------

def api_base(region):
    return "https://api.bambulab.com" if region == "global" else "https://api.bambulab.cn"


def mqtt_host(region):
    return "us.mqtt.bambulab.com" if region == "global" else "cn.mqtt.bambulab.com"


def cloud_request(region, method, pathname, body=None, token=None, timeout=20):
    url = api_base(region) + pathname
    site = "https://bambulab.com" if region == "global" else "https://bambulab.cn"
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "Accept-Language": "en-US,en;q=0.9" if region == "global" else "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": site,
        "Referer": site + "/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125 Safari/537.36"
        ),
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.getcode() or 0
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
    except urllib.error.HTTPError as err:
        text = err.read().decode("utf-8", "replace") if err.fp else ""
        status = err.code
        resp_headers = {k.lower(): v for k, v in (err.headers.items() if err.headers else [])}
    except Exception as err:  # 网络错误
        return {"status": 0, "error": str(err), "json": None, "text": "", "headers": {}}
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    return {"status": status, "headers": resp_headers, "json": parsed, "text": text}


def cloud_ok(result):
    if not result or result["status"] < 200 or result["status"] >= 300:
        return False
    body = result.get("json")
    if not isinstance(body, dict):
        return True
    code = body.get("code", body.get("errorCode", body.get("statusCode")))
    if code in (None, ""):
        return True
    return code in (0, "0", 200, "200", "SUCCESS")


def cloud_error(result):
    if not result:
        return "Bambu 云服务没有返回结果"
    if result.get("error"):
        return result["error"]
    body = result.get("json")
    if isinstance(body, dict):
        return body.get("message") or body.get("msg") or body.get("error") or json.dumps(body, ensure_ascii=False)
    return result.get("text") or "HTTP {}".format(result.get("status"))


def pick_token(body, headers):
    token = find_deep(body, ["accessToken", "access_token", "token", "idToken"])
    if token:
        return token
    auth = str((headers or {}).get("authorization", "")).strip()
    return re.sub(r"^Bearer\s+", "", auth, flags=re.IGNORECASE)


def send_code(region, account):
    account = str(account or "").strip()
    if not account:
        raise ValueError("请填写手机号或邮箱")
    cfg = read_config()
    cfg["cloud"]["region"] = region
    cfg["cloud"]["account"] = account
    save_config(cfg)

    is_email = "@" in account
    pathname = "/v1/user-service/user/sendemail/code" if is_email else "/v1/user-service/user/sendsmscode"
    payloads = (
        [{"email": account, "type": "codeLogin"}, {"account": account, "type": "codeLogin"}, {"email": account}]
        if is_email
        else [
            {"phone": account, "type": "codeLogin"},
            {"account": account, "type": "codeLogin"},
            {"phone": account},
            {"mobile": account},
        ]
    )
    last = None
    for payload in payloads:
        last = cloud_request(region, "POST", pathname, payload)
        if cloud_ok(last):
            return "验证码已发送，请查看手机或邮箱"
    raise RuntimeError("验证码发送失败：" + cloud_error(last))


def login(region, account, code):
    account = str(account or "").strip()
    code = str(code or "").strip()
    if not account:
        raise ValueError("请填写手机号或邮箱")
    if not code:
        raise ValueError("请填写验证码")

    cfg = read_config()
    cfg["cloud"]["region"] = region
    cfg["cloud"]["account"] = account
    save_config(cfg)

    tries = (
        [{"email": account, "code": code, "type": "codeLogin"},
         {"account": account, "code": code, "type": "codeLogin"},
         {"account": account, "code": code}]
        if "@" in account
        else [{"phone": account, "code": code, "type": "codeLogin"},
              {"account": account, "code": code, "type": "codeLogin"},
              {"phone": account, "code": code},
              {"mobile": account, "code": code},
              {"account": account, "code": code}]
    )
    last = None
    for payload in tries:
        last = cloud_request(region, "POST", "/v1/user-service/user/login", payload)
        if cloud_ok(last):
            token = pick_token(last.get("json"), last.get("headers"))
            if not token:
                raise RuntimeError("登录成功但没有拿到 access token")
            cfg = read_config()
            cfg["cloud"]["region"] = region
            cfg["cloud"]["account"] = account
            cfg["cloud"]["access_token"] = token
            save_config(cfg)
            refresh_mqtt_username()
            return True
    raise RuntimeError(cloud_error(last) or "Bambu 云登录失败，HTTP {}".format(last.get("status") if last else 0))


def refresh_mqtt_username():
    cfg = read_config()
    if not cfg["cloud"]["access_token"]:
        return ""
    paths = [
        "/v1/design-user-service/my/preference",
        "/v1/user-service/my/profile",
        "/v1/user-service/user/info",
        "/v1/user-service/my/user",
    ]
    for pathname in paths:
        res = cloud_request(cfg["cloud"]["region"], "GET", pathname, None, cfg["cloud"]["access_token"])
        if 200 <= res["status"] < 300:
            uid = find_deep(res.get("json"), ["uid", "userId", "user_id", "id"])
            if uid and not str(uid).startswith("u_"):
                uid = "u_" + str(uid)
            if uid:
                cfg = read_config()
                cfg["cloud"]["mqtt_username"] = uid
                save_config(cfg)
                return uid
    return ""


def normalize_model_name(model):
    raw = str(model or "").strip()
    normalized = re.sub(r"[^a-z0-9]", "", raw, flags=re.IGNORECASE).upper()
    if not normalized:
        return raw
    if "A1MINI" in normalized or normalized == "N1":
        return "A1 mini"
    if normalized == "A1" or "BAMBULABA1" in normalized or normalized == "N2S":
        return "A1"
    if "P1P" in normalized or normalized == "C11":
        return "P1P"
    if "P1S" in normalized or normalized == "C12":
        return "P1S"
    if "P2S" in normalized:
        return "P2S"
    if "X2D" in normalized or normalized == "N6V2":
        return "X2D"
    if "H2DPRO" in normalized:
        return "H2D Pro"
    if "H2D" in normalized:
        return "H2D"
    if "H2S" in normalized:
        return "H2S"
    if "H2C" in normalized:
        return "H2C"
    if "X1E" in normalized or normalized == "AP02":
        return "X1E"
    if "X1CARBON" in normalized or "X1C" in normalized or normalized == "AP05":
        return "X1C"
    if "X1" in normalized:
        return "X1"
    return raw


def normalize_device(dev):
    serial_no = str(dev.get("dev_id") or dev.get("serial") or dev.get("device_id") or "").strip()
    raw_model = str(
        dev.get("dev_model_name") or dev.get("model") or dev.get("product_name")
        or dev.get("dev_product_name") or dev.get("productName") or ""
    ).strip()
    return {
        "serial": serial_no,
        "display_name": str(dev.get("name") or dev.get("dev_name") or dev.get("device_name") or serial_no or "").strip(),
        "model": normalize_model_name(raw_model),
        "access_code": str(dev.get("dev_access_code") or dev.get("access_code") or dev.get("lan_access_code") or "").strip(),
        "online": dev.get("online") is not False,
        "print_status": str(dev.get("print_status") or dev.get("status") or "").strip(),
    }


def fetch_bindings():
    cfg = read_config()
    if not cfg["cloud"]["access_token"]:
        raise RuntimeError("尚未登录 Bambu 云服务")
    res = cloud_request(cfg["cloud"]["region"], "GET", "/v1/iot-service/api/user/bind", None, cfg["cloud"]["access_token"])
    if res["status"] < 200 or res["status"] >= 300:
        raise RuntimeError("读取打印机列表失败：" + cloud_error(res))
    raw = res.get("json") or {}
    devices = [normalize_device(d) for d in (raw.get("devices") or [])]
    devices = [d for d in devices if d["serial"]]
    write_json(DEVICES_FILE, {"updated_at": int(time.time() * 1000), "devices": devices})
    return devices


# ---------------------------------------------------------------------------
# 串口通信（pyserial）
# ---------------------------------------------------------------------------

def list_serial_ports():
    """返回 [{port, name}, ...]，按 COM 号排序（非 Windows 按名称）。"""
    items = []
    for info in serial_list_ports.comports():
        items.append({"port": info.device, "name": (info.description or "串口设备")})

    def sort_key(item):
        m = re.match(r"^COM(\d+)$", item["port"], re.IGNORECASE)
        return (0, int(m.group(1))) if m else (1, item["port"])

    items.sort(key=sort_key)
    return items


def serial_json_command(port, payload_obj, marker="", timeout_ms=35000):
    """
    向 ESP 串口发送一行 JSON，等待返回以 '{' 开头、且包含 marker 的 JSON 行。
    对齐原 PowerShell 行为：115200 8N1，DTR/RTS 关闭（避免复位 ESP），
    打开后等待 1.2s 再发送，逐行读取直到超时。
    """
    port_name = normalize_serial_port(port)
    payload = json.dumps(payload_obj)
    with _serial_lock:
        ser = None
        try:
            ser = serial.Serial()
            ser.port = port_name
            ser.baudrate = 115200
            ser.bytesize = serial.EIGHTBITS
            ser.parity = serial.PARITY_NONE
            ser.stopbits = serial.STOPBITS_ONE
            ser.timeout = 0.5  # ReadTimeout 500ms
            ser.write_timeout = 1.0
            ser.dtr = False
            ser.rts = False
            ser.open()
            try:
                ser.dtr = False
                ser.rts = False
            except Exception:
                pass
            time.sleep(1.2)
            ser.reset_input_buffer()
            ser.write((payload + "\n").encode("utf-8"))
            ser.flush()

            deadline = time.monotonic() + timeout_ms / 1000.0
            buffer = ""
            while time.monotonic() < deadline:
                chunk = ser.readline()
                if not chunk:
                    continue
                line = chunk.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith("{") and (not marker or marker in line):
                    try:
                        parsed = json.loads(line)
                    except ValueError as err:
                        return {"ok": False, "port": port_name, "error": str(err), "text": line}
                    return {"ok": True, "port": port_name, "json": parsed, "text": line}
                buffer += line + "\n"
            return {"ok": False, "port": port_name, "text": buffer, "error": "串口无响应"}
        except Exception as err:
            return {"ok": False, "port": port_name, "error": str(err), "text": ""}
        finally:
            if ser is not None:
                try:
                    if ser.is_open:
                        ser.close()
                except Exception:
                    pass


def push_config_to_serial(port, payload):
    result = serial_json_command(port, payload, '"ok"', 10000)
    if not result["ok"]:
        return {"status": 0, "error": result.get("error") or "ESP 未确认收到串口配置"}
    body = result.get("json")
    if not isinstance(body, dict) or body.get("ok") is not True:
        return {"status": 0, "error": (body or {}).get("error") or "ESP 拒绝了串口配置"}
    return {"status": 200, "json": body, "port": result["port"], "acknowledged": True}


# ---------------------------------------------------------------------------
# 本地 / 局域网 HTTP（ESP 已连 WiFi 时的兜底通道）
# ---------------------------------------------------------------------------

def local_request(url, method="GET", body=None, timeout_ms=1500):
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Connection": "close"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.getcode() or 0
    except urllib.error.HTTPError as err:
        text = err.read().decode("utf-8", "replace") if err.fp else ""
        status = err.code
    except Exception as err:
        return {"status": 0, "error": str(err)}
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    return {"status": status, "json": parsed, "text": text}


def esp_config_url(payload, cfg):
    from urllib.parse import urlencode
    params = {k: str(v) for k, v in payload.items() if v not in (None, "") and len(str(v))}
    base = "http://{}:{}/api/config-url".format(cfg["esp"]["host"], int(cfg["esp"]["port"] or 8081))
    return base + "?" + urlencode(params)


def is_usable_esp_status(result):
    body = result.get("json") if result else None
    if not result or not result.get("status") or not isinstance(body, dict) or body.get("ok") is not True:
        return False
    return str(body.get("ap_ssid") or "").startswith("PrintSphereLite-") or ("mqtt_connected" in body)


def is_wifi_configured_status(body):
    return bool(normalize_esp_host((body or {}).get("ip")))


# ---------------------------------------------------------------------------
# ESP 操作：身份检测、WiFi 扫描、写配置
# ---------------------------------------------------------------------------

def apply_status_to_config(status, serial_port, host=""):
    """把串口/HTTP 读到的 ESP status 落进当前配置（单 ESP 模型）。"""
    cfg = read_config()
    cfg["esp"]["serial_port"] = normalize_serial_port(serial_port or cfg["esp"]["serial_port"])
    cfg["esp"]["port"] = int(cfg["esp"]["port"] or 8081)
    if host:
        cfg["esp"]["host"] = normalize_esp_host(host)
    if isinstance(status, dict):
        if "ip" in status:
            ip = normalize_esp_host(status.get("ip"))
            cfg["esp"]["host"] = ip
            cfg["esp"]["last_ip"] = ip
        if "mqtt_connected" in status:
            cfg["esp"]["last_error"] = ""
        cfg["esp"]["wifi_status"] = (
            "configured" if is_wifi_configured_status(status)
            else ("ap_only" if status.get("ap_ssid") else cfg["esp"]["wifi_status"])
        )
        if status.get("serial"):
            cfg["printer"]["serial"] = str(status["serial"])
        if status.get("name"):
            cfg["printer"]["display_name"] = str(status["name"])
        if "brightness" in status:
            cfg["display"]["brightness"] = normalize_brightness(status["brightness"])
        if "brightness_active" in status:
            cfg["display"]["active_brightness"] = normalize_brightness(status["brightness_active"])
        if "brightness_dimmed" in status:
            cfg["display"]["dimmed"] = bool(status["brightness_dimmed"])
    cfg["esp"]["last_checked_at"] = int(time.time() * 1000)
    save_config(cfg)
    return cfg


def read_esp_status_via_serial(port):
    cfg = read_config()
    serial_port = normalize_serial_port(port or cfg["esp"]["serial_port"])
    result = serial_json_command(serial_port, {"cmd": "status"}, '"ok"', 9000)
    if not result["ok"]:
        return {"ok": False, "port": serial_port, "error": result.get("error") or "USB 串口没有响应"}
    cfg = apply_status_to_config(result["json"], serial_port, "")
    return {"ok": True, "port": serial_port, "status": result["json"], "esp": cfg["esp"]}


def rssi_to_signal(rssi):
    try:
        value = float(rssi)
    except (TypeError, ValueError):
        return 0
    if value <= -100:
        return 0
    if value >= -50:
        return 100
    return round(2 * (value + 100))


def normalize_wifi_networks(networks):
    seen = {}
    for item in networks or []:
        ssid = str((item or {}).get("ssid") or "").strip()
        if not ssid:
            continue
        signal = item.get("signal")
        signal = int(signal) if isinstance(signal, (int, float)) and signal else rssi_to_signal(item.get("rssi"))
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = {"ssid": ssid, "signal": signal}
    return sorted(seen.values(), key=lambda n: (-n["signal"], n["ssid"]))


def list_esp_wifi_via_serial(port):
    cfg = read_config()
    serial_port = normalize_serial_port(port or cfg["esp"]["serial_port"])
    result = serial_json_command(serial_port, {"cmd": "wifi_scan"}, "networks", 35000)
    if not result["ok"]:
        return {"ok": False, "networks": [], "error": result.get("error") or "串口无响应",
                "message": "没有通过 USB 串口 {} 获取到 ESP WiFi 列表".format(serial_port)}
    networks = normalize_wifi_networks((result["json"] or {}).get("networks"))
    return {"ok": True, "port": serial_port, "networks": networks,
            "message": "ESP 通过 USB 串口扫描到 {} 个 WiFi".format(len(networks)) if networks
            else "ESP 没有扫描到 WiFi，可直接手动输入 WiFi 名称。"}


def list_esp_wifi_via_http(cfg):
    port = int(cfg["esp"]["port"] or 8081)
    hosts = []
    for host in [cfg["esp"]["host"], cfg["esp"]["last_ip"], "192.168.4.1"]:
        host = normalize_esp_host(host)
        if host and host not in hosts:
            hosts.append(host)
    for host in hosts:
        result = local_request("http://{}:{}/api/wifi/scan".format(host, port), "GET", None, 9000)
        body = result.get("json")
        if result.get("status") and isinstance(body, dict) and body.get("ok") is True:
            networks = normalize_wifi_networks(body.get("networks"))
            return {"ok": True, "host": host, "networks": networks,
                    "message": "ESP 已扫描到 {} 个 WiFi".format(len(networks)) if networks
                    else "ESP 没有扫描到 WiFi。"}
    return {"ok": False, "networks": [], "message": "没有连接到 ESP 的配置接口"}


def list_wifi_networks(port):
    cfg = read_config()
    http_result = list_esp_wifi_via_http(cfg)
    if http_result["ok"]:
        return http_result
    return list_esp_wifi_via_serial(port)


def saved_devices():
    return (read_json(DEVICES_FILE, {"devices": []}) or {}).get("devices") or []


def compact_printers_for_esp(devices):
    """精简打印机列表，对齐 ESP 本地网页所需字段，最多 16 台。"""
    out = []
    for item in devices or []:
        serial_no = str((item or {}).get("serial") or "").strip()
        if not serial_no:
            continue
        out.append({
            "serial": serial_no,
            "name": str(item.get("display_name") or item.get("name") or serial_no).strip()[:40],
            "model": str(item.get("model") or "").strip()[:20],
        })
        if len(out) >= 16:
            break
    return out


def sync_printer_list_to_esp(devices, cfg=None):
    """把精简打印机列表推送到 ESP 本地网页：串口优先，HTTP 兜底。"""
    cfg = cfg or read_config()
    host = normalize_esp_host(cfg["esp"]["host"] or cfg["esp"]["last_ip"])
    printers = compact_printers_for_esp(devices)
    if not printers:
        return {"ok": False, "skipped": True, "reason": "没有可同步的打印机"}
    serial_error = "USB 串口未连接"
    serial_payload = {"printers": printers}
    if len(json.dumps(serial_payload).encode("utf-8")) <= 1900:
        serial_port = normalize_serial_port(cfg["esp"]["serial_port"])
        if serial_port in [p["port"] for p in list_serial_ports()]:
            serial_result = push_config_to_serial(serial_port, serial_payload)
            if serial_result["status"]:
                return {"ok": True, "transport": "serial", "count": len(printers)}
            serial_error = serial_result.get("error") or "ESP 未确认打印机列表"
    else:
        serial_error = "打印机列表超过串口单次写入长度"
    if not host:
        return {"ok": False, "skipped": True, "reason": "没有 ESP 地址", "serial_error": serial_error}
    from urllib.parse import urlencode
    params = urlencode({"printers_json": json.dumps({"printers": printers})})
    url = "http://{}:{}/api/printers-url?{}".format(host, int(cfg["esp"]["port"] or 8081), params)
    result = local_request(url, "GET", None, 2000)
    ok = 200 <= result.get("status", 0) < 300
    return {"ok": ok, "transport": "http", "host": host, "count": len(printers),
            "status": result.get("status", 0), "serial_error": serial_error}


def push_config_to_esp(cfg=None):
    """写入完整云 MQTT 配置：串口优先，HTTP 兜底。"""
    cfg = cfg or read_config()
    if not cfg["cloud"]["access_token"]:
        raise RuntimeError("尚未登录 Bambu 云服务")
    if not cfg["printer"]["serial"]:
        raise RuntimeError("尚未选择打印机")
    if not cfg["wifi"]["ssid"] or not cfg["wifi"]["password"]:
        raise RuntimeError("尚未配置 WiFi，请先完成 WiFi 步骤。")
    username = cfg["cloud"]["mqtt_username"] or refresh_mqtt_username()
    if not username:
        raise RuntimeError("没有获取到 MQTT 用户名，请重新登录 Bambu 云服务")
    payload = {
        "wifi_ssid": cfg["wifi"]["ssid"],
        "wifi_password": cfg["wifi"]["password"],
        "region": cfg["cloud"]["region"],
        "mqtt_host": mqtt_host(cfg["cloud"]["region"]),
        "mqtt_username": username,
        "token": cfg["cloud"]["access_token"],
        "serial": cfg["printer"]["serial"],
        "name": cfg["printer"]["display_name"] or cfg["printer"]["serial"],
        "brightness": normalize_brightness(cfg["display"]["brightness"]),
    }
    serial_result = push_config_to_serial(cfg["esp"]["serial_port"], payload)
    if serial_result["status"]:
        printer_sync = sync_printer_list_to_esp(saved_devices(), cfg)
        return {"ok": True, "transport": "serial", "printer_sync": printer_sync, **serial_result}
    if cfg["esp"]["host"]:
        http_result = local_request(esp_config_url(payload, cfg), "GET", None, 1500)
        if 200 <= http_result.get("status", 0) < 300:
            printer_sync = sync_printer_list_to_esp(saved_devices(), cfg)
            return {"ok": True, "transport": "http", "printer_sync": printer_sync, **http_result}
        return {"ok": False, "transport": "failed", "serial_error": serial_result.get("error"),
                "http_error": "ESP HTTP 写入失败"}
    return {"ok": False, "transport": "failed", "serial_error": serial_result.get("error")}


def push_wifi_to_esp(cfg=None):
    cfg = cfg or read_config()
    if not cfg["wifi"]["ssid"] or not cfg["wifi"]["password"]:
        raise RuntimeError("请填写 WiFi 名称和密码")
    payload = {
        "wifi_ssid": cfg["wifi"]["ssid"],
        "wifi_password": cfg["wifi"]["password"],
        "brightness": normalize_brightness(cfg["display"]["brightness"]),
    }
    serial_result = push_config_to_serial(cfg["esp"]["serial_port"], payload)
    if serial_result["status"]:
        return {"ok": True, "transport": "serial", **serial_result}
    if cfg["esp"]["host"]:
        http_result = local_request(esp_config_url(payload, cfg), "GET", None, 1500)
        if 200 <= http_result.get("status", 0) < 300:
            return {"ok": True, "transport": "http", **http_result}
        return {"ok": False, "transport": "failed", "serial_error": serial_result.get("error"),
                "http_error": "ESP HTTP 写入失败"}
    return {"ok": False, "transport": "failed", "serial_error": serial_result.get("error")}


def push_brightness_to_esp(cfg=None):
    cfg = cfg or read_config()
    payload = {"brightness": normalize_brightness(cfg["display"]["brightness"])}
    serial_result = push_config_to_serial(cfg["esp"]["serial_port"], payload)
    if serial_result["status"]:
        return {"ok": True, "transport": "serial", **serial_result}
    if cfg["esp"]["host"]:
        http_result = local_request(esp_config_url(payload, cfg), "GET", None, 1500)
        if 200 <= http_result.get("status", 0) < 300:
            return {"ok": True, "transport": "http", **http_result}
        return {"ok": False, "transport": "failed", "serial_error": serial_result.get("error"),
                "http_error": http_result.get("error") or "HTTP {}".format(http_result.get("status", 0))}
    return {"ok": False, "transport": "failed", "serial_error": serial_result.get("error")}


def check_esp_status():
    cfg = read_config()
    port = int(cfg["esp"]["port"] or 8081)
    if cfg["esp"]["host"]:
        result = local_request("http://{}:{}/api/status".format(cfg["esp"]["host"], port), "GET", None, 1200)
        if is_usable_esp_status(result):
            apply_status_to_config(result["json"], cfg["esp"]["serial_port"], cfg["esp"]["host"])
            ip = normalize_esp_host((result["json"] or {}).get("ip"))
            return {"ok": bool(ip), "source": "http",
                    "message": "ESP 已通过 HTTP 检测到 WiFi IP：{}".format(ip) if ip
                    else "ESP HTTP 已响应，但还没有连上 WiFi。"}
    # USB 串口兜底
    serial_port = cfg["esp"]["serial_port"]
    result = serial_json_command(serial_port, {"cmd": "status"}, '"ok"', 9000)
    if not result["ok"]:
        return {"ok": False, "source": "serial", "error": result.get("error") or "USB 串口没有响应",
                "message": result.get("error") or "USB 串口没有响应"}
    apply_status_to_config(result["json"], serial_port, "")
    ip = normalize_esp_host((result["json"] or {}).get("ip"))
    return {"ok": bool(ip), "source": "serial",
            "message": "ESP 已通过 USB 检测到 WiFi IP：{}".format(ip) if ip
            else "ESP USB 可通信，但还没有连上 WiFi。请确认 WiFi 名称和密码，等待 10-20 秒后再检测。"}


# ---------------------------------------------------------------------------
# 终端交互界面
# ---------------------------------------------------------------------------

def prompt(text, default=""):
    suffix = "（{}）".format(default) if default else ""
    try:
        value = input("{}{}: ".format(text, suffix)).strip()
    except EOFError:
        return default
    return value or default


def prompt_password(text):
    import getpass
    try:
        return getpass.getpass("{}: ".format(text)).strip()
    except (EOFError, Exception):
        return input("{}: ".format(text)).strip()


def pause():
    try:
        input("\n按回车返回菜单...")
    except EOFError:
        pass


def banner():
    cfg = read_config()
    logged = "已登录" if cfg["cloud"]["access_token"] else "未登录"
    printer = cfg["printer"]["display_name"] or cfg["printer"]["serial"] or "未选择"
    wifi = cfg["wifi"]["ssid"] or "未填写"
    print("=" * 60)
    print("  PrintSphere Lite 配置工具 · CLI  ({})".format(BACKEND_VERSION))
    print("-" * 60)
    print("  云账号: {} ({})   串口: {}".format(cfg["cloud"]["account"] or "--", logged, cfg["esp"]["serial_port"] or "--"))
    print("  WiFi: {}   打印机: {}   ESP地址: {}".format(wifi, printer, cfg["esp"]["host"] or "--"))
    print("=" * 60)


MENU = """
  1) Bambu 云登录（发送验证码 / 验证码登录）
  2) 检测当前 ESP（读取串口身份）
  3) 选择串口
  4) 扫描附近 WiFi
  5) 设置 WiFi 并写入 ESP
  6) 刷新打印机列表
  7) 选择打印机并写入云配置
  8) 设置屏幕亮度
  9) 检测 ESP WiFi 状态
 10) 同步打印机列表到 ESP 本地网页
  0) 退出
"""


def action_login():
    cfg = read_config()
    region = prompt("区域 cn=中国区 / global=国际区", cfg["cloud"]["region"] or "cn")
    region = "global" if region.lower() == "global" else "cn"
    account = prompt("手机号或邮箱", cfg["cloud"]["account"])
    if not account:
        print("! 未填写账号"); return
    if prompt("是否发送验证码？y/n", "y").lower().startswith("y"):
        try:
            print("→", send_code(region, account))
        except Exception as err:
            print("! 发送失败:", err); return
    code = prompt("请输入收到的验证码")
    if not code:
        print("! 未填写验证码"); return
    try:
        login(region, account, code)
        cfg = read_config()
        print("✓ 登录成功。MQTT 用户名:", cfg["cloud"]["mqtt_username"] or "（未取到）")
        try:
            devices = fetch_bindings()
            print("✓ 已读取 {} 台打印机。".format(len(devices)))
        except Exception as err:
            print("! 打印机列表读取失败:", err)
    except Exception as err:
        print("! 登录失败:", err)


def action_detect_esp():
    cfg = read_config()
    print("→ 正在通过串口 {} 读取 ESP 身份...".format(cfg["esp"]["serial_port"] or "（未选择）"))
    result = read_esp_status_via_serial(cfg["esp"]["serial_port"])
    if not result["ok"]:
        print("! 检测失败:", result.get("error"))
        return
    status = result["status"] or {}
    print("✓ ESP 已响应。")
    print("   MAC:", status.get("mac", "--"), " chip:", status.get("chip_id", "--"))
    ip = normalize_esp_host(status.get("ip"))
    print("   WiFi:", "已连，IP " + ip if ip else "未连上 WiFi")

    if prompt("查看 ESP 返回的完整原始数据？y/n", "n").lower().startswith("y"):
        print(json.dumps(status, ensure_ascii=False, indent=2))


def action_choose_serial():
    ports = list_serial_ports()
    if not ports:
        print("! 没有检测到串口设备。")
        manual = prompt("手动输入串口名（留空取消）")
        if manual:
            cfg = read_config(); cfg["esp"]["serial_port"] = normalize_serial_port(manual); save_config(cfg)
            print("✓ 已设置串口:", cfg["esp"]["serial_port"])
        return
    print("检测到的串口：")
    for idx, item in enumerate(ports, 1):
        print("  {}) {}  {}".format(idx, item["port"], item["name"]))
    choice = prompt("选择串口编号（留空取消）")
    if not choice.isdigit():
        return
    idx = int(choice) - 1
    if 0 <= idx < len(ports):
        cfg = read_config(); cfg["esp"]["serial_port"] = normalize_serial_port(ports[idx]["port"]); save_config(cfg)
        print("✓ 已设置串口:", cfg["esp"]["serial_port"])


def action_scan_wifi():
    cfg = read_config()
    print("→ 正在扫描 WiFi（优先 ESP HTTP，其次 USB 串口）...")
    result = list_wifi_networks(cfg["esp"]["serial_port"])
    networks = result.get("networks") or []
    if networks:
        print("✓ 扫描到 {} 个 WiFi：".format(len(networks)))
        for n in networks:
            print("   - {} ({}%)".format(n["ssid"], n["signal"]))
    else:
        print("! ", result.get("message") or "未扫描到 WiFi，可手动输入。")


def action_set_wifi():
    cfg = read_config()
    ssid = prompt("WiFi 名称(SSID)", cfg["wifi"]["ssid"])
    if not ssid:
        print("! 未填写 SSID"); return
    password = prompt_password("WiFi 密码（留空保留已存密码）") or cfg["wifi"]["password"]
    cfg["wifi"]["ssid"] = ssid
    cfg["wifi"]["password"] = password
    save_config(cfg)
    if not prompt("是否立即写入 ESP？y/n", "y").lower().startswith("y"):
        print("✓ 已保存 WiFi 设置（未写入）。"); return
    print("→ 正在写入 ESP...")
    try:
        result = push_wifi_to_esp(read_config())
    except Exception as err:
        print("! 写入失败:", err); return
    if result["ok"]:
        print("✓ WiFi 已通过 {} 写入。".format("USB 串口" if result["transport"] == "serial" else "ESP HTTP"))
        time.sleep(3.5 if result["transport"] == "serial" else 1.2)
        status = check_esp_status()
        print("   状态:", status.get("message"))
    else:
        print("! 写入失败:", result.get("serial_error") or result.get("http_error"))


def action_refresh_printers():
    try:
        devices = fetch_bindings()
    except Exception as err:
        print("! ", err); return
    if not devices:
        print("! 没有读取到打印机。"); return
    print("✓ 共 {} 台打印机：".format(len(devices)))
    for idx, d in enumerate(devices, 1):
        print("  {}) {}  型号:{}  序列号:{}  {}".format(
            idx, d["display_name"], d["model"], d["serial"], d.get("print_status") or ""))
    # 顺带把打印机列表同步到 ESP 本地网页（串口优先，HTTP 兜底）
    sync = sync_printer_list_to_esp(devices, read_config())
    if sync.get("ok"):
        print("✓ 已通过 {} 同步打印机列表到 ESP 本地网页。".format(
            "USB 串口" if sync.get("transport") == "serial" else "ESP HTTP"))
    elif not sync.get("skipped"):
        print("! 打印机列表未能同步到 ESP:", sync.get("serial_error") or sync.get("reason") or "")


def action_select_printer():
    cfg = read_config()
    if not cfg["cloud"]["access_token"]:
        print("! 请先登录 Bambu 云。"); return
    if not (cfg["wifi"]["ssid"] and cfg["wifi"]["password"]):
        print("! 请先完成 WiFi 设置（菜单 5）。"); return
    devices = saved_devices()
    if not devices:
        print("! 打印机列表为空，请先刷新（菜单 6）。"); return
    print("已保存的打印机：")
    for idx, d in enumerate(devices, 1):
        print("  {}) {}  ({})  {}".format(idx, d["display_name"], d["model"], d["serial"]))
    choice = prompt("选择要显示的打印机编号")
    if not choice.isdigit() or not (1 <= int(choice) <= len(devices)):
        print("! 选择无效"); return
    device = devices[int(choice) - 1]
    cfg = read_config()
    cfg["printer"]["serial"] = device["serial"]
    cfg["printer"]["access_code"] = device.get("access_code") or cfg["printer"]["access_code"]
    cfg["printer"]["display_name"] = device["display_name"] or device["serial"]
    cfg["printer"]["model"] = device.get("model") or ""
    saved = save_config(cfg)
    print("→ 正在把云 MQTT 配置写入 ESP...")
    try:
        result = push_config_to_esp(saved)
    except Exception as err:
        print("! 写入失败:", err); return
    if result["ok"]:
        print("✓ 已通过 {} 写入打印机 {}。".format(
            "USB 串口" if result["transport"] == "serial" else "ESP HTTP", device["display_name"]))
        sync = result.get("printer_sync") or {}
        if sync.get("ok"):
            print("   并已同步打印机列表到 ESP 本地网页。")
        elif not sync.get("skipped"):
            print("   ! 打印机列表未能同步到 ESP:", sync.get("serial_error") or sync.get("reason") or "")
    else:
        print("! 写入失败:", result.get("serial_error") or result.get("http_error"))


def action_sync_printers():
    devices = saved_devices()
    if not devices:
        print("! 打印机列表为空，请先刷新打印机列表（菜单 6）。"); return
    cfg = read_config()
    print("→ 正在把 {} 台打印机同步到 ESP 本地网页（串口优先，HTTP 兜底）...".format(len(devices)))
    result = sync_printer_list_to_esp(devices, cfg)
    if result.get("ok"):
        print("✓ 已通过 {} 同步 {} 台打印机到 ESP 本地网页。".format(
            "USB 串口" if result.get("transport") == "serial" else "ESP HTTP", result.get("count")))
    else:
        print("! 同步失败:", result.get("serial_error") or result.get("reason")
              or "请确认 ESP 已连接 USB 串口，或先检测到 ESP 的局域网地址。")


def action_set_brightness():
    cfg = read_config()
    print("当前亮度:", cfg["display"]["brightness"], "%")
    value = prompt("设置屏幕亮度 25/50/75/100", str(cfg["display"]["brightness"]))
    cfg["display"]["brightness"] = normalize_brightness(value)
    save_config(cfg)
    if not prompt("是否立即写入 ESP？y/n", "y").lower().startswith("y"):
        print("✓ 已保存亮度:", cfg["display"]["brightness"], "%"); return
    print("→ 正在写入亮度...")
    try:
        result = push_brightness_to_esp(read_config())
    except Exception as err:
        print("! 写入失败:", err); return
    print("✓ 已写入。" if result["ok"] else "! 写入失败: " + str(result.get("serial_error") or result.get("http_error")))


def action_check_wifi_status():
    print("→ 正在检测 ESP WiFi 状态...")
    status = check_esp_status()
    print(("✓ " if status["ok"] else "! ") + (status.get("message") or status.get("error") or "状态未知"))


def main():
    ensure_data()
    actions = {
        "1": action_login,
        "2": action_detect_esp,
        "3": action_choose_serial,
        "4": action_scan_wifi,
        "5": action_set_wifi,
        "6": action_refresh_printers,
        "7": action_select_printer,
        "8": action_set_brightness,
        "9": action_check_wifi_status,
        "10": action_sync_printers,
    }
    while True:
        banner()
        print(MENU)
        choice = prompt("请选择操作", "")
        if choice == "0":
            print("再见。")
            return
        action = actions.get(choice)
        if not action:
            print("! 无效选项")
            continue
        print()
        try:
            action()
        except KeyboardInterrupt:
            print("\n（已取消）")
        except Exception as err:
            print("! 出错:", err)
        pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出。")
