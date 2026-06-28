#!/usr/bin/env python3
"""
League of Legends ARAM Bench Champion Swapper
Bypass client cooldown - instant bench swap during champ select
"""

import base64
import ctypes
import ctypes.wintypes
import json
import os
import re
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

SWAPPER_PORT = 9753
POLL_INTERVAL = 1.0
LCU_BASE_URL = None
LCU_AUTH = None
LCU_HEADERS = {}
CHAMPION_NAMES = {}
SESSION_CACHE = None
CONNECTED = False
GAMEFLOW_PHASE = None
LAST_ERROR = None
SWAP_LOG = []
NEED_ADMIN = False

WMIC_PATH = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'),
                         'System32', 'wbem', 'WMIC.exe')

# ============================================================
# LCU Discovery
# ============================================================
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def elevate():
    """Restart the script as administrator"""
    if not is_admin():
        print('[Swapper] WMI command line reading requires admin rights.')
        print('[Swapper] Attempting to restart as administrator...')
        try:
            script = (
                'Start-Process python -ArgumentList '
                f'"\\"{sys.argv[0]}\\"" '
                '-Verb RunAs'
            )
            subprocess.run(['powershell', '-NoProfile', '-Command', script],
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
            print('[Swapper] Elevation requested. If UAC prompt appears, please accept it.')
            sys.exit(0)
        except Exception:
            print('[Swapper] Elevation failed. You can manually run as admin:')
            print(f'         Right-click "start.bat" -> Run as administrator')


def find_lcu_via_ctypes():
    """Use ctypes to get exe path (no elevation needed), then read lockfile"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    for proc_name, pid in _get_pids_fast():
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(260)
            sz = ctypes.wintypes.DWORD(260)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(sz)):
                exe_path = buf.value
                for d in [os.path.dirname(exe_path), os.path.dirname(os.path.dirname(exe_path))]:
                    lf = os.path.join(d, 'lockfile')
                    if os.path.exists(lf) and os.path.getsize(lf) > 0:
                        with open(lf, 'r') as f:
                            content = f.read().strip()
                        parts = content.split(':')
                        if len(parts) >= 4:
                            return {
                                'port': int(parts[1]),
                                'auth_token': parts[2],
                                'pid': int(parts[0])
                            }
                        return None
        finally:
            kernel32.CloseHandle(handle)
    return None


def find_lcu_via_logs():
    """Fallback for Tencent/WeGame: parse --remoting-auth-token from LCU log files"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    for proc_name, pid in _get_pids_fast():
        if proc_name != 'LeagueClientUx.exe':
            continue
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(260)
            sz = ctypes.wintypes.DWORD(260)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(sz)):
                lcu_dir = os.path.dirname(buf.value)
                for logs_candidate in (
                    lcu_dir,
                    os.path.join(lcu_dir, 'Logs', 'LeagueClient Logs'),
                    os.path.join(os.path.dirname(lcu_dir), 'Logs', 'LeagueClient Logs'),
                ):
                    if os.path.isdir(logs_candidate):
                        log_files = [f for f in os.listdir(logs_candidate)
                                     if f.endswith('.log') and 'LeagueClientUx' in f and 'Helper' not in f]
                        log_files.sort(reverse=True)
                        for log_file in log_files[:5]:
                            log_path = os.path.join(logs_candidate, log_file)
                            try:
                                with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
                                    for line in fh:
                                        if 'Command line arguments:' in line:
                                            m_token = re.search(r'--remoting-auth-token=([\w\-_]+)', line)
                                            m_port = re.search(r'--app-port=(\d+)', line)
                                            if m_token and m_port:
                                                return {'port': int(m_port.group(1)),
                                                        'auth_token': m_token.group(1), 'pid': pid}
                            except (OSError, PermissionError):
                                continue
        finally:
            kernel32.CloseHandle(handle)
    return None


