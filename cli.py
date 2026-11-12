#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PrintSphere Lite CLI
"""

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from profiles import (
    BACKEND_VERSION, DEFAULT_CONFIG, CONFIG_FILE, DEVICES_FILE, HISTORY_FILE,
    ensure_data, read_json, write_json, merge_config, read_config, save_config,
    normalize_brightness, normalize_layout, normalize_alias, normalize_serial_port,
    normalize_esp_host, esp_id_from_status, esp_label, ensure_esp_profiles,
    ensure_active_esp_selected, apply_status_to_config, activate_esp_profile,
)
from platform_support import render_alias_bitmap, local_lan_prefixes, list_pc_wifi_networks

try:
    import serial  # pyserial
    from serial.tools import list_ports as serial_list_ports
except ImportError:  # 友好提示
    sys.stderr.write(
        "缺少依赖 pyserial，请先安装：\n    pip install pyserial\n"
    )
    sys.exit(1)



# 串口操作串行化，避免多个动作同时抢占 COM 口
_serial_lock = threading.Lock()
_local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
    if "P2S" in normalized or normalized == "N7V2":
        return "P2S"
    if "X2D" in normalized or normalized == "N6V2":
        return "X2D"
    if normalized == "BLP001":
        return "X1C"
    if normalized == "C13":
        return "X1E"
    if "H2DPRO" in normalized:
        return "H2D Pro"
    if "H2C" in normalized or normalized == "O1C2V2":
        return "H2C"
    if "H2D" in normalized or normalized == "O1D":
        return "H2D"
    if "H2S" in normalized or normalized == "O1S":
        return "H2S"
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
        "raw": dev,
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
    snapshot = {"updated_at": int(time.time() * 1000), "devices": devices}
    write_json(DEVICES_FILE, snapshot)
    with open(HISTORY_FILE, "a", encoding="utf-8") as history:
        history.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    sync = sync_printer_list_to_esp(devices)
    if not sync.get("ok") and not sync.get("skipped"):
        print("! 打印机列表同步失败:", sync.get("serial_error") or sync.get("error"))
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
    payload = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"))
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
            buffer = b""
            while time.monotonic() < deadline:
                chunk = ser.read(ser.in_waiting or 1)
                if not chunk:
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    line = raw_line.decode("utf-8", "replace").strip()
                    if line.startswith("{") and (not marker or marker in line):
                        try:
                            parsed = json.loads(line)
                        except ValueError:
                            continue
                        return {"ok": True, "port": port_name, "json": parsed}
                if len(buffer) > 65536:
                    return {"ok": False, "port": port_name, "error": "串口响应过长"}
            return {"ok": False, "port": port_name, "error": "串口无响应"}
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
        return {"status": 0, "error": body.get("error", "ESP 拒绝了串口配置") if isinstance(body, dict) else "ESP 返回了无效确认"}
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
        with _local_http.open(req, timeout=timeout_ms / 1000.0) as resp:
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
    params = {k: str(v) for k, v in payload.items() if v is not None}
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



def auto_detect_esp_ports():
    before = read_config()
    original = before["active_esp_id"]
    ports = list_serial_ports()
    results = []
    for item in ports:
        port = item["port"]
        result = serial_json_command(port, {"cmd": "status"}, '"ok"', 6500)
        body = result.get("json")
        if result["ok"] and isinstance(body, dict) and body.get("ok") is True and esp_id_from_status(body) != "default":
            cfg = apply_status_to_config(body, port)
            results.append({"ok": True, "port": port, "id": cfg["active_esp_id"]})
        else:
            results.append({"ok": False, "port": port, "error": result.get("error") or "不是 PrintSphere ESP"})
    cfg = read_config()
    detected = {r["port"]: r["id"] for r in results if r["ok"]}
    connected = {p["port"] for p in ports}
    for identity, profile in cfg["esp_devices"].items():
        if identity == "default" or identity in detected.values():
            continue
        port = profile["esp"]["serial_port"]
        profile["esp"]["last_error"] = ("当前未连接" if port not in connected else
            "当前串口连接的是另一台 ESP" if port in detected else "串口未响应")
        profile["esp"]["last_checked_at"] = int(time.time() * 1000)
    if len(detected) == 1:
        cfg["active_esp_id"] = next(iter(detected.values()))
    elif original in cfg["esp_devices"]:
        cfg["active_esp_id"] = original
    ensure_esp_profiles(cfg)
    write_json(CONFIG_FILE, cfg)
    return {"ok": len(detected) == 1, "multiple_devices": len(detected) > 1,
            "results": results, "message": "检测到多台 ESP，请只保留一台再写入" if len(detected) > 1 else
            "已选择当前 ESP" if detected else "没有检测到 USB ESP，可选择已保存档案使用 HTTP"}


def prepare_esp_write(cfg=None):
    """锁定已选设备；换机必须重新检测，绝不把旧配置写到新设备。"""
    desired = cfg or read_config()
    ensure_active_esp_selected(desired)
    identity = desired["active_esp_id"]
    selected_port = desired["esp"]["serial_port"]
    detected = []
    for item in list_serial_ports():
        result = serial_json_command(item["port"], {"cmd": "status"}, '"ok"', 3000)
        body = result.get("json")
        if result["ok"] and isinstance(body, dict) and body.get("ok") is True and esp_id_from_status(body) != "default":
            detected.append((item["port"], body))
    if len(detected) > 1:
        raise RuntimeError("检测到多台 ESP 同时连接，请只保留当前要配置的一台")
    if detected:
        port, body = detected[0]
        if esp_id_from_status(body) != identity or (selected_port and port != selected_port):
            raise RuntimeError("USB ESP 身份或串口已改变，请重新检测设备后再配置，未写入任何数据")
        actual = apply_status_to_config(body, port)
    else:
        host = normalize_esp_host(desired["esp"]["host"] or desired["esp"]["last_ip"])
        if not host:
            raise RuntimeError("当前 ESP USB 未响应且没有已确认的 HTTP 地址")
        result = local_request("http://{}:{}/api/status".format(host, desired["esp"]["port"]), timeout_ms=1500)
        if not is_usable_esp_status(result) or esp_id_from_status(result["json"]) != identity:
            raise RuntimeError("HTTP 地址的 ESP 身份未通过校验，已阻止写入")
        actual = apply_status_to_config(result["json"], selected_port, host)
    actual["esp"]["port"] = desired["esp"]["port"]
    for key in ("wifi", "printer", "display"):
        actual[key] = merge_config(DEFAULT_CONFIG[key], desired[key])
    return save_config(actual)


def read_esp_status_via_serial(port):
    cfg = read_config()
    serial_port = normalize_serial_port(port or cfg["esp"]["serial_port"])
    result = serial_json_command(serial_port, {"cmd": "status"}, '"ok"', 9000)
    if not result["ok"] or not isinstance(result.get("json"), dict) or result["json"].get("ok") is not True:
        return {"ok": False, "port": serial_port, "error": result.get("error") or "USB ESP 未响应"}
    cfg = apply_status_to_config(result["json"], serial_port, "")
    return {"ok": True, "port": serial_port, "status": result["json"], "esp": cfg["esp"], "active_esp_id": cfg["active_esp_id"]}


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
    serial_result = list_esp_wifi_via_serial(port)
    if serial_result["ok"]:
        return serial_result
    pc_result = list_pc_wifi_networks()
    pc_result["message"] = "{}；{}；{}".format(http_result["message"], serial_result["message"], pc_result["message"])
    return pc_result


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


def sync_printer_list_to_esp(devices, cfg=None, verified=False):
    printers = compact_printers_for_esp(devices)
    if not printers:
        return {"ok": False, "skipped": True, "reason": "没有可同步的打印机"}
    try:
        cfg = cfg if verified else prepare_esp_write(cfg)
    except RuntimeError as error:
        return {"ok": False, "skipped": True, "reason": str(error)}
    payload = {"printers": printers}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    serial_error = "打印机列表超过串口单次写入长度"
    if len(encoded.encode("utf-8")) <= 1900:
        result = push_config_to_serial(cfg["esp"]["serial_port"], payload)
        if result["status"]:
            return {"ok": True, "transport": "serial", "count": len(printers), "target_esp_id": cfg["active_esp_id"]}
        serial_error = result.get("error")
    host = normalize_esp_host(cfg["esp"]["host"] or cfg["esp"]["last_ip"])
    if not host:
        return {"ok": False, "serial_error": serial_error, "error": "没有 ESP HTTP 地址"}
    from urllib.parse import urlencode
    url = "http://{}:{}/api/printers-url?{}".format(host, cfg["esp"]["port"], urlencode({"printers_json": encoded}))
    result = local_request(url, timeout_ms=2000)
    return {"ok": 200 <= result.get("status", 0) < 300, "transport": "http", "count": len(printers),
            "serial_error": serial_error, "status": result.get("status", 0), "error": result.get("error"),
            "target_esp_id": cfg["active_esp_id"]}


def display_payload(cfg):
    display = cfg["display"]
    return {"brightness": normalize_brightness(display["brightness"]),
            "layout": normalize_layout(display["layout"]), "alias": normalize_alias(display["alias"]),
            "alias_bitmap_hex": display["alias_bitmap_hex"],
            "alias_bitmap_w": display["alias_bitmap_w"], "alias_bitmap_h": display["alias_bitmap_h"]}


def write_esp_payload(cfg, payload):
    result = push_config_to_serial(cfg["esp"]["serial_port"], payload)
    target = {"target_esp_id": cfg["active_esp_id"], "target_serial_port": cfg["esp"]["serial_port"],
              "target_host": cfg["esp"]["host"]}
    if result["status"]:
        return {"ok": True, "transport": "serial", **target}
    if cfg["esp"]["host"]:
        http_result = local_request(esp_config_url(payload, cfg), timeout_ms=1500)
        if 200 <= http_result.get("status", 0) < 300:
            return {"ok": True, "transport": "http", **target}
        return {"ok": False, "serial_error": result.get("error"), "http_error": http_result.get("error") or
                "HTTP {}".format(http_result.get("status", 0)), **target}
    return {"ok": False, "serial_error": result.get("error"), **target}


def push_config_to_esp(cfg=None):
    cfg = prepare_esp_write(cfg)
    if not cfg["cloud"]["access_token"]:
        raise RuntimeError("尚未登录 Bambu 云服务")
    if not cfg["printer"]["serial"]:
        raise RuntimeError("尚未选择打印机")
    if not cfg["wifi"]["ssid"] or not cfg["wifi"]["password"]:
        raise RuntimeError("请先配置 WiFi")
    username = cfg["cloud"]["mqtt_username"] or refresh_mqtt_username()
    if not username:
        raise RuntimeError("没有获取到 MQTT 用户名，请重新登录")
    payload = {"wifi_ssid": cfg["wifi"]["ssid"], "wifi_password": cfg["wifi"]["password"],
               "region": cfg["cloud"]["region"], "mqtt_host": mqtt_host(cfg["cloud"]["region"]),
               "mqtt_username": username, "token": cfg["cloud"]["access_token"],
               "serial": cfg["printer"]["serial"], "name": cfg["printer"]["display_name"] or cfg["printer"]["serial"],
               "model": cfg["printer"]["model"], **display_payload(cfg)}
    result = write_esp_payload(cfg, payload)
    if result["ok"]:
        result["printer_sync"] = sync_printer_list_to_esp(saved_devices(), cfg, verified=True)
    return result


def push_wifi_to_esp(cfg=None):
    cfg = prepare_esp_write(cfg)
    if not cfg["wifi"]["ssid"] or not cfg["wifi"]["password"]:
        raise RuntimeError("请填写 WiFi 名称和密码")
    return write_esp_payload(cfg, {"wifi_ssid": cfg["wifi"]["ssid"], "wifi_password": cfg["wifi"]["password"],
                                   "brightness": normalize_brightness(cfg["display"]["brightness"])})


def push_display_to_esp(cfg=None):
    cfg = prepare_esp_write(cfg)
    return write_esp_payload(cfg, display_payload(cfg))


def configure_wifi_and_detect(cfg):
    write = push_config_to_esp(cfg) if cfg["cloud"]["access_token"] and cfg["printer"]["serial"] else push_wifi_to_esp(cfg)
    if write["ok"]:
        time.sleep(3.5 if write["transport"] == "serial" else 1.2)
    return {"ok": write["ok"], "write": write, "detection": check_esp_status(discover=True)}


def scan_for_esp(port):
    def probe(host):
        result = local_request("http://{}:{}/api/status".format(host, port), timeout_ms=350)
        return (host, result) if is_usable_esp_status(result) else None
    for prefix in local_lan_prefixes():
        with ThreadPoolExecutor(max_workers=32) as pool:
            for found in pool.map(probe, ("{}.{}".format(prefix, i) for i in range(1, 255))):
                if found:
                    return found
    return None


def check_esp_status(discover=False):
    cfg = read_config()
    port = cfg["esp"]["port"]
    if cfg["esp"]["host"]:
        result = local_request("http://{}:{}/api/status".format(cfg["esp"]["host"], port), timeout_ms=1200)
        if is_usable_esp_status(result):
            apply_status_to_config(result["json"], cfg["esp"]["serial_port"], cfg["esp"]["host"])
            ip = normalize_esp_host(result["json"].get("ip"))
            return {"ok": bool(ip), "source": "http", "status": result["json"],
                    "message": "ESP WiFi IP：" + ip if ip else "ESP HTTP 已响应，但未连接 WiFi"}
    result = read_esp_status_via_serial(cfg["esp"]["serial_port"])
    if result["ok"]:
        ip = normalize_esp_host(result["status"].get("ip"))
        return {"ok": bool(ip), "source": "serial", "status": result["status"],
                "message": "ESP WiFi IP：" + ip if ip else "ESP USB 可通信，但未连接 WiFi；请检查密码并稍后重试"}
    if discover:
        found = scan_for_esp(port)
        if found:
            host, response = found
            apply_status_to_config(response["json"], "", host)
            return {"ok": bool(normalize_esp_host(response["json"].get("ip"))), "source": "discover",
                    "status": response["json"], "message": "局域网找到 ESP：" + host}
    cfg["esp"]["wifi_status"] = "unknown"
    cfg["esp"]["last_error"] = result.get("error") or "没有检测到 ESP"
    save_config(cfg)
    return {"ok": False, "message": cfg["esp"]["last_error"]}


# ---------------------------------------------------------------------------
# 终端交互界面
# ---------------------------------------------------------------------------

def prompt(text, default=""):
    suffix = "（{}）".format(default) if default != "" else ""
    return input("{}{}: ".format(text, suffix)).strip() or default


def prompt_password(text):
    import getpass
    return getpass.getpass("{}: ".format(text))


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
    print("  当前设备:", esp_label(cfg["esp_devices"].get(cfg["active_esp_id"])))
    print("  屏幕: {} / {}%".format(cfg["display"]["layout"], cfg["display"]["brightness"]))
    print("=" * 60)


MENU = """
  1) Bambu 云登录（发送验证码 / 验证码登录）
  2) 自动检测当前 ESP / 热插拔识别
  3) 选择串口
  4) 扫描附近 WiFi
  5) 设置 WiFi 并写入 ESP
  6) 刷新打印机列表
  7) 选择打印机并写入云配置
  8) 屏幕设置（0-100 亮度 / 经典或信息面板 / 别名）
  9) 检测 ESP WiFi 状态
 10) 同步打印机列表到 ESP 本地网页
 11) 已保存 ESP 档案 / 切换设备
 12) 高级 HTTP 地址与端口 / 只保存设置
 13) 同步完整云配置到 ESP
 14) 查看当前配置（隐藏密码和 token）
 15) 读取所选串口 ESP 身份
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
    print("→ 正在扫描串口 ESP...")
    result = auto_detect_esp_ports()
    print(result["message"])
    for item in result["results"]:
        print("  {}: {}".format(item["port"], item.get("id") or item.get("error")))


