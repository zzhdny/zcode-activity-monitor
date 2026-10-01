# -*- coding: utf-8 -*-
"""ZCode 活动监视器

轮询读取 ZCode 客户端窗口（Windows UI 自动化），监视侧边栏左下角区域。
一旦出现活动卡片相关文字（如"领取 3 亿 Token 活动"），立即弹 Windows 通知、
响铃并闪烁任务栏图标提醒。

用法：
    py -3.12 monitor.py                # 常驻监视
    py -3.12 monitor.py --once         # 只扫描一次（调试用）
    py -3.12 monitor.py --test-toast   # 测试通知/响铃/闪烁
    py -3.12 monitor.py --install-startup  # 写入开机自启（启动文件夹）

已知限制：ZCode 客户端必须处于运行状态且未最小化到托盘，
侧边栏保持展开时检测最可靠。
"""
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import time
import winsound
from ctypes import wintypes
from datetime import date, datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

import uiautomation as uia

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
LOG_PATH = BASE / "monitor.log"
TOAST_PS1 = BASE / "toast.ps1"

log = logging.getLogger("zcode-monitor")


# ---------------------------------------------------------------- config

DEFAULT_CONFIG = {
    "poll_interval_seconds": 20,
    "zone": {
        # 侧边栏右边界：优先取"调整侧边栏宽度"分隔条位置，找不到时用窗口宽度比例兜底
        "sidebar_max_fraction": 0.30,
        # 活动卡片在侧边栏底部：只看窗口底部这一段高度占比
        "bottom_fraction": 0.22,
    },
    "rules": {
        # 满足任意一条即判定活动出现（该区域平时只有账号栏等固定界面，
        # 任务列表已被排除，单关键词是安全的）。
        # 关键词来自 2026-09-30 实拍卡片：Global Build / 100,000,000
        # Tokens / GLM-5.3-Flash / 领取
        "any_of": ["领取", "Token", "亿", "GLM-5.3-Flash", "Global Build", "福利"],
        # 组合词：两个词同时出现才提醒，进一步降误报
        "all_of": [
            ["活动", "体验套餐"],
        ],
        # 结构性卡片规则：有的卡片内容整块渲染为图片，UIA 树里只剩一个
        # 卡片尺寸的大按钮（如实测的"打开" 354x144）+ 角上"关闭"小按钮，
        # 关键词完全匹配不到。此规则不依赖文字，按"侧边栏底部区出现卡片
        # 尺寸的大按钮"判定；这种大按钮平时在该区域不存在，误报风险低。
        "card": {
            "button_names": ["打开", "领取", "去领取", "立即领取", "点击领取", "查看"],
            "min_width": 200,
            "min_height": 90,
        },
    },
    "alert": {
        "realert_minutes": 30,   # 同一活动重复提醒的间隔
        "max_alerts_per_day": 8,
        "confirm_scans": 2,      # 连续 N 次扫描都命中才提醒（防瞬时弹窗误报）
    },
    # 定时兜底提醒：客户端关闭时唯一能主动提醒的方式（本机时间）。
    # 已知"周末 3 亿 Token"系列已于 2026-09-14 结束，故默认清空；
    # 新一轮活动官宣后按公告时间添加，如
    # {"weekday": "fri", "time": "19:45", "message": "……"}
    "schedules": [],
    # 备用：轮询网页接口（默认关闭）。示例：[{"url": "https://...", "pattern": "3亿"}]
    "url_checks": [],
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if k in ("zone", "rules", "alert") and isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            log.warning("读取 config.json 失败，使用默认配置：%s", e)
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("保存 state.json 失败：%s", e)


# ---------------------------------------------------------------- notify

def toast(title, message):
    if not TOAST_PS1.exists():
        log.error("找不到 toast.ps1，无法弹通知")
        return
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(TOAST_PS1), "-Title", title, "-Message", message],
            timeout=20, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        log.error("弹通知失败：%s", e)


def beep():
    for freq in (880, 1100, 880):
        try:
            winsound.Beep(freq, 260)
        except Exception:
            pass


class FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD)]


