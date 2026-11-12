# PrintSphere Lite CLI

PrintSphere Lite 的**命令行（CLI）移植版本**，适用于 **Linux / macOS**（也可在 Windows 上运行）。

> 原项目（含设备模型、固件与 Windows 网页版配置工具）见 MakerWorld：
> <https://makerworld.com.cn/zh/models/2587841-cheng-ben-25-printsphere-litetuo-zhu-da-yin-zhuang>

原版配置工具是一个基于 Node.js 的本地 HTTP + 网页应用，串口通信依赖 **Windows PowerShell**，因此只能在 Windows 上使用。本仓库的 `cli.py` 把同一套配置流程改写成跨平台的终端交互程序：串口通信改用 [`pyserial`](https://pypi.org/project/pyserial/)，去掉了 HTTP 服务和内嵌网页，让 Linux / macOS 用户也能配置自己的 PrintSphere Lite 设备。

当前版本：`cli-v0.4.71`，对齐交付包 `backend-v0.4.71-ui-clean` 的配置功能。固件继续使用交付包里的 ESP8266 `.bin`；CLI 不修改 MQTT 数据解析或屏幕绘制，也不负责烧录固件。

- 共用云账号，每台 ESP 独立保存 WiFi、打印机、屏幕和 HTTP/串口设置；自动迁移旧单 ESP 配置。
- USB 优先、HTTP 兜底；每次配置前复核设备身份，多台 ESP 同时连接或身份不符时禁止写入。
- 支持经典/信息面板布局、0–100 亮度和别名点阵；最新信息面板隐藏别名，但保留配置协议。
- 支持 ESP/电脑 WiFi 扫描、局域网设备发现、打印机列表同步与高级 HTTP 设置。


---

## 安装

```bash
# 在项目目录中执行；需要 Python 3.9 或更新版本
cd printsphere-lite-cli

# 建议使用虚拟环境
python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### Linux 串口权限

普通用户访问 `/dev/ttyUSB0` 可能需要加入 `dialout` 组（Arch/部分发行版为 `uucp`）：

```bash
sudo usermod -aG dialout $USER   # 之后重新登录生效
```

### 平台依赖

- macOS：requirements 自动安装 CoreWLAN 的 PyObjC 绑定。电脑 WiFi 扫描需要给终端开启位置服务；无权限时仍可通过 ESP 串口扫描或手动填写 SSID。
- Linux：电脑 WiFi 扫描需要 NetworkManager 的 `nmcli`；缺少时使用 ESP 扫描或手动填写。局域网发现使用 psutil 枚举网卡。
- Windows：PowerShell 激活虚拟环境用 `.venv\Scripts\Activate.ps1`；电脑扫描用系统 `netsh`，串口仍用 pyserial。
- 别名点阵用 Pillow 和本机字体。中文需要系统 CJK 字体（例如 Noto Sans CJK）；可设置 `PRINTSPHERE_FONT` 为字体文件路径。没有可用字体时明确报错，不发送空点阵冒充成功。


---

## 使用

```bash
python3 cli.py
```

启动后是一个终端菜单：

```
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
```

**推荐顺序**：只连接一台 ESP → `2 检测 ESP` → `1 登录` → `4/5 配置 WiFi` → `6 刷新打印机` → `7 选择打印机` → 按需用 `8` 设置屏幕。

交互终端启动时自动检测设备，等待菜单输入时每 8 秒检查串口变化；执行操作或填写密码期间不进行后台串口操作。拔换设备后可随时用 `2` 强制重新识别。身份不符会阻止旧配置写入新设备，需重新检测后再选打印机。

离线 USB 的已配置设备可在 `11` 选择档案、`12` 设置 HTTP 地址（默认 8081）；写入前通过 `/api/status` 验证身份。局域网搜索通过 `9`，只扫描本机私网 IPv4 的 /24 网段。

亮度 `0` 关闭背光，`1–100` 手动调节，没有旧版自动降亮。别名输入 `-` 清空，留空保留。`5` 和 `8` 可以选择只保存、不写设备；`13` 重新发送完整云配置。

```bash
python3 cli.py --version
python3 -m unittest -v test_cli
```

---

## 配置与数据文件

首次运行会在程序所在目录下自动创建 `data/`：

- `data/config.json` —— 公共云配置、默认 WiFi、当前设备 ID 和每台 ESP 的独立档案
- `data/devices.json` —— 最近一次读取到的打印机列表
- `data/device-history.jsonl` —— 云打印机列表快照历史

这些文件含明文 WiFi 密码、云 token 或打印机访问码；不要上传、截图或发给他人，整个 `data/` 已在 `.gitignore` 中排除。CLI 与原网页工具默认使用各自目录下的数据，不并发共享配置文件。退出或输入流关闭后会结束程序，不在后台保留串口。

旧版 `config.json` 会迁移到默认档案；首次识别真实 ESP 时继承原配置。新设备只继承默认 WiFi，不复制其他设备的打印机/屏幕；切换档案会恢复该设备自己的完整设置。明确报告空 IP 的设备会清空历史地址，避免串台。

### 变更记录

- **cli-v0.4.71**：移植最新设备档案与防误写流程、热插拔检测、HTTP 身份确认、局域网发现、完整机型映射、布局/别名点阵和 0–100 亮度；增加跨平台电脑 WiFi 扫描、打印机快照历史、隐藏敏感字段的配置展示。串口使用紧凑 UTF-8 JSON，支持跨读取超时分片回包；HTTP 保留零亮度和别名清空参数。原子保存失败不删除旧配置。
- **cli-v0.4.57-printer-sync**：原单 ESP 命令行版本，支持登录、WiFi、打印机同步和四档亮度。

终端菜单取代原网页和 HTTP 控制 API；不生成仅供 `.bat` 打开浏览器使用的 `server-state.json`，不引入 Windows PowerShell 依赖。

---

## 致谢

- 设备、固件与原版配置工具来自 MakerWorld 上的 PrintSphere Lite 项目：
  <https://makerworld.com.cn/zh/models/2587841-cheng-ben-25-printsphere-litetuo-zhu-da-yin-zhuang>
- 本仓库仅为方便 Linux / macOS 用户使用而做的 CLI 移植。

---

## 许可证

本项目由 MIT 许可证开源