def action_read_device():
    result = read_esp_status_via_serial(read_config()["esp"]["serial_port"])
    if not result["ok"]:
        print("!", result.get("error"))
        return
    status = result["status"]
    print("设备:", result["active_esp_id"], "MAC:", status.get("mac", "--"), "IP:", status.get("ip") or "未联网")


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
    ensure_active_esp_selected(cfg)
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
        result = configure_wifi_and_detect(read_config())
    except Exception as err:
        print("! 写入失败:", err); return
    write = result["write"]
    print("✓ WiFi 已通过 {} 写入".format(write["transport"]) if write["ok"] else
          "! 写入失败: " + str(write.get("serial_error") or write.get("http_error")))
    print("联网状态:", result["detection"].get("message"))


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


def action_select_printer():
    cfg = read_config()
    ensure_active_esp_selected(cfg)
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


def action_set_display():
    cfg = read_config()
    ensure_active_esp_selected(cfg)
    value = prompt("亮度 0-100（0 为关闭背光）", str(cfg["display"]["brightness"]))
    level = float(value)
    if not 0 <= level <= 100:
        raise ValueError("亮度必须是 0-100")
    layout = prompt("布局 classic=经典 / dashboard=信息面板", cfg["display"]["layout"])
    if layout not in ("classic", "dashboard"):
        raise ValueError("布局必须是 classic 或 dashboard")
    cfg["display"]["brightness"] = normalize_brightness(level)
    cfg["display"]["layout"] = layout
    print("当前别名:", cfg["display"]["alias"] or "（空）", "；最新信息面板已隐藏别名，此设置保留协议兼容")
    alias = prompt("别名（留空保留，输入 - 清空）", cfg["display"]["alias"])
    alias = "" if alias == "-" else normalize_alias(alias)
    if alias != cfg["display"]["alias"] or (alias and not cfg["display"]["alias_bitmap_hex"]):
        cfg["display"].update(render_alias_bitmap(alias))
    save_config(cfg)
    if prompt("立即写入 ESP？y/n", "y").lower().startswith("y"):
        print_write_result(push_display_to_esp(cfg))
    else:
        print("已保存屏幕设置，未写入 ESP")


