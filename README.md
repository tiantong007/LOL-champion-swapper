# ⚡ 海克斯大乱斗秒换英雄 v1.3

英雄联盟大乱斗（ARAM）选人阶段秒换英雄工具。绕过客户端换英雄冷却，通过 LCU API 直接操作，无需管理员权限。

## 功能

- **秒换英雄** — 一键交换候补席英雄，跳过客户端冷却计时
- **英雄头像展示** — 选人阶段显示所有候补英雄头像及名称
- **自动接受对局** — 可选开关，检测到匹配成功后自动点击接受
- **实时状态** — 显示当前连接状态、游戏阶段（大厅/匹配中/选人等）
- **操作日志** — 记录所有换英雄、选英雄操作
- **自适应轮询** — 选人阶段高频轮询（0.2s），其他阶段低频（1.0s）
- **自动重连** — 客户端重启后自动检测新端口和 Token

## 使用

### 直接运行

```bash
pip install -r requirements.txt
python swapper.py
```

或双击 `start.bat`（自动安装依赖并启动）。

启动后会打开桌面窗口，选人阶段点击英雄头像即可秒换。

### 打包为 exe

```bash
pip install pyinstaller
python -m PyInstaller "海克斯大乱斗秒换英雄.spec" --clean
```

生成的可执行文件位于 `dist/海克斯大乱斗秒换英雄v1.3.exe`。

## 依赖

- Python 3.7+
- requests — HTTP 请求
- psutil — 进程检测
- pywebview — 桌面窗口

```bash
pip install -r requirements.txt
```

## 原理

1. 通过 `psutil` 遍历进程列表，找到 `LeagueClientUx.exe` 并解析其命令行参数，提取 `--remoting-auth-token` 和 `--app-port`
2. 使用 Basic Auth 连接本地 LCU API（`https://127.0.0.1:{port}`）
3. 选人阶段调用 `/lol-champ-select/v1/session/bench/swap/{championId}` 接口实现秒换
4. 内嵌 HTTP 服务器（端口 9753）提供 Web UI，pywebview 加载显示

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/state` | GET | 获取当前轮询状态（连接、选人、英雄列表） |
| `/api/icon/{id}` | GET | 获取英雄头像图片 |
| `/api/swap/{id}` | POST | 秒换指定英雄 |
| `/api/quick-pick/{cid}/{aid}` | POST | 快速选择英雄（补选） |
| `/api/settings/auto-accept` | POST | 开关自动接受对局 |
| `/api/logs` | GET | 获取操作日志 |

## ⚠️ 注意事项

- **无需管理员权限**（直接读取进程命令行参数）
- 仅在大乱斗（ARAM）模式的选人阶段有效
- 客户端未启动时会显示"等待英雄联盟客户端..."
- 使用风险自负，可能违反腾讯/拳头游戏用户协议
