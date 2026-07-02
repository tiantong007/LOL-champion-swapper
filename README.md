# ⚡ 海克斯大乱斗秒换英雄 v1.3

英雄联盟大乱斗模式选人阶段自动绕过冷却秒换英雄工具。

## 功能

- 自动识别 LCU（英雄联盟客户端）连接
- 选人阶段展示候补席所有英雄（含头像）
- 一键秒换英雄（跳过冷却）
- 快速选英雄（选人动作未完成时自动补选）
- 自动接受对局（可选开关）
- 实时显示连接状态、游戏阶段
- 内置操作日志

## 使用

### 直接运行

```bash
pip install -r requirements.txt
python swapper.py
```

或双击 `start.bat`（自动安装依赖）

### 打包为 exe

```bash
pip install pyinstaller
python -m PyInstaller "海克斯大乱斗秒换英雄.spec" --clean
```

生成的可执行文件位于 `dist/海克斯大乱斗秒换英雄.exe`。

## 依赖

- Python 3.7+
- requests
- psutil
- pywebview
- PyInstaller（仅打包时需要）

```bash
pip install -r requirements.txt
```

## 原理

通过 `psutil` 直接读取 `LeagueClientUx.exe` 进程命令行，提取 `--remoting-auth-token` 和 `--app-port`，利用 LCU API 发送 HTTP 请求绕过客户端的换英雄冷却。无需管理员权限。

## ⚠️ 注意事项

- **无需管理员权限**（直接读取进程命令行）
- 仅在大乱斗（ARAM）模式有效
- 使用风险自负，可能违反腾讯/拳头游戏用户协议
