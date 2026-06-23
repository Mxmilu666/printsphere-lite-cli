# PrintSphere Lite CLI

PrintSphere Lite 的**命令行（CLI）移植版本**，适用于 **Linux / macOS**（也可在 Windows 上运行）。

> 原项目（含设备模型、固件与 Windows 网页版配置工具）见 MakerWorld：
> <https://makerworld.com.cn/zh/models/2587841-cheng-ben-25-printsphere-litetuo-zhu-da-yin-zhuang>

原版配置工具是一个基于 Node.js 的本地 HTTP + 网页应用，串口通信依赖 **Windows PowerShell**，因此只能在 Windows 上使用。本仓库的 `cli.py` 把同一套配置流程改写成跨平台的终端交互程序：串口通信改用 [`pyserial`](https://pypi.org/project/pyserial/)，去掉了 HTTP 服务和内嵌网页，让 Linux / macOS 用户也能配置自己的 PrintSphere Lite 设备。

---

## 安装

```bash
git clone <本仓库地址>
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

---

## 使用

```bash
python3 cli.py
```

启动后是一个终端菜单：

```
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
```

**推荐顺序**：`3 选择串口` → `2 检测 ESP` → `1 登录` → `4/5 配置 WiFi` → `6 刷新打印机` → `7 选择打印机`。

---

## 配置与数据文件

首次运行会在程序所在目录下自动创建 `data/`：

- `data/config.json` —— 当前配置（WiFi、云 token、打印机、ESP 地址 / 串口、亮度等）
- `data/devices.json` —— 最近一次读取到的打印机列表

---

## 致谢

- 设备、固件与原版配置工具来自 MakerWorld 上的 PrintSphere Lite 项目：
  <https://makerworld.com.cn/zh/models/2587841-cheng-ben-25-printsphere-litetuo-zhu-da-yin-zhuang>
- 本仓库仅为方便 Linux / macOS 用户使用而做的 CLI 移植。

---

## 许可证

本项目由 MIT 许可证开源