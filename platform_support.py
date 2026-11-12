"""Native font rendering, LAN discovery and host Wi-Fi scanning.

Pillow and psutil are required for their respective operations. macOS Wi-Fi
scanning additionally needs pyobjc-framework-CoreWLAN and Location Services
permission for the terminal application. Linux scanning uses NetworkManager's
nmcli executable; Windows uses netsh. Scanning never changes Wi-Fi connections.
"""

import ipaddress
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys


_CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U000323af\u3040-\u30ff]")
_VIRTUAL = re.compile(r"virtual|vmware|virtualbox|vbox|vethernet|loopback|bluetooth", re.I)
_PRIVATE_V4 = tuple(ipaddress.IPv4Network(net) for net in
                    ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _font_candidates(cjk):
    """Find installed fonts, prioritizing bold and CJK-capable families."""
    if sys.platform == "win32":
        roots = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
                 Path(os.environ.get("LOCALAPPDATA", "~")).expanduser() / "Microsoft/Windows/Fonts"]
    elif sys.platform == "darwin":
        roots = [Path("/System/Library/Fonts"), Path("/Library/Fonts"),
                 Path.home() / "Library/Fonts"]
    else:
        roots = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                 Path.home() / ".local/share/fonts", Path.home() / ".fonts"]
    cjk_names = ("notosanscjk", "notoserifcjk", "sourcehansans", "sourcehanserif",
                 "pingfang", "hiragino", "heiti", "msyh", "msjh", "simsun", "simhei",
                 "arialuni", "malgun", "wenquanyi", "wqy", "droidsansfallback")
    latin_names = ("dejavusans", "liberationsans", "arial", "helvetica", "notosans")
    paths = set()
    for root in roots:
        if not root.is_dir():
            continue
        for folder, _, names in os.walk(root):
            for name in names:
                if Path(name).suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"):
                    paths.add(str(Path(folder) / name))

    def rank(path):
        name = re.sub(r"[\s_-]", "", Path(path).name.lower())
        preferred = cjk_names if cjk else latin_names + cjk_names
        family = next((i for i, part in enumerate(preferred) if part in name), len(preferred))
        return family, 0 if "bold" in name or "demi" in name else 1, path

    return sorted(paths, key=rank)


def _supports_text(font, text):
    # FreeType renders unsupported codepoints using the font's .notdef glyph.
    # Compare against unassigned codepoints rather than accepting tofu as text.
    def signature(char):
        mask = font.getmask(char, mode="1")
        return mask.size, bytes(mask)

    missing = {signature("\U0010ffff"), signature("\u0378")}
    return all(signature(char) not in missing for char in set(text) if not char.isspace())


