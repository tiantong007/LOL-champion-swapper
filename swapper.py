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
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests

try:
    import webview
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False

SWAPPER_PORT = 9753
POLL_INTERVAL = 0.3

AUTO_ACCEPT_ENABLED = False
AUTO_ACCEPT_TRIGGERED = False
ACCEPT_DELAY = 0.8
LCU_BASE_URL = None
LCU_HEADERS = {}
CHAMPION_NAMES = {}
CONNECTED = False
GAMEFLOW_PHASE = None
LAST_ERROR = None

SWAP_LOG = []
_LCU_SESSION = None

PHASE_TRANSLATIONS = {
    'Lobby': '大厅',
    'Matchmaking': '匹配中',
    'ReadyCheck': '接受对局',
    'ChampSelect': '选人中',
    'GameStart': '加载中',
    'InProgress': '游戏中',
    'WaitingForStats': '等待统计',
    'PreEndOfGame': '结算中',
    'EndOfGame': '结算完成',
    'Reconnect': '重新连接',
    'TerminatedInError': '异常结束',
}


def find_lcu_via_logs():
    """Parse --remoting-auth-token from LCU log files"""
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


def discover_lcu():
    global LCU_BASE_URL, LCU_HEADERS

    info = find_lcu_via_logs()
    if not info:
        return False

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
# LCU HTTP Client (requests.Session 连接池复用)
# ============================================================