def print_write_result(result):
    if result["ok"]:
        print("✓ 已通过 {} 写入 {}".format(result["transport"], result["target_esp_id"]))
        sync = result.get("printer_sync")
        if sync and not sync["ok"]:
            print("! 打印机列表未同步:", sync.get("reason") or sync.get("serial_error") or sync.get("error"))
    else:
        print("! 写入失败:", result.get("serial_error") or result.get("http_error"))


def action_profiles():
    cfg = read_config()
    profiles = sorted((p for p in cfg["esp_devices"].values() if p["id"] != "default"),
                      key=lambda p: (p["esp"]["serial_port"], p["id"]))
    for index, profile in enumerate(profiles, 1):
        print("{}{}) {}".format("*" if profile["id"] == cfg["active_esp_id"] else " ", index, esp_label(profile)))
    if not profiles:
        print("没有已保存的 ESP 档案，请先检测设备"); return
    choice = prompt("选择档案编号（留空取消）")
    if choice.isdigit() and 1 <= int(choice) <= len(profiles):
        activate_esp_profile(profiles[int(choice) - 1]["id"])
        print("已切换档案；写入前仍会验证物理设备身份")


def action_http_settings():
    cfg = read_config()
    ensure_active_esp_selected(cfg)
    host = prompt("ESP 地址（- 清空）", cfg["esp"]["host"])
    host = "" if host == "-" else host
    if host and not normalize_esp_host(host):
        raise ValueError("请输入局域网 IPv4 或主机名")
    port = int(prompt("ESP HTTP 端口", str(cfg["esp"]["port"])))
    if not 1 <= port <= 65535:
        raise ValueError("端口必须是 1-65535")
    cfg["esp"].update(host=normalize_esp_host(host), port=port)
    save_config(cfg)
    print("已保存，未写入 ESP；使用 HTTP 前会验证设备身份")