def _get_pids_fast():
    """Fast process enumeration via ctypes Toolhelp32Snapshot"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    TH32CS_SNAPPROCESS = 0x00000002
    target_names = ['LeagueClientUx.exe', 'LeagueClient.exe']

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ('dwSize', ctypes.c_ulong),
            ('cntUsage', ctypes.c_ulong),
            ('th32ProcessID', ctypes.c_ulong),
            ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
            ('th32ModuleID', ctypes.c_ulong),
            ('cntThreads', ctypes.c_ulong),
            ('th32ParentProcessID', ctypes.c_ulong),
            ('pcPriClassBase', ctypes.c_long),
            ('dwFlags', ctypes.c_ulong),
            ('szExeFile', ctypes.c_wchar * 260),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return []

    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(pe)):
            return []
        pids = []
        while True:
            if pe.szExeFile in target_names:
                pids.append((pe.szExeFile, pe.th32ProcessID))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(pe)):
                break
        return pids
    finally:
        kernel32.CloseHandle(snapshot)


def find_lcu_via_powershell():
    script = '''
    $processes = Get-CimInstance -ClassName Win32_Process |
        Where-Object { $_.Name -match 'LeagueClientUx\\.exe$|^LeagueClient\\.exe$' }
    foreach ($p in $processes) {
        [PSCustomObject]@{ Pid = $p.ProcessId; CmdLine = $p.CommandLine }
    }
    '''
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', script],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode != 0:
            return None, False

        all_empty = True
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line.startswith('@{') and 'Pid=' in line:
                parts = line.strip('@{}').split('; ')
                cmdline = None
                for p in parts:
                    if p.startswith('CmdLine='):
                        cmdline = p.split('=', 1)[1]
                        if cmdline.startswith(' ' * 32):
                            cmdline = cmdline[32:]
                        cmdline = cmdline.strip()
                        break
                if cmdline:
                    all_empty = False
                    parsed = parse_lcu_cmdline(cmdline)
                    if parsed:
                        return parsed, False
        if all_empty:
            return None, True  # WMI returned but cmdline was empty
        return None, False
    except Exception:
        return None, False


def find_lcu_via_wmic():
    try:
        for name in ['LeagueClientUx.exe', 'LeagueClient.exe']:
            result = subprocess.run(
                [WMIC_PATH, 'process', 'where', f"name like '%{name}%'", 'get', 'CommandLine'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    line = line.strip()
                    if not line or line.upper() == 'COMMANDLINE':
                        continue
                    parsed = parse_lcu_cmdline(line)
                    if parsed:
                        return parsed
    except Exception:
        pass
    return None


def parse_lcu_cmdline(cmdline):
    port_match = re.search(r'--app-port=([0-9]+)', cmdline)
    auth_match = re.search(r'--remoting-auth-token=([\w\-_]+)', cmdline)
    pid_match = re.search(r'--app-pid=([0-9]+)', cmdline)
    if port_match and auth_match:
        return {
            'port': int(port_match.group(1)),
            'auth_token': auth_match.group(1),
            'pid': int(pid_match.group(1)) if pid_match else 0
        }
    return None


def discover_lcu():
    global LCU_BASE_URL, LCU_AUTH, LCU_HEADERS, NEED_ADMIN

    info, wmi_blocked = find_lcu_via_powershell()
    if not info and not wmi_blocked:
        info = find_lcu_via_wmic()
    if not info:
        info = find_lcu_via_ctypes()
    if not info:
        info = find_lcu_via_logs()
    if not info:
        if wmi_blocked and not is_admin():
            NEED_ADMIN = True
        return False

    LCU_AUTH = info
    LCU_BASE_URL = f'https://127.0.0.1:{info["port"]}'
    token = base64.b64encode(f'riot:{info["auth_token"]}'.encode()).decode()
    LCU_HEADERS = {
        'Authorization': f'Basic {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    try:
        data = lcu_get('/riotclient/auth-token')
        return data is not None
    except Exception:
        return False


# ============================================================
# LCU HTTP Client
# ============================================================
ssl_ctx = ssl._create_unverified_context()

def _lcu_request(method, path, body=None):
    if not LCU_BASE_URL:
        return None
    url = f'{LCU_BASE_URL}{path}'
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=LCU_HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        raise

def lcu_get(path): return _lcu_request('GET', path)
def lcu_post(path, body=None): return _lcu_request('POST', path, body)
def lcu_patch(path, body=None): return _lcu_request('PATCH', path, body)


# ============================================================
# Champion Name & Icon
# ============================================================
def fetch_champion_names():
    global CHAMPION_NAMES
    try:
        grid = lcu_get('/lol-champ-select/v1/all-grid-champions')
        if grid and isinstance(grid, list):
            CHAMPION_NAMES = {c['id']: c['name'] for c in grid}
            return True
    except Exception:
        pass
    return False

def fetch_champion_icon(champion_id):
    try:
        path = f'/lol-game-data/assets/v1/champion-icons/{champion_id}.png'
        url = f'{LCU_BASE_URL}{path}'
        req = urllib.request.Request(url, headers=LCU_HEADERS)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as resp:
            return resp.read()
    except Exception:
        return None


# ============================================================
# Core Swapper Logic
# ============================================================
def swap_bench_champion(champion_id):
    try:
        lcu_post(f'/lol-champ-select/v1/session/bench/swap/{champion_id}')
        log_swap(f'Swapped to champion #{champion_id} ({CHAMPION_NAMES.get(champion_id, "?")})')
        return True
    except Exception as e:
        log_swap(f'Swap failed: champion #{champion_id} - {str(e)}')
        return False


def quick_pick_champion(champion_id, action_id):
    try:
        lcu_patch(f'/lol-champ-select/v1/session/actions/{action_id}', {
            'championId': champion_id, 'completed': True, 'type': 'pick'
        })
        log_swap(f'Picked champion #{champion_id} ({CHAMPION_NAMES.get(champion_id, "?")})')
        return True
    except Exception as e:
        log_swap(f'Pick failed: champion #{champion_id} - {str(e)}')
        return False


def log_swap(msg):
    global SWAP_LOG
    ts = time.strftime('%H:%M:%S')
    entry = f'[{ts}] {msg}'
    SWAP_LOG.append(entry)
    if len(SWAP_LOG) > 100:
        SWAP_LOG = SWAP_LOG[-50:]
    print(entry)


# ============================================================
# State Polling
# ============================================================
POLLING_RESULT = {'connected': False, 'inChampSelect': False, 'champions': [],
                  'phase': None, 'error': 'Starting...', 'needAdmin': False}


def poll_state():
    global CONNECTED, GAMEFLOW_PHASE, SESSION_CACHE, POLLING_RESULT, LAST_ERROR
    global LCU_BASE_URL, LCU_AUTH, LCU_HEADERS, NEED_ADMIN

    while True:
        try:
            if not LCU_BASE_URL:
                if discover_lcu():
                    CONNECTED = True
                    fetch_champion_names()
                    print(f'[Swapper] Connected to LCU at {LCU_BASE_URL}')
                else:
                    error_msg = 'Waiting for League Client...'
                    if NEED_ADMIN and not is_admin():
                        error_msg = ('Cannot read LCU info - need admin rights. '
                                     'Right-click start.bat -> Run as administrator')
                    POLLING_RESULT = {
                        'connected': False, 'inChampSelect': False, 'phase': None,
                        'champions': [], 'error': error_msg, 'needAdmin': NEED_ADMIN and not is_admin()
                    }
                    time.sleep(POLL_INTERVAL)
                    continue

            try:
                gf = lcu_get('/lol-gameflow/v1/session')
                if gf and 'phase' in gf:
                    GAMEFLOW_PHASE = gf['phase']
                else:
                    GAMEFLOW_PHASE = None
            except Exception:
                GAMEFLOW_PHASE = None

            session = None
            try:
                session = lcu_get('/lol-champ-select/v1/session')
            except Exception:
                session = None

            SESSION_CACHE = session

            if session and 'benchChampions' in session:
                bench = session.get('benchChampions', [])
                champ_ids = [b['championId'] for b in bench if b.get('championId')]
                pickable = set(session.get('pickableChampionIds', []))
                disabled = set(session.get('bannableChampionIds', []))
                try:
                    disabled_ids = lcu_get('/lol-champ-select/v1/disabled-champion-ids')
                    if disabled_ids:
                        disabled = set(disabled_ids)
                except Exception:
                    pass
                my_team = session.get('myTeam', [])
                local_cell = session.get('localPlayerCellId', 0)
                actions_flat = [a for group in session.get('actions', []) for a in group]
                first_pick_action = None
                for a in actions_flat:
                    if a.get('type') == 'pick' and not a.get('completed') and a.get('actorCellId') == local_cell:
                        first_pick_action = a
                        break
                timer_phase = session.get('timer', {}).get('phase', '')
                POLLING_RESULT = {
                    'connected': True, 'inChampSelect': True, 'phase': timer_phase,
                    'benchEnabled': session.get('benchEnabled', False),
                    'champions': [{'id': cid, 'name': CHAMPION_NAMES.get(cid, f'Champion {cid}'),
                                   'pickable': cid in pickable, 'disabled': cid in disabled}
                                  for cid in champ_ids],
                    'pickableIds': list(pickable),
                    'selfChampion': next((m.get('championId', 0) for m in my_team if m.get('cellId') == local_cell), 0),
                    'firstPickAction': {'id': first_pick_action['id'],
                                        'isInProgress': first_pick_action.get('isInProgress', False)}
                    if first_pick_action else None,
                    'timerPhase': timer_phase, 'error': None, 'needAdmin': False
                }
            else:
                POLLING_RESULT = {
                    'connected': CONNECTED, 'inChampSelect': False, 'phase': GAMEFLOW_PHASE,
                    'benchEnabled': False, 'champions': [], 'pickableIds': [],
                    'selfChampion': 0, 'firstPickAction': None, 'timerPhase': None,
                    'error': None, 'needAdmin': False
                }

            LAST_ERROR = None

        except Exception as e:
            estr = str(e)
            if 'Connection refused' in estr or 'Connect error' in estr or 'SSL' in estr:
                CONNECTED = False
                LCU_BASE_URL = None
                LCU_AUTH = None
                print('[Swapper] Lost connection to LCU, will retry...')
            elif LAST_ERROR is None or estr != LAST_ERROR:
                LAST_ERROR = estr
                print(f'[Swapper] Poll error: {e}')
            POLLING_RESULT = {
                'connected': CONNECTED, 'inChampSelect': False, 'phase': None,
                'champions': [], 'error': estr, 'needAdmin': False
            }

        time.sleep(POLL_INTERVAL)


# ============================================================
# Web Server
# ============================================================
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ARAM 秒换英雄</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0e14;color:#c8cbd1;min-height:100vh}
  .container{max-width:800px;margin:0 auto;padding:20px}
  .header{display:flex;align-items:center;justify-content:space-between;padding:16px 0;border-bottom:1px solid #1e232c;margin-bottom:24px}
  .header h1{font-size:20px;font-weight:700;color:#e0e3e8}
  .header h1 span{color:#f59e0b}
  .status-bar{display:flex;align-items:center;gap:10px;padding:10px 16px;border-radius:8px;margin-bottom:20px;font-size:13px}
  .status-bar.connected{background:#0d2818;color:#4ade80}
  .status-bar.disconnected{background:#2d1b1b;color:#f87171}
  .status-bar.waiting{background:#1e1b0d;color:#fbbf24}
  .status-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
  .status-bar.connected .status-dot{background:#4ade80}
  .status-bar.disconnected .status-dot{background:#f87171}
  .status-bar.waiting .status-dot{background:#fbbf24}
  .info-row{display:flex;gap:12px;margin-bottom:24px}
  .info-card{flex:1;background:#111820;border-radius:8px;padding:12px 16px;border:1px solid #1e232c}
  .info-card .label{font-size:11px;color:#6b7280;margin-bottom:4px}
  .info-card .value{font-size:16px;font-weight:600}
  .section-title{font-size:14px;font-weight:600;color:#9ca3af;margin-bottom:12px;display:flex;align-items:center;gap:8px}
  .champ-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:12px;margin-bottom:24px}
  .champ-card{background:#111820;border:1px solid #1e232c;border-radius:10px;padding:12px;text-align:center;cursor:pointer;transition:all .15s ease;user-select:none}
  .champ-card:hover{background:#1a2430;border-color:#f59e0b;transform:translateY(-2px);box-shadow:0 4px 12px rgba(245,158,11,.15)}
  .champ-card:active{transform:scale(.96)}
  .champ-card .icon{width:64px;height:64px;border-radius:50%;margin:0 auto 8px;display:block;border:2px solid #1e232c;background:#0d1117}
  .champ-card .name{font-size:13px;font-weight:500;color:#e0e3e8}
  .champ-card.picking .icon{border-color:#22c55e}
  .champ-card .badge{display:inline-block;font-size:10px;padding:1px 6px;border-radius:4px;margin-top:4px}
  .champ-card .badge.yours{background:#0d2818;color:#4ade80}
  .champ-card .badge.off-cd{background:#1e1b0d;color:#fbbf24}
  .empty-state{text-align:center;padding:48px 16px;color:#6b7280}
  .empty-state .icon-big{font-size:48px;margin-bottom:12px}
  .empty-state p{font-size:14px}
  .admin-banner{background:#2d1b1b;border:1px solid #f87171;border-radius:8px;padding:16px;margin-bottom:16px;text-align:center}
  .admin-banner p{color:#f87171;font-size:14px;margin-bottom:12px}
  .admin-banner .btn{background:#f87171;color:#0a0e14;border:none;padding:8px 20px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer}
  .admin-banner .btn:hover{opacity:.85}
  .log-section{margin-top:24px}
  .log-section .section-title{cursor:pointer;user-select:none}
  .log-box{background:#090d13;border:1px solid #1e232c;border-radius:6px;padding:12px;max-height:200px;overflow-y:auto;font-family:monospace;font-size:12px;line-height:1.6}
  .log-box::-webkit-scrollbar{width:4px}
  .log-box::-webkit-scrollbar-track{background:#090d13}
  .log-box::-webkit-scrollbar-thumb{background:#1e232c;border-radius:2px}
  .log-entry{color:#6b7280}
  .log-entry.success{color:#4ade80}
  .log-entry.error{color:#f87171}
  .btn{background:#f59e0b;color:#0a0e14;border:none;padding:8px 20px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s}
  .btn:hover{opacity:.85}
  .btn:disabled{opacity:.4;cursor:not-allowed}
  .btn.secondary{background:#1e232c;color:#c8cbd1}
  .btn.secondary:hover{background:#2a3340}
  .quick-pick-section{background:#111820;border:1px solid #1e232c;border-radius:10px;padding:16px;margin-bottom:24px;display:none}
  .quick-pick-section h3{font-size:13px;color:#22c55e;margin-bottom:8px}
  @media(max-width:600px){.champ-grid{grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px}.champ-card .icon{width:48px;height:48px}.info-row{flex-direction:column}}
</style>
</head>
<body>
<div class="container" id="app">
  <div class="header">
    <h1>&#x26A1; <span>ARAM</span> 秒换英雄</h1>
    <div><button class="btn secondary" onclick="window.close()">关闭</button></div>
  </div>

  <div id="status-bar" class="status-bar waiting">
    <div class="status-dot"></div>
    <span id="status-text">正在连接 LCU...</span>
  </div>

  <div id="admin-banner" class="admin-banner" style="display:none">
    <p>需要管理员权限才能读取 LCU 连接信息，请以管理员身份重新运行</p>
    <p style="font-size:13px;color:#fca5a5;margin-bottom:8px">右键 start.bat → 以管理员身份运行</p>
  </div>

  <div class="info-row">
    <div class="info-card"><div class="label">游戏阶段</div><div class="value" id="phase-value">-</div></div>
    <div class="info-card"><div class="label">模式</div><div class="value" id="mode-value">-</div></div>
    <div class="info-card"><div class="label">当前英雄</div><div class="value" id="self-champ-value">-</div></div>
  </div>

  <div class="section-title">&#x1FA91; 板凳英雄 <span id="champ-count" style="color:#6b7280;font-weight:400"></span></div>
  <div id="champ-grid" class="champ-grid">
    <div class="empty-state"><p id="empty-text">等待进入选人阶段...</p></div>
  </div>

  <div class="log-section">
    <div class="section-title" onclick="toggleLog()">&#x1F4CB; 操作日志</div>
    <div id="log-box" class="log-box"><div class="log-entry">工具已启动</div></div>
  </div>
</div>

<script>
const POLL_URL = '/api/state';
function poll(){
  fetch(POLL_URL).then(r=>r.json()).then(updateUI).catch(()=>{});
  setTimeout(poll, 800);
}

function updateUI(data){
  const sb = document.getElementById('status-bar');
  const st = document.getElementById('status-text');
  const ab = document.getElementById('admin-banner');

  if (data.needAdmin) {
    sb.className = 'status-bar disconnected';
    st.textContent = '需要管理员权限';
    ab.style.display = 'block';
    document.getElementById('phase-value').textContent = '-';
    document.getElementById('mode-value').textContent = '-';
    document.getElementById('self-champ-value').textContent = '-';
    document.getElementById('champ-grid').innerHTML = '<div class="empty-state"><p>' + data.error + '</p></div>';
    return;
  }
  ab.style.display = 'none';

  if (!data.connected) {
    sb.className = 'status-bar disconnected';
    st.textContent = data.error || '未连接';
    document.getElementById('phase-value').textContent = '断开';
    document.getElementById('mode-value').textContent = '-';
    document.getElementById('self-champ-value').textContent = '-';
    document.getElementById('champ-grid').innerHTML = '<div class="empty-state"><p>' + (data.error||'等待游戏客户端...') + '</p></div>';
    return;
  }

  if (!data.inChampSelect) {
    sb.className = 'status-bar waiting';
    st.textContent = '已连接，等待进入选人阶段...';
    document.getElementById('phase-value').textContent = data.phase || '大厅';
    document.getElementById('mode-value').textContent = '-';
    document.getElementById('self-champ-value').textContent = '-';
    document.getElementById('champ-grid').innerHTML = '<div class="empty-state"><p>等待进入选人阶段...</p></div>';
    return;
  }

  sb.className = 'status-bar connected';
  st.textContent = '已连接 - 选人中';
  document.getElementById('phase-value').textContent = data.timerPhase || '-';
  document.getElementById('mode-value').textContent = data.benchEnabled ? '大乱斗 (板凳模式)' : '普通模式';
  const sc = data.selfChampion || 0;
  document.getElementById('self-champ-value').textContent = sc > 0 ? getName(sc, data) : '未选择';

  if (data.benchEnabled) {
    renderBench(data.champions, sc);
  } else {
    document.getElementById('champ-grid').innerHTML = '<div class="empty-state"><p>非板凳模式，仅限大乱斗使用</p></div>';
  }
}

function getName(id, data){
  if (data.champions) {
    const c = data.champions.find(x => x.id === id);
    if (c) return c.name;
  }
  return '#' + id;
}

function renderBench(champs, selfChamp){
  const grid = document.getElementById('champ-grid');
  if (!champs || champs.length === 0) {
    grid.innerHTML = '<div class="empty-state"><p>板凳上没有英雄</p></div>';
    document.getElementById('champ-count').textContent = '';
    return;
  }
  document.getElementById('champ-count').textContent = '(' + champs.length + '个)';
  grid.innerHTML = champs.map(c => {
    const isYours = c.id === selfChamp;
    return '<div class="champ-card' + (isYours?' picking':'') + '" onclick="swapChamp(' + c.id + ')" title="点击秒换">'
      + '<img class="icon" src="/api/icon/' + c.id + '" alt="' + c.name + '" onerror="this.style.display=\\'none\\'">'
      + '<div class="name">' + c.name + '</div>'
      + (isYours ? '<div class="badge yours">当前</div>' : '<div class="badge off-cd">秒换</div>')
      + '</div>';
  }).join('');
}

async function swapChamp(id){
  try{
    const r = await fetch('/api/swap/' + id, {method:'POST'});
    const d = await r.json();
    addLog(d.success?'success':'error', (d.success?'&#x2713; 换到: ':'&#x2717; 失败: ')+(d.name||'#'+id));
  }catch(e){
    addLog('error', '&#x2717; 请求失败: '+e.message);
  }
}

function addLog(type, msg){
  const box = document.getElementById('log-box');
  const e = document.createElement('div');
  e.className = 'log-entry '+type;
  e.textContent = '['+new Date().toLocaleTimeString()+'] '+msg;
  box.appendChild(e);
  box.scrollTop = box.scrollHeight;
}

function toggleLog(){
  const box = document.getElementById('log-box');
  box.style.display = box.style.display==='none'?'block':'none';
}

poll();
</script>
</body>
</html>'''

class SwapHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            self._ok('text/html', HTML_TEMPLATE.encode('utf-8'))
        elif path == '/api/state':
            self._ok('application/json', json.dumps(POLLING_RESULT).encode('utf-8'))
        elif path.startswith('/api/icon/'):
            try:
                cid = int(path.split('/api/icon/')[1])
                icon = fetch_champion_icon(cid)
                if icon:
                    self._ok('image/png', icon, cache=3600)
                    return
            except ValueError:
                pass
            self._err(404)
        elif path == '/api/logs':
            self._ok('application/json', json.dumps(SWAP_LOG[-30:]).encode('utf-8'))
        else:
            self._err(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith('/api/swap/'):
            try:
                cid = int(path.split('/api/swap/')[1])
                ok = swap_bench_champion(cid)
                self._ok('application/json', json.dumps(
                    {'success': ok, 'name': CHAMPION_NAMES.get(cid, f'#{cid}'),
                     'error': None if ok else 'See log'}).encode('utf-8'))
            except ValueError:
                self._ok('application/json', json.dumps({'success': False, 'error': 'Invalid ID'}).encode('utf-8'))
        elif path.startswith('/api/quick-pick/'):
            try:
                parts = path.split('/api/quick-pick/')[1].split('/')
                cid, aid = int(parts[0]), int(parts[1])
                ok = quick_pick_champion(cid, aid)
                self._ok('application/json', json.dumps(
                    {'success': ok, 'name': CHAMPION_NAMES.get(cid, f'#{cid}')}).encode('utf-8'))
            except (ValueError, IndexError):
                self._ok('application/json', json.dumps({'success': False, 'error': 'Bad params'}).encode('utf-8'))
        else:
            self._err(404)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _ok(self, content_type, data, cache=0):
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        if cache:
            self.send_header('Cache-Control', f'max-age={cache}')
        else:
            self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code):
        self.send_response(code)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_server():
    server = HTTPServer(('127.0.0.1', SWAPPER_PORT), SwapHandler)
    print(f'[Web UI] http://127.0.0.1:{SWAPPER_PORT}')
    print(f'[Info] Open in browser')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down...')
        server.shutdown()


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    print('=' * 50)
    print('  ARAM Instant Champion Swapper')
    print('=' * 50)

    poll_thread = threading.Thread(target=poll_state, daemon=True)
    poll_thread.start()
    time.sleep(1)

    try:
        webbrowser.open(f'http://127.0.0.1:{SWAPPER_PORT}')
    except Exception:
        pass

    run_server()


if __name__ == '__main__':
    main()