def flash_taskbar(hwnd):
    FLASHW_ALL = 0x3
    FLASHW_TIMERNOFG = 0xC
    info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 5, 0)
    try:
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


# ---------------------------------------------------------------- process / window

def zcode_pids():
    """枚举所有 ZCode.exe 进程 pid。"""
    pids = set()
    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    k32.Process32NextW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return pids
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "zcode.exe":
                pids.add(entry.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return pids


_win_cache = {"hwnd": None}


def find_zcode_window():
    """返回 ZCode 主窗口控件（找不到返回 None）。

    客户端隐藏到托盘后，顶层窗口枚举（根节点子元素）看不到它，
    但句柄仍然有效、控件树照样可读，因此首次找到后缓存句柄复用；
    句柄失效（客户端重启/退出）则回退到重新枚举。
    """
    hwnd = _win_cache["hwnd"]
    if hwnd:
        try:
            if ctypes.windll.user32.IsWindow(hwnd):
                pids = zcode_pids()
                buf = wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(buf))
                if not pids or buf.value in pids:
                    c = uia.ControlFromHandle(hwnd)
                    if c is not None:
                        return c
        except Exception as e:
            log.debug("使用缓存句柄失败：%s", e)
        _win_cache["hwnd"] = None

    pids = zcode_pids()
    if not pids:
        return None
    try:
        root = uia.GetRootControl()
        for w in root.GetChildren():
            try:
                if (w.ProcessId in pids) and (w.Name or "") == "ZCode" \
                        and w.ClassName == "Chrome_WidgetWin_1":
                    try:
                        _win_cache["hwnd"] = w.NativeWindowHandle
                    except Exception:
                        pass
                    return w
            except Exception:
                continue
    except Exception as e:
        log.debug("查找窗口失败：%s", e)
    return None


# ---------------------------------------------------------------- scan

def scan_window(win, cfg):
    """扫描 ZCode 窗口侧边栏底部活动区，返回 (文本列表, hwnd, 元素列表)。

    元素列表为 (名称, 矩形, 控件类型) 三元组，供结构性卡片识别
    （detect_card）使用——卡片内容为图片时 UIA 只剩按钮名和尺寸。

    以"调整侧边栏宽度"分隔条为参照系：它的左缘≈侧边栏右边界，
    顶部≈窗口顶部，高度≈窗口内容高度。实测最小化时所有元素坐标
    只是平移（窗口映射到原点）、隐藏到托盘时坐标不变，因此正常、
    最小化、托盘三种状态都能用同一套相对几何定位活动区。
    任务列表的 ListItemControl 子树一律排除（标题是用户任意文本，
    会随列表变长延伸进底部区域，是主要误报源）。
    """
    z = cfg["zone"]
    items, divider = [], None
    suppress_depth = None
    suppress_reason = None
    try:
        for c, depth in uia.WalkControl(win, includeTop=True, maxDepth=30):
            if suppress_depth is not None and depth > suppress_depth:
                continue  # 被剪枝元素的子孙节点
            suppress_depth = suppress_reason = None
            try:
                name = (c.Name or "").strip()
                r = c.BoundingRectangle
            except Exception:
                continue
            if c.ControlTypeName == "ListItemControl" and r.height() > 0:
                suppress_depth, suppress_reason = depth, "listitem"
                continue
            if not name or r.width() <= 0 or r.height() <= 0:
                continue
            if name == "调整侧边栏宽度" and divider is None:
                if r.height() >= 600:
                    divider = r  # 分隔条纵贯整个窗口内容区
            elif divider is not None and r.left >= divider.right:
                # 主区域（聊天/编辑器）占元素大头且与活动无关，
                # DFS 一进入分隔条右侧就整棵剪掉
                suppress_depth, suppress_reason = depth, "main-area"
                continue
            items.append((name, r, c.ControlTypeName))
    except Exception as e:
        log.debug("遍历控件树中断（窗口可能正在变化）：%s", e)

    if divider is None:
        return None, None, None  # 侧边栏已折叠或窗口不可用，本次无法定位活动区

    top, height, right = divider.top, divider.height(), divider.left
    bottom = top + height * (1 - z["bottom_fraction"])
    texts, zone_items = [], []
    for name, r, ctype in items:
        if name == "调整侧边栏宽度":
            continue
        # 完全在参照区之外（被裁剪到可视区外）的元素不算
        if r.top >= top + height - 4 or r.bottom > top + height + 8 or r.bottom <= top + 4:
            continue
        cx, cy = (r.left + r.right) / 2, (r.top + r.bottom) / 2
        if cx < right and cy > bottom:
            texts.append(name)
            zone_items.append((name, r, ctype))

    hwnd = None
    try:
        hwnd = win.NativeWindowHandle
    except Exception:
        pass
    return texts, hwnd, zone_items