def render_alias_bitmap(alias, font_path=None):
    """Render up to 16 alias characters into row-major, MSB-first 1-bit hex.

    Use font_path or PRINTSPHERE_FONT to choose a font file explicitly. A failed
    nonempty render raises RuntimeError instead of clearing the device alias.
    """
    text = str(alias or "").strip()[:16]
    result = {"alias": text, "alias_bitmap_hex": "", "alias_bitmap_w": 0, "alias_bitmap_h": 0}
    if not text:
        return result
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Alias rendering requires Pillow; install it with: python -m pip install Pillow") from exc

    explicit = font_path or os.environ.get("PRINTSPHERE_FONT")
    candidates = [str(Path(explicit).expanduser())] if explicit else _font_candidates(bool(_CJK.search(text)))
    font = None
    for candidate in candidates:
        try:
            loaded = ImageFont.truetype(candidate, 16)
            if _supports_text(loaded, text):
                font = loaded
                break
        except (OSError, ValueError):
            continue
    if font is None:
        detail = "The selected font is unreadable or lacks alias glyphs." if explicit else "No installed font can render this alias."
        raise RuntimeError(detail + " Install a suitable font (Noto Sans CJK for Chinese/Japanese/Korean), "
                           "then set PRINTSPHERE_FONT to its .ttf/.otf/.ttc file or supply font_path.")

    # Synthetic one-pixel emboldening also makes regular system CJK fonts legible
    # at the backend's 16px size. Reserve the same 22px-high, at-most-108px area.
    left, top, right, bottom = font.getbbox(text)
    width = min(108, max(8, int(math.ceil(max(font.getlength(text), right - left))) + 2))
    image = Image.new("1", (width, 22), 0)
    draw = ImageDraw.Draw(image)
    draw.text((max(0, -left), max(0, (22 - (bottom - top)) // 2) - top), text,
              font=font, fill=1, stroke_width=1, stroke_fill=1)
    if image.getbbox() is None:
        raise RuntimeError("The alias contains no visible glyphs; choose visible text or clear the alias explicitly.")
    # Pillow's 1-bit raw encoder pads each row and places x=0 at bit 7.
    result.update(alias_bitmap_hex=image.tobytes().hex(), alias_bitmap_w=width, alias_bitmap_h=22)
    return result


def local_lan_prefixes():
    """Return unique RFC1918 /24 prefixes from nonvirtual IPv4 interfaces."""
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError("LAN discovery requires psutil; install it with: python -m pip install psutil") from exc
    prefixes = []
    for name, addresses in psutil.net_if_addrs().items():
        if _VIRTUAL.search(name):
            continue
        for item in addresses:
            if item.family != socket.AF_INET:
                continue
            try:
                address = ipaddress.IPv4Address(item.address)
            except ipaddress.AddressValueError:
                continue
            if address.is_loopback or not any(address in net for net in _PRIVATE_V4):
                continue
            if str(address).startswith("192.168.56."):
                continue
            prefix = str(address).rsplit(".", 1)[0]
            if prefix not in prefixes:
                prefixes.append(prefix)
    return prefixes


def _rssi_percent(rssi):
    return max(0, min(100, int(math.floor(2 * (float(rssi) + 100) + 0.5))))


def _scan_command(args, env=None):
    try:
        result = subprocess.run(args, capture_output=True, timeout=20, env=env,
                                encoding="utf-8", errors="replace", check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("Wi-Fi scanning command is not installed: " + args[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Wi-Fi scan timed out; check that the wireless adapter is enabled.") from exc
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Wi-Fi scan failed").strip())
    return result.stdout


def _windows_networks():
    output = _scan_command(["cmd.exe", "/d", "/s", "/c",
                            "chcp 65001>nul & netsh wlan show networks mode=bssid"])
    # netsh sometimes returns exit status zero for denied location access or a
    # missing WLAN adapter. Do not turn those responses into successful scans.
    if re.search(r"location|位置|privacy|隐私|denied|拒绝|administrator|管理员|elevation|提升|"
                 r"no wireless|no wlan|没有无线|未运行|not running|turned off|已关闭", output, re.I):
        raise RuntimeError(output.strip())
    networks = []
    current = None
    recognized = False
    for line in output.splitlines():
        match = re.match(r"\s*SSID\s+\d+\s*[:：]\s*(.*?)\s*$", line, re.I)
        if match:
            recognized = True
            current = {"ssid": match.group(1), "signal": 0}
            networks.append(current)
        elif current is not None:
            match = re.match(r"\s*[^:：]+[:：]\s*(\d+)\s*%\s*$", line)
            if match:
                current["signal"] = max(current["signal"], min(100, int(match.group(1))))
    if not recognized and not re.search(r"(?:There (?:are|is)\s+0\s+networks?|(?:当前|共).*?0\s*个.*?网络)", output, re.I):
        raise RuntimeError("netsh did not return a recognized Wi-Fi scan; check WLAN service and adapter availability.")
    return networks


def _split_nmcli(line):
    fields, chars = [], []
    escaped = False
    for char in line:
        if escaped:
            chars.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(chars))
            chars = []
        else:
            chars.append(char)
    if escaped:
        chars.append("\\")
    fields.append("".join(chars))
    return fields


def _linux_networks():
    env = dict(os.environ, LC_ALL="C")
    radio = _scan_command(["nmcli", "radio", "wifi"], env=env).strip()
    if radio != "enabled":
        raise RuntimeError("Wi-Fi radio is disabled; enable it in system settings or run: nmcli radio wifi on")
    devices = _scan_command(["nmcli", "--terse", "--escape", "yes", "--fields", "TYPE,STATE",
                             "device", "status"], env=env)
    wifi_states = [fields[1] for line in devices.splitlines()
                   if len(fields := _split_nmcli(line)) == 2 and fields[0] == "wifi"]
    if not wifi_states or all(state in ("unavailable", "unmanaged") for state in wifi_states):
        raise RuntimeError("No available NetworkManager-managed Wi-Fi adapter; enable the adapter and NetworkManager management.")
    output = _scan_command(["nmcli", "--terse", "--escape", "yes", "--fields", "SSID,SIGNAL",
                            "device", "wifi", "list", "--rescan", "yes"], env=env)
    networks = []
    for line in output.splitlines():
        if not line:
            continue
        fields = _split_nmcli(line)
        if len(fields) != 2 or not fields[1].isdigit():
            raise RuntimeError("NetworkManager returned an unrecognized Wi-Fi scan row; update nmcli and retry.")
        networks.append({"ssid": fields[0], "signal": min(100, int(fields[1]))})
    return networks


def _macos_networks():
    # Keep PyObjC entirely optional on non-Darwin platforms.
    try:
        import CoreWLAN
    except ImportError as exc:
        raise RuntimeError("macOS Wi-Fi scanning requires pyobjc-framework-CoreWLAN; install it with: "
                           "python -m pip install pyobjc-framework-CoreWLAN") from exc
    interfaces = CoreWLAN.CWWiFiClient.sharedWiFiClient().interfaces()
    if not interfaces:
        raise RuntimeError("No Wi-Fi adapter is available. Enable Wi-Fi in System Settings.")
    networks = []
    failures = []
    hidden_count = 0
    for interface in interfaces:
        found, error = interface.scanForNetworksWithName_error_(None, None)
        if error is not None:
            failures.append(str(error.localizedDescription()))
            continue
        for network in found or []:
            ssid = network.ssid()
            if ssid is None:
                hidden_count += 1
                continue
            networks.append({"ssid": str(ssid), "signal": _rssi_percent(network.rssiValue())})
    if not networks and failures:
        raise RuntimeError("CoreWLAN scan failed: " + "; ".join(failures) +
                           ". Enable Wi-Fi and allow Location Services for your terminal in "
                           "System Settings > Privacy & Security > Location Services.")
    if not networks and hidden_count:
        raise RuntimeError("macOS withheld Wi-Fi names (or only hidden networks are visible). Allow Location Services "
                           "for your terminal in System Settings > Privacy & Security > Location Services, then restart it.")
    return networks


def list_pc_wifi_networks():
    """Scan the host Wi-Fi adapter; failures retain manual SSID entry as an option."""
    try:
        if sys.platform == "win32":
            scanned = _windows_networks()
        elif sys.platform == "darwin":
            scanned = _macos_networks()
        elif sys.platform.startswith("linux"):
            scanned = _linux_networks()
        else:
            raise RuntimeError("Host Wi-Fi scanning is supported on Windows, macOS and Linux only.")
        seen = {}
        for item in scanned:
            ssid = str(item["ssid"] or "").strip()
            if not ssid:
                continue
            signal = max(0, min(100, int(item["signal"])))
            if ssid not in seen or signal > seen[ssid]["signal"]:
                seen[ssid] = {"ssid": ssid, "signal": signal}
        networks = sorted(seen.values(), key=lambda item: (-item["signal"], item["ssid"]))
        return {"ok": True, "source": "pc", "networks": networks,
                "message": "电脑已扫描到 {} 个 WiFi".format(len(networks)) if networks
                else "电脑未扫描到 WiFi，请确认无线网卡已启用；也可直接手动输入 WiFi 名称。"}
    except Exception as exc:
        error = str(exc)
        if re.search(r"location|privacy|位置|隐私", error, re.I):
            advice = "请在系统隐私设置中为终端开启位置服务权限，并重新打开终端。"
        elif re.search(r"permission|authorized|denied|elevation|administrator|权限|拒绝|管理员|提升", error, re.I):
            advice = "请允许无线扫描权限（Windows 可用管理员终端；Linux 请检查 NetworkManager/Polkit 权限）。"
        elif sys.platform.startswith("linux"):
            advice = "请安装 NetworkManager/nmcli，启动 NetworkManager 并启用无线网卡。"
        else:
            advice = "请确认无线网卡和 WiFi 服务已启用。"
        return {"ok": False, "source": "pc", "networks": [], "error": error,
                "message": "电脑 WiFi 扫描失败。" + advice + "也可直接手动输入 WiFi 名称。"}