def _ensure_session():
    global _LCU_SESSION
    if _LCU_SESSION is None:
        _LCU_SESSION = requests.Session()
        _LCU_SESSION.verify = False
    _LCU_SESSION.headers.update({
        'Authorization': LCU_HEADERS.get('Authorization', ''),
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    return _LCU_SESSION


def _lcu_request(method, path, body=None):
    if not LCU_BASE_URL:
        return None
    url = f'{LCU_BASE_URL}{path}'
    s = _ensure_session()
    try:
        resp = s.request(method, url, json=body, timeout=5)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json() if resp.text else {}
    except requests.exceptions.HTTPError:
        raise
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout) as e:
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
        s = _ensure_session()
        resp = s.get(url, timeout=5)
        if resp.status_code == 200:
            return resp.content
    except Exception:
        pass
    return None


# ============================================================
# Core Swapper Logic
# ============================================================
def swap_bench_champion(champion_id):
    try:
        lcu_post(f'/lol-champ-select/v1/session/bench/swap/{champion_id}')
        log_swap(f'已换到英雄 #{champion_id} ({CHAMPION_NAMES.get(champion_id, "?")})')
        _refresh_after_swap()
        return True
    except Exception as e:
        log_swap(f'换英雄失败: #{champion_id} - {str(e)}')
        return False


def _refresh_after_swap():
    global POLLING_RESULT
    try:
        session = lcu_get('/lol-champ-select/v1/session')
        if session and 'benchChampions' in session:
            POLLING_RESULT = _build_champ_select_result(session)
    except Exception:
        pass


def quick_pick_champion(champion_id, action_id):
    try:
        lcu_patch(f'/lol-champ-select/v1/session/actions/{action_id}', {
            'championId': champion_id, 'completed': True, 'type': 'pick'
        })
        log_swap(f'已选择英雄 #{champion_id} ({CHAMPION_NAMES.get(champion_id, "?")})')
        return True
    except Exception as e:
        log_swap(f'选英雄失败: #{champion_id} - {str(e)}')
        return False


def log_swap(msg):
    global SWAP_LOG
    ts = time.strftime('%H:%M:%S')
    entry = f'[{ts}] {msg}'
    SWAP_LOG.append(entry)
    if len(SWAP_LOG) > 100:
        SWAP_LOG = SWAP_LOG[-50:]
    print(entry)


def check_auto_accept(raw_phase):
    global AUTO_ACCEPT_TRIGGERED
    if not AUTO_ACCEPT_ENABLED:
        AUTO_ACCEPT_TRIGGERED = False
        return
    if raw_phase == 'ReadyCheck' and not AUTO_ACCEPT_TRIGGERED:
        try:
            rc = lcu_get('/lol-matchmaking/v1/ready-check')
            if rc and rc.get('state') == 'InProgress':
                log_swap(f'检测到对局，{ACCEPT_DELAY}s 后自动接受...')
                time.sleep(ACCEPT_DELAY)
                lcu_post('/lol-matchmaking/v1/ready-check/accept')
                log_swap('已自动接受对局 \u2713')
                AUTO_ACCEPT_TRIGGERED = True
        except Exception as e:
            log_swap(f'自动接受失败: {e}')
    elif raw_phase != 'ReadyCheck':
        AUTO_ACCEPT_TRIGGERED = False


# ============================================================
# State Polling
# ============================================================
POLLING_RESULT = {'connected': False, 'inChampSelect': False, 'champions': [],
                  'phase': None, 'error': '正在启动...', 'needAdmin': False,
                  'autoAcceptEnabled': False}


def _build_champ_select_result(session):
    bench = session.get('benchChampions', [])
    champ_ids = [b['championId'] for b in bench if b.get('championId')]
    pickable = set(session.get('pickableChampionIds', []))
    disabled = set(session.get('bannableChampionIds', []))
    my_team = session.get('myTeam', [])
    local_cell = session.get('localPlayerCellId', 0)
    actions_flat = [a for group in session.get('actions', []) for a in group]
    first_pick_action = None
    for a in actions_flat:
        if a.get('type') == 'pick' and not a.get('completed') and a.get('actorCellId') == local_cell:
            first_pick_action = a
            break
    timer_phase = session.get('timer', {}).get('phase', '')
    self_cid = next((m.get('championId', 0) for m in my_team if m.get('cellId') == local_cell), 0)
    return {
        'connected': True, 'inChampSelect': True, 'phase': timer_phase,
        'benchEnabled': session.get('benchEnabled', False),
        'champions': [{'id': cid, 'name': CHAMPION_NAMES.get(cid, f'#{cid}'),
                        'pickable': cid in pickable, 'disabled': cid in disabled}
                      for cid in champ_ids],
        'pickableIds': list(pickable),
        'selfChampion': self_cid,
        'selfChampionName': CHAMPION_NAMES.get(self_cid),
        'firstPickAction': {'id': first_pick_action['id'],
                            'isInProgress': first_pick_action.get('isInProgress', False)}
        if first_pick_action else None,
        'timerPhase': timer_phase, 'error': None, 'needAdmin': False,
        'autoAcceptEnabled': AUTO_ACCEPT_ENABLED,
    }


def poll_state():
    global CONNECTED, GAMEFLOW_PHASE, POLLING_RESULT, LAST_ERROR
    global LCU_BASE_URL, LCU_HEADERS, _LCU_SESSION

    while True:
        try:
            if not LCU_BASE_URL:
                if discover_lcu():
                    CONNECTED = True
                    POLLING_RESULT = {
                        'connected': True, 'inChampSelect': False, 'phase': None,
                        'benchEnabled': False, 'champions': [], 'pickableIds': [],
                        'selfChampion': 0, 'selfChampionName': None,
                        'firstPickAction': None, 'timerPhase': None,
                        'error': None, 'needAdmin': False,
                    }
                    fetch_champion_names()
                    print(f'[Swapper] 已连接 LCU: {LCU_BASE_URL}')
                else:
                    POLLING_RESULT = {
                        'connected': False, 'inChampSelect': False, 'phase': None,
                        'champions': [], 'error': '等待英雄联盟客户端...', 'needAdmin': False
                    }
                    time.sleep(POLL_INTERVAL)
                    continue

            if not CHAMPION_NAMES:
                fetch_champion_names()

            raw_phase = None
            try:
                gf = lcu_get('/lol-gameflow/v1/session')
                if gf and 'phase' in gf:
                    raw_phase = gf['phase']
                    GAMEFLOW_PHASE = PHASE_TRANSLATIONS.get(raw_phase, raw_phase)
                else:
                    GAMEFLOW_PHASE = None
            except Exception:
                GAMEFLOW_PHASE = None
            if raw_phase:
                check_auto_accept(raw_phase)

            session = None
            try:
                session = lcu_get('/lol-champ-select/v1/session')
            except Exception:
                session = None

            if session and 'benchChampions' in session:
                POLLING_RESULT = _build_champ_select_result(session)
            else:
                POLLING_RESULT = {
                    'connected': CONNECTED, 'inChampSelect': False, 'phase': GAMEFLOW_PHASE,
                    'benchEnabled': False, 'champions': [], 'pickableIds': [],
                    'selfChampion': 0, 'selfChampionName': None,
                    'firstPickAction': None, 'timerPhase': None,
                    'error': None, 'needAdmin': False
                }

            LAST_ERROR = None

        except Exception as e:
            estr = str(e)
            is_connect_refused = 'Connection refused' in estr
            if is_connect_refused:
                CONNECTED = False
                LCU_BASE_URL = None
                _LCU_SESSION = None
                print(f'[Swapper] LCU 连接被拒绝，正在重试...')
            elif LAST_ERROR is None or estr != LAST_ERROR:
                LAST_ERROR = estr
                print(f'[Swapper] 轮询错误: {e}')
            POLLING_RESULT = {
                'connected': CONNECTED, 'inChampSelect': False, 'phase': None,
                'champions': [], 'error': estr, 'needAdmin': False
            }

        POLLING_RESULT['autoAcceptEnabled'] = AUTO_ACCEPT_ENABLED
        time.sleep(POLL_INTERVAL)


# ============================================================
# Web Server
# ============================================================
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>海克斯大乱斗秒换英雄</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#151c24;color:#d1d5db;min-height:100vh}
  .container{max-width:800px;margin:0 auto;padding:20px}
  .header{display:flex;align-items:center;justify-content:space-between;padding:16px 0;border-bottom:1px solid #2a3444;margin-bottom:24px}
  .header h1{font-size:20px;font-weight:700;color:#f59e0b}
  .status-bar{display:flex;align-items:center;gap:10px;padding:10px 16px;border-radius:8px;margin-bottom:20px;font-size:13px}
  .status-bar.connected{background:#0d2818;color:#4ade80}
  .status-bar.disconnected{background:#2d1b1b;color:#f87171}
  .status-bar.waiting{background:#1e1b0d;color:#fbbf24}
  .status-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
  .status-bar.connected .status-dot{background:#4ade80}
  .status-bar.disconnected .status-dot{background:#f87171}
  .status-bar.waiting .status-dot{background:#fbbf24}
  .info-row{display:flex;gap:12px;margin-bottom:24px}
  .info-card{flex:1;background:#1a2332;border-radius:8px;padding:12px 16px;border:1px solid #2a3444}
  .info-card .label{font-size:11px;color:#8892a0;margin-bottom:4px}
  .info-card .value{font-size:16px;font-weight:600}
  .section-title{font-size:14px;font-weight:600;color:#9ca3af;margin-bottom:12px;display:flex;align-items:center;gap:8px}
  .champ-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:12px;margin-bottom:24px}
  .champ-card{background:#1a2332;border:1px solid #2a3444;border-radius:10px;padding:12px;text-align:center;cursor:pointer;transition:transform .15s ease,border-color .15s ease,box-shadow .15s ease;user-select:none;will-change:transform}
  .champ-card:hover{background:#243044;border-color:#f59e0b;transform:translateY(-2px);box-shadow:0 4px 12px rgba(245,158,11,.15)}
  .champ-card:active{transform:scale(.96)}
  .champ-card .icon{width:64px;height:64px;border-radius:50%;margin:0 auto 8px;display:block;border:2px solid #2a3444;background:#111a24}
  .champ-card .name{font-size:13px;font-weight:500;color:#e0e3e8}
  .champ-card.picking .icon{border-color:#22c55e}
  .champ-card .badge{display:inline-block;font-size:10px;padding:1px 6px;border-radius:4px;margin-top:4px}
  .champ-card .badge.yours{background:#0d2818;color:#4ade80}
  .champ-card .badge.off-cd{background:#1e1b0d;color:#fbbf24}
  .empty-state{text-align:center;padding:48px 16px;color:#8892a0}
  .empty-state .icon-big{font-size:48px;margin-bottom:12px}
  .empty-state p{font-size:14px}
  .admin-banner{background:#2d1b1b;border:1px solid #f87171;border-radius:8px;padding:16px;margin-bottom:16px;text-align:center}
  .admin-banner p{color:#f87171;font-size:14px;margin-bottom:12px}
  .admin-banner .btn{background:#f87171;color:#151c24;border:none;padding:8px 20px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer}
  .admin-banner .btn:hover{opacity:.85}
  .log-section{margin-top:24px}
  .log-section .section-title{cursor:pointer;user-select:none}
  .log-box{background:#111a24;border:1px solid #2a3444;border-radius:6px;padding:12px;max-height:200px;overflow-y:auto;font-family:monospace;font-size:12px;line-height:1.6}
  .log-box::-webkit-scrollbar{width:4px}
  .log-box::-webkit-scrollbar-track{background:#111a24}
  .log-box::-webkit-scrollbar-thumb{background:#2a3444;border-radius:2px}
  .log-entry{color:#8892a0}
  .log-entry.success{color:#4ade80}
  .log-entry.error{color:#f87171}
  .btn{background:#f59e0b;color:#151c24;border:none;padding:8px 20px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s}
  .btn:hover{opacity:.85}
  .btn:disabled{opacity:.4;cursor:not-allowed}
  .btn.secondary{background:#2a3444;color:#d1d5db}
  .btn.secondary:hover{background:#354254}
  .quick-pick-section{background:#1a2332;border:1px solid #2a3444;border-radius:10px;padding:16px;margin-bottom:24px;display:none}
  .quick-pick-section h3{font-size:13px;color:#22c55e;margin-bottom:8px}
  .header-right{display:flex;align-items:center;gap:12px}
  .setting-row{display:flex;align-items:center;justify-content:space-between;background:#1a2332;border:1px solid #2a3444;border-radius:8px;padding:12px 16px;margin-bottom:20px}
  .setting-row .setting-label{display:flex;align-items:center;gap:8px;font-size:14px;color:#e0e3e8}
  .setting-row .setting-icon{font-size:16px}
  .toggle-label{display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none}
  .toggle-label input{display:none}
  .toggle-slider{width:36px;height:20px;background:#2a3444;border-radius:10px;position:relative;transition:background .2s}
  .toggle-slider::after{content:'';position:absolute;width:16px;height:16px;border-radius:50%;background:#8892a0;top:2px;left:2px;transition:all .2s}
  .toggle-label input:checked+.toggle-slider{background:#f59e0b}
  .toggle-label input:checked+.toggle-slider::after{background:#fff;left:18px}
  @media(max-width:600px){.champ-grid{grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px}.champ-card .icon{width:48px;height:48px}.info-row{flex-direction:column}}
</style>
</head>
<body>
<div class="container" id="app">
  <div class="header">
    <h1>&#x26A1; 海克斯大乱斗秒换英雄 <span style="font-size:12px;color:#6b7280;font-weight:400">v1.1</span></h1>
    <div class="header-right">
      <a href="https://github.com/tiantong007/LOL-champion-swapper" target="_blank" style="font-size:12px;color:#6b7280;text-decoration:none">GitHub</a>
    </div>
  </div>

  <div id="status-bar" class="status-bar waiting">
    <div class="status-dot"></div>
    <span id="status-text">正在连接 LCU...</span>
  </div>

  <div id="admin-banner" class="admin-banner" style="display:none">
    <p>需要管理员权限才能读取 LCU 连接信息，请以管理员身份重新运行</p>
    <p style="font-size:13px;color:#fca5a5;margin-bottom:8px">右键 start.bat → 以管理员身份运行</p>
  </div>

  <div class="setting-row">
    <div class="setting-label">
      <span class="setting-icon">&#x2699;</span>
      <span>自动接受对局</span>
    </div>
    <label class="toggle-label">
      <input type="checkbox" id="auto-accept-toggle" onchange="toggleAutoAccept(this.checked)">
      <span class="toggle-slider"></span>
    </label>
  </div>

  <div class="info-row">
    <div class="info-card"><div class="label">模式</div><div class="value" id="mode-value">-</div></div>
    <div class="info-card"><div class="label">当前英雄</div><div class="value" id="self-champ-value">-</div></div>
  </div>

  <div class="section-title">&#x1FA91; 可选英雄 <span id="champ-count" style="color:#6b7280;font-weight:400"></span></div>
  <div id="champ-grid" class="champ-grid">
    <div class="empty-state"><p id="empty-text">等待进入选人...</p></div>
  </div>

  <div class="log-section">
    <div class="section-title" onclick="toggleLog()">&#x1F4CB; 操作日志</div>
    <div id="log-box" class="log-box"><div class="log-entry">工具已启动</div></div>
  </div>
</div>

<script>
const POLL_URL = '/api/state';
let _pollTimer = null;
let _swapPending = false;

function doPoll(){
  fetch(POLL_URL).then(r=>r.json()).then(data => {
    updateUI(data);
    if (!data.connected) scheduleNext(2000);
    else if (!data.inChampSelect) scheduleNext(1000);
    else scheduleNext(300);
  }).catch(() => scheduleNext(2000));
}

function scheduleNext(ms){
  if (_pollTimer) clearTimeout(_pollTimer);
  _pollTimer = setTimeout(doPoll, ms);
}

function fetchNow(){
  fetch(POLL_URL).then(r=>r.json()).then(updateUI).catch(()=>{});
}

document.addEventListener('visibilitychange', () => {
  if (_pollTimer) clearTimeout(_pollTimer);
  if (document.hidden) { _pollTimer = setTimeout(doPoll, 2000); }
  else { _pollTimer = setTimeout(doPoll, 300); }
});

function updateUI(data){
  const dk = JSON.stringify(data);
  if (dk === _lastDataKey) return;
  _lastDataKey = dk;
  const sb = document.getElementById('status-bar');
  const st = document.getElementById('status-text');
  const ab = document.getElementById('admin-banner');

  if (data.needAdmin) {
    sb.className = 'status-bar disconnected';
    st.textContent = '需要管理员权限';
    ab.style.display = 'block';
    document.getElementById('mode-value').textContent = '-';
    document.getElementById('self-champ-value').textContent = '-';
    document.getElementById('champ-grid').innerHTML = '<div class="empty-state"><p>' + data.error + '</p></div>';
    return;
  }
  ab.style.display = 'none';
  syncAutoAcceptUI(data.autoAcceptEnabled);

  if (!data.connected) {
    sb.className = 'status-bar disconnected';
    st.textContent = data.error || '未连接';
    document.getElementById('mode-value').textContent = '-';
    document.getElementById('self-champ-value').textContent = '-';
    document.getElementById('champ-grid').innerHTML = '<div class="empty-state"><p>' + (data.error||'等待客户端..') + '</p></div>';
    return;
  }

  if (!data.inChampSelect) {
    sb.className = 'status-bar waiting';
    st.textContent = '已连接(' + (data.phase || '大厅') + ')';
    st.textContent += (data.autoAcceptEnabled ? ' ● 自动接受' : '');
    document.getElementById('mode-value').textContent = '-';
    document.getElementById('self-champ-value').textContent = '-';
    document.getElementById('champ-grid').innerHTML = '<div class="empty-state"><p>等待进入选人...</p></div>';
    return;
  }

  sb.className = 'status-bar connected';
  st.textContent = '选人中 · ' + (data.champions.length + '个可选');
  st.textContent += (data.autoAcceptEnabled ? ' ● 自动接受' : '');
  document.getElementById('mode-value').textContent = data.benchEnabled ? '大乱斗(可选模式)' : '普通模式';
  const sc = data.selfChampion || 0;
  document.getElementById('self-champ-value').textContent = data.selfChampionName || (sc > 0 ? '#' + sc : '未选择');

  if (data.benchEnabled) {
    renderBench(data.champions, sc);
  } else {
    document.getElementById('champ-grid').innerHTML = '<div class="empty-state"><p>非可选模式，仅限大乱斗使用</p></div>';
  }
}

let _lastDataKey = '';
let _lastChampKey = '';
function renderBench(champs, selfChamp){
  const key = JSON.stringify(champs) + '|' + selfChamp;
  if (key === _lastChampKey) return;
  _lastChampKey = key;
  const grid = document.getElementById('champ-grid');
  if (!champs || champs.length === 0) {
    grid.innerHTML = '<div class="empty-state"><p>暂无可选英雄</p></div>';
    document.getElementById('champ-count').textContent = '';
    return;
  }
  document.getElementById('champ-count').textContent = '(' + champs.length + '个)';
  grid.innerHTML = champs.map(c => {
    const isYours = c.id === selfChamp;
    return '<div class="champ-card' + (isYours?' picking':'') + '" onclick="swapChamp(' + c.id + ')" title="点击秒换">'
      + '<img class="icon" src="/api/icon/' + c.id + '" alt="' + c.name + '" onerror="handleIconError(this)">'
      + '<div class="name">' + c.name + '</div>'
      + (isYours ? '<div class="badge yours">当前</div>' : '<div class="badge off-cd">秒换</div>')
      + '</div>';
  }).join('');
}

function handleIconError(img){
  img.style.display = 'none';
}

async function swapChamp(id){
  if (_swapPending) return;
  _swapPending = true;
  try{
    const r = await fetch('/api/swap/' + id, {method:'POST'});
    const d = await r.json();
    addLog(d.success?'success':'error', (d.success?'✅ 换到: ':'❌ 失败: ')+(d.name||'#'+id));
    fetchNow();
  }catch(e){
    addLog('error', '❌ 请求失败: '+e.message);
  }finally{
    _swapPending = false;
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

function syncAutoAcceptUI(enabled){
  const cb = document.getElementById('auto-accept-toggle');
  if (cb) cb.checked = !!enabled;
}

async function toggleAutoAccept(enabled){
  try{
    await fetch('/api/settings/auto-accept', {method:'POST', body:JSON.stringify({enabled})});
    addLog(enabled?'success':'error', (enabled?'已开启自动接受对局':'已关闭自动接受对局'));
  }catch(e){
    addLog('error', '切换自动接受失败: '+e.message);
  }
}

scheduleNext(1000);
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
                     'error': None if ok else '查看日志'}).encode('utf-8'))
            except ValueError:
                self._ok('application/json', json.dumps({'success': False, 'error': '无效ID'}).encode('utf-8'))
        elif path.startswith('/api/quick-pick/'):
            try:
                parts = path.split('/api/quick-pick/')[1].split('/')
                cid, aid = int(parts[0]), int(parts[1])
                ok = quick_pick_champion(cid, aid)
                self._ok('application/json', json.dumps(
                    {'success': ok, 'name': CHAMPION_NAMES.get(cid, f'#{cid}')}).encode('utf-8'))
            except (ValueError, IndexError):
                self._ok('application/json', json.dumps({'success': False, 'error': '参数错误'}).encode('utf-8'))
        elif path == '/api/settings/auto-accept':
            global AUTO_ACCEPT_ENABLED
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                AUTO_ACCEPT_ENABLED = body.get('enabled', False)
                self._ok('application/json', json.dumps({'enabled': AUTO_ACCEPT_ENABLED}).encode('utf-8'))
            except Exception as e:
                self._ok('application/json', json.dumps({'error': str(e)}).encode('utf-8'))
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


def run_server_in_thread():
    server = HTTPServer(('127.0.0.1', SWAPPER_PORT), SwapHandler)
    server.serve_forever()


def run_desktop():
    print('[Desktop] 正在启动桌面窗口...')
    server_thread = threading.Thread(target=run_server_in_thread, daemon=True)
    server_thread.start()
    time.sleep(0.5)
    webview.create_window(
        '海克斯大乱斗秒换英雄',
        f'http://127.0.0.1:{SWAPPER_PORT}',
        width=500, height=700, resizable=True,
    )
    webview.start(private_mode=False)
    print('\n正在关闭...')


def run_browser():
    server = HTTPServer(('127.0.0.1', SWAPPER_PORT), SwapHandler)
    print(f'[Web UI] http://127.0.0.1:{SWAPPER_PORT}')
    try:
        webbrowser.open(f'http://127.0.0.1:{SWAPPER_PORT}')
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n正在关闭...')
        server.shutdown()


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    print('=' * 50)
    print('  海克斯大乱斗秒换英雄工具')
    print('=' * 50)

    poll_thread = threading.Thread(target=poll_state, daemon=True)
    poll_thread.start()
    time.sleep(1)

    if HAS_WEBVIEW:
        run_desktop()
    else:
        run_browser()


if __name__ == '__main__':
    main()