def scan_popup(pw):
    """扫描悬浮窗/弹窗的全部文本与元素，返回 (文本列表, 元素列表)。

    无区域几何，靠规则与二次确认防误报；元素列表供结构性卡片识别。
    """
    texts, items, suppress_depth = [], [], None
    try:
        for c, depth in uia.WalkControl(pw, includeTop=True, maxDepth=20):
            if suppress_depth is not None and depth > suppress_depth:
                continue
            suppress_depth = None
            try:
                name = (c.Name or "").strip()
                r = c.BoundingRectangle
            except Exception:
                continue
            if c.ControlTypeName == "ListItemControl" and r.height() > 0:
                suppress_depth = depth
                continue
            if name and r.width() > 0 and r.height() > 0:
                texts.append(name)
                items.append((name, r, c.ControlTypeName))
    except Exception as e:
        log.debug("悬浮窗扫描中断：%s", e)
    return texts, items


def find_zcode_popups(main_win):
    """主窗口之外、属于 ZCode 进程的顶层窗口（悬浮卡片/弹窗/菜单）。"""
    pids = zcode_pids()
    if not pids:
        return []
    try:
        main_hwnd = main_win.NativeWindowHandle
    except Exception:
        main_hwnd = None
    out = []
    try:
        root = uia.GetRootControl()
        for w in root.GetChildren():
            try:
                if w.ProcessId not in pids or w.ClassName != "Chrome_WidgetWin_1":
                    continue
                if main_hwnd and w.NativeWindowHandle == main_hwnd:
                    continue
                r = w.BoundingRectangle
                if r.width() > 80 and r.height() > 60:
                    out.append(w)
            except Exception:
                continue
    except Exception as e:
        log.debug("枚举悬浮窗失败：%s", e)
    return out


def match_rules(texts, cfg):
    """对活动区域文本应用关键词规则，返回命中的描述（未命中返回 None）。"""
    joined = " ".join(texts)
    hits = []
    for kw in cfg["rules"].get("any_of", []):
        if kw and kw in joined:
            hits.append(kw)
    for pair in cfg["rules"].get("all_of", []):
        if pair and all(k in joined for k in pair):
            hits.append("+".join(pair))
    if not hits:
        return None
    return "；命中规则：" + "、".join(sorted(set(hits)))


def detect_card(items, cfg, require_close=False):
    """结构性卡片识别（不依赖卡片文字）。

    卡片内容整块渲染为图片时，UIA 树里只剩一个卡片尺寸的大按钮
    （如"打开" 354x144）和角上的"关闭"小按钮。返回命中描述或 None。
    require_close=True 时还要求大按钮内部有一个"关闭"小按钮
    （悬浮窗扫描用：弹窗里的大按钮未必是活动卡片，需更多佐证）。
    """
    card_cfg = cfg["rules"].get("card") or {}
    names = set(card_cfg.get("button_names") or ["打开", "领取"])
    min_w = float(card_cfg.get("min_width", 200))
    min_h = float(card_cfg.get("min_height", 90))
    for name, r, ctype in items:
        if name not in names or ctype != "ButtonControl":
            continue
        if r.width() < min_w or r.height() < min_h:
            continue
        if require_close:
            has_close = False
            for n2, r2, t2 in items:
                if n2 != "关闭" or r2.width() > 60 or r2.height() > 60:
                    continue
                cx2, cy2 = (r2.left + r2.right) / 2, (r2.top + r2.bottom) / 2
                if r.left <= cx2 <= r.right and r.top <= cy2 <= r.bottom:
                    has_close = True
                    break
            if not has_close:
                continue
        return f"；命中规则：卡片大按钮「{name}」{r.width()}x{r.height()}"
    return None