def action_push_config():
    print_write_result(push_config_to_esp())


def action_show_config():
    cfg = read_config()
    public = {key: cfg[key] for key in ("active_esp_id", "printer", "display", "esp")}
    public["printer"] = {k: v for k, v in cfg["printer"].items() if k != "access_code"}
    public["display"] = {k: v for k, v in cfg["display"].items() if not k.startswith("alias_bitmap")}
    public["wifi"] = {"ssid": cfg["wifi"]["ssid"], "password_saved": bool(cfg["wifi"]["password"])}
    public["cloud"] = {"region": cfg["cloud"]["region"], "account": cfg["cloud"]["account"],
                       "logged_in": bool(cfg["cloud"]["access_token"])}
    public["version"] = BACKEND_VERSION
    print(json.dumps(public, ensure_ascii=False, indent=2))


def action_check_wifi_status():
    print("→ 正在检测 ESP WiFi 状态...")
    status = check_esp_status(discover=True)
    print(("✓ " if status["ok"] else "! ") + (status.get("message") or status.get("error") or "状态未知"))


def menu_prompt():
    """Detect hotplug only while idle; never run a serial job during an action."""
    if not sys.stdin.isatty():
        return prompt("请选择操作")
    print("请选择操作: ", end="", flush=True)
    signature = tuple(p["port"] for p in list_serial_ports())
    checked_at = time.monotonic()
    identity = read_config()["active_esp_id"]
    while True:
        if sys.platform == "win32":
            import msvcrt
            if msvcrt.kbhit():
                return input().strip()
            time.sleep(0.1)
        else:
            import select
            if select.select([sys.stdin], [], [], 1)[0]:
                return input().strip()
        if time.monotonic() - checked_at >= 8:
            current = tuple(p["port"] for p in list_serial_ports())
            result = auto_detect_esp_ports()
            active = read_config()["active_esp_id"]
            if current != signature or active != identity or result["multiple_devices"]:
                print("\n" + result["message"])
                print("请选择操作: ", end="", flush=True)
            signature, identity = current, active
            checked_at = time.monotonic()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="PrintSphere Lite v0.4.71 跨平台配置工具")
    parser.add_argument("--version", action="version", version=BACKEND_VERSION)
    parser.parse_args()
    ensure_data()
    if sys.stdin.isatty():
        print("正在自动识别 USB ESP...")
        print(auto_detect_esp_ports()["message"])
    actions = {
        "1": action_login,
        "2": action_detect_esp,
        "3": action_choose_serial,
        "4": action_scan_wifi,
        "5": action_set_wifi,
        "6": action_refresh_printers,
        "7": action_select_printer,
        "8": action_set_display,
        "9": action_check_wifi_status,
        "10": action_sync_printers,
        "11": action_profiles,
        "12": action_http_settings,
        "13": action_push_config,
        "14": action_show_config,
        "15": action_read_device,
    }
    while True:
        banner()
        print(MENU)
        choice = menu_prompt()
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
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。")