# ---------------------------------------------------------------- alerts

def maybe_alert(cfg, state, reason, hwnd, confirmed=True, detail=""):
    today = date.today().isoformat()
    if state.get("day") != today:
        state["day"], state["alerts_today"] = today, 0
    alert_cfg = cfg["alert"]

    # UI 扫描路径需要连续命中 confirm_scans 次才确认，排除瞬时弹窗/悬浮提示
    if not confirmed:
        need = max(1, int(alert_cfg.get("confirm_scans", 2)))
        pending = state.get("pending") or {}
        if pending.get("signature") == reason:
            pending["count"] = pending.get("count", 0) + 1
        else:
            pending = {"signature": reason, "count": 1}
        state["pending"] = pending
        save_state(state)
        if pending["count"] < need:
            log.info("疑似活动（第 %d/%d 次命中），待确认：%s | 区域文本：%s",
                     pending["count"], need, reason, (detail or "")[:400])
            return False

    signature = reason
    new_activity = state.get("signature") != signature
    due = not state.get("last_alert") or \
        (datetime.now() - datetime.fromisoformat(state["last_alert"])).total_seconds() \
        >= alert_cfg["realert_minutes"] * 60
    if state.get("alerts_today", 0) >= alert_cfg["max_alerts_per_day"]:
        log.info("今日提醒次数已达上限，跳过")
        return False
    if not (new_activity or due):
        return False

    msg = f"ZCode 客户端出现活动入口，打开客户端 → 侧边栏左下角活动卡片查看领取。{reason}"
    log.info("触发提醒：%s | 区域文本：%s", msg, (detail or "")[:400])
    toast("ZCode 活动出现，快去领取！", msg)
    beep()
    if hwnd:
        flash_taskbar(hwnd)
    state["signature"] = signature
    state["last_alert"] = datetime.now().isoformat(timespec="seconds")
    state["alerts_today"] = state.get("alerts_today", 0) + 1
    state.pop("pending", None)
    save_state(state)
    return True


# ---------------------------------------------------------------- optional checkers

def check_schedules(cfg, state):
    for item in cfg.get("schedules", []):
        wd_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        want_wd = wd_map.get(str(item.get("weekday", "")).lower())
        want_hm = str(item.get("time", ""))
        if want_wd is None or not re.match(r"^\d{1,2}:\d{2}$", want_hm):
            continue
        now = datetime.now()
        if now.weekday() != want_wd:
            continue
        key = f"sch:{item.get('weekday')}:{want_hm}"
        if state.get(key) == today_key():
            continue
        hh, mm = (int(x) for x in want_hm.split(":"))
        if (now.hour, now.minute) >= (hh, mm):
            toast("ZCode 活动定时提醒", item.get("message", "到点了，去看看活动"))
            beep()
            state[key] = today_key()
            save_state(state)
            log.info("定时提醒已触发：%s", key)


def check_urls(cfg):
    import urllib.request
    for item in cfg.get("url_checks", []):
        url, pattern = item.get("url"), item.get("pattern")
        if not url or not pattern:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read(2_000_000).decode("utf-8", "ignore")
            if re.search(pattern, body):
                return f"网页 {url} 命中 {pattern!r}"
        except Exception as e:
            log.debug("url_checks %s 失败：%s", url, e)
    return None


def today_key():
    return date.today().isoformat()


# ---------------------------------------------------------------- main loop

def scan_once(cfg, state, alert_enabled=True):
    win = find_zcode_window()
    if win is None:
        log.debug("ZCode 客户端未运行或窗口未找到")
        return "no_window"
    texts, hwnd, items = scan_window(win, cfg)
    detail, strong = [], False
    if texts is None:
        log.debug("侧边栏折叠或窗口不可用，本轮仅扫描悬浮窗")
        texts, items = [], []
    else:
        # 飞行记录：区域文本一有变化就记进日志，便于事后诊断漏报
        prev = state.get("last_zone_texts")
        if prev != texts:
            log.info("活动区域文本变化：%s", texts)
            state["last_zone_texts"] = texts
            save_state(state)
        detail = list(texts)
    reason = match_rules(texts, cfg)
    if not reason:
        card = detect_card(items, cfg)
        if card:
            # 结构性卡片命中不需要二次确认（大按钮不是瞬时弹窗那种形态）
            reason, strong = card, True
    if not reason:
        # 主窗口没命中，再查 ZCode 的悬浮卡片/弹窗（独立顶层窗口）
        for pw in find_zcode_popups(win):
            ptexts, pitems = scan_popup(pw)
            if not ptexts:
                continue
            reason = match_rules(ptexts, cfg)
            card = detect_card(pitems, cfg, require_close=True)
            if reason or card:
                if card:
                    reason, strong = card, True
                detail = ptexts
                try:
                    hwnd = pw.NativeWindowHandle
                except Exception:
                    hwnd = None
                break
    if reason:
        if alert_enabled:
            maybe_alert(cfg, state, reason, hwnd, confirmed=strong,
                        detail=" | ".join(detail))
        else:
            log.info("检测到活动（提醒已关闭）：%s", reason)
        return "hit"
    if state.pop("pending", None):
        save_state(state)  # 本轮未命中，清掉待确认计数
    return "clear"


def setup_logging():
    handler = RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    log.addHandler(console)
    log.setLevel(logging.INFO)


def install_startup():
    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    vbs = startup / "zcode_activity_monitor.vbs"
    content = (
        'CreateObject("WScript.Shell").Run '
        f'"""{pythonw}"" ""{BASE / "monitor.py"}""", 0, False'
    )
    vbs.write_text(content, encoding="ascii")
    print(f"已写入开机自启：{vbs}")
    print(f"启动命令：\"{pythonw}\" \"{BASE / 'monitor.py'}\"")
    print("如需取消自启，删除该 .vbs 文件即可。")


def acquire_single_instance():
    """保证只有一个常驻实例（多实例会互抢 state.json、重复提醒）。"""
    import msvcrt
    # 用追加模式打开，避免第二个实例打开文件时清掉第一个实例写入的 PID
    fh = open(BASE / "monitor.lock", "a")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        print("已有监视器实例在运行（monitor.lock 被占用），本次退出。")
        sys.exit(0)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh  # 持有到进程退出，锁自动释放；进程崩溃时系统也会释放


def main():
    setup_logging()
    cfg = load_config()

    if "--test-toast" in sys.argv:
        print("测试：应弹出 Windows 通知、响三声、任务栏闪烁……")
        toast("ZCode 活动监视器测试", "通知样式如上，正式提醒会长这样。")
        time.sleep(1)
        beep()
        win = find_zcode_window()
        if win:
            try:
                flash_taskbar(win.NativeWindowHandle)
            except Exception:
                pass
        print("测试完成。")
        return

    if "--install-startup" in sys.argv:
        install_startup()
        return

    once = "--once" in sys.argv
    state = load_state()
    interval = max(10, int(cfg.get("poll_interval_seconds", 30)))
    log.info("ZCode 活动监视器启动（每 %d 秒扫描一次，配置：%s）", interval, CONFIG_PATH)
    if once:
        result = scan_once(cfg, state, alert_enabled=False)
        print({
            "hit": "扫描结果：检测到活动！",
            "clear": "扫描结果：已找到 ZCode 窗口，活动区域无关键词命中",
            "no_window": "扫描结果：未找到 ZCode 客户端窗口（客户端没开或最小化到托盘）",
            "no_zone": "扫描结果：找不到侧边栏分隔条（侧边栏可能已折叠），无法定位活动区",
        }.get(result, f"扫描结果：未知状态 {result}"))
        return

    _lock = acquire_single_instance()
    while True:
        try:
            scan_once(cfg, state)
            check_schedules(cfg, state)
            url_reason = check_urls(cfg)
            if url_reason:
                maybe_alert(cfg, state, url_reason, None, confirmed=True)
        except Exception as e:
            log.error("本轮扫描异常：%s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
