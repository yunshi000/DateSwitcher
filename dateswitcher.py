#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DateSwitcher —— Windows 定时日期切换器

按自定义计划自动切换 Windows 系统日期：
  - 切换到指定日期 / 相对偏移天数 / 恢复真实日期
  - 重复规则：每天、工作日、每周、仅一次、固定间隔（分钟）
  - 内置"真实时间"基准（可 NTP 校准），随时可一键恢复真实日期
  - 修改系统日期需要管理员权限（程序内可一键提权重启）

仅使用 Python 标准库（tkinter + ctypes），无第三方依赖。
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import socket
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

APP_NAME = "DateSwitcher"
APP_TITLE = "定时日期切换器 DateSwitcher"

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

MODE_LABELS = {
    "set": "切换到指定日期",
    "offset": "相对偏移（天）",
    "restore": "恢复真实日期",
}
REPEAT_LABELS = {
    "daily": "每天",
    "workday": "工作日",
    "weekly": "每周（勾选星期）",
    "once": "仅一次",
    "interval": "固定间隔（分钟）",
}
LABEL_TO_MODE = {v: k for k, v in MODE_LABELS.items()}
LABEL_TO_REPEAT = {v: k for k, v in REPEAT_LABELS.items()}


# ---------------------------------------------------------------------------
# Win32 系统时间 API
# ---------------------------------------------------------------------------

class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ushort) for name in (
        "wYear", "wMonth", "wDayOfWeek", "wDay",
        "wHour", "wMinute", "wSecond", "wMilliseconds",
    )]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def set_system_time(dt: datetime) -> tuple[bool, str]:
    """设置 Windows 本地系统时间（需要管理员权限）。

    SetLocalTime 依赖当前日期评估夏令时，存在已知的换算特性，
    官方建议连续调用两次以确保结果正确。
    """
    st = SYSTEMTIME(
        dt.year, dt.month, 0, dt.day,
        dt.hour, dt.minute, dt.second, dt.microsecond // 1000,
    )
    ok = _kernel32.SetLocalTime(ctypes.byref(st))
    if not ok:
        err = ctypes.get_last_error()
        if err == 1314:  # ERROR_PRIVILEGE_NOT_HELD
            return False, "权限不足：请以管理员身份运行程序"
        return False, f"SetLocalTime 失败（错误码 {err}）"
    _kernel32.SetLocalTime(ctypes.byref(st))  # 二次调用修正夏令时换算
    return True, "ok"


# ---------------------------------------------------------------------------
# NTP 校时（纯标准库 UDP 实现）
# ---------------------------------------------------------------------------

def ntp_utc_epoch(server: str = "ntp.aliyun.com", timeout: float = 3.0) -> float:
    """从 NTP 服务器获取真实 UTC 时间戳。"""
    ntp_delta = 2208988800  # 1900-01-01 → 1970-01-01 的秒数
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(b"\x1b" + b"\x00" * 47, (server, 123))
        data, _ = sock.recvfrom(1024)
    finally:
        sock.close()
    if len(data) < 48:
        raise ValueError("NTP 响应不完整")
    return struct.unpack("!I", data[40:44])[0] - ntp_delta


class RealClock:
    """维护"真实时间"基准：基准 UTC 时间戳 + 本会话单调流逝。

    - calibrate_to_system()：把当前系统时间标记为真实时间
    - calibrate_to_ntp()：从 NTP 服务器获取真实时间（最可靠）
    - now()：推算当前真实本地时间（不受系统日期被篡改影响）
    """

    def __init__(self, epoch: float | None):
        self.perf0 = time.perf_counter()
        if epoch is None:
            self.epoch = datetime.now().timestamp()
            self.fresh = True  # 首次运行，自动以当前时间为基准
        else:
            self.epoch = float(epoch)
            self.fresh = False

    def now(self) -> datetime:
        elapsed = time.perf_counter() - self.perf0
        return (datetime.fromtimestamp(self.epoch + elapsed, tz=timezone.utc)
                .astimezone().replace(tzinfo=None))

    def calibrate_to_system(self) -> None:
        self.epoch = datetime.now().timestamp()
        self.perf0 = time.perf_counter()
        self.fresh = False

    def calibrate_to_ntp(self, server: str = "ntp.aliyun.com") -> None:
        self.epoch = ntp_utc_epoch(server)
        self.perf0 = time.perf_counter()
        self.fresh = False


# ---------------------------------------------------------------------------
# 任务模型与配置持久化
# ---------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    name: str
    time: str                 # "HH:MM"
    mode: str                 # set / offset / restore
    repeat: str               # daily / workday / weekly / once / interval
    enabled: bool = True
    date: list = field(default_factory=lambda: [2026, 1, 1])    # mode=set
    days: int = 1                                               # mode=offset
    weekdays: list = field(default_factory=lambda: [0, 1, 2, 3, 4])  # repeat=weekly
    interval_min: int = 60                                      # repeat=interval
    last_fired: str = ""      # 上次触发的日期，用于当天防重复

    def action_desc(self) -> str:
        if self.mode == "set":
            y, m, d = self.date
            return f"设为 {y:04d}-{m:02d}-{d:02d}"
        if self.mode == "offset":
            return f"偏移 {self.days:+d} 天"
        return "恢复真实日期"

    def repeat_desc(self) -> str:
        if self.repeat == "weekly":
            return "每周 " + "、".join(WEEKDAY_NAMES[w] for w in sorted(self.weekdays))
        if self.repeat == "interval":
            return f"每 {self.interval_min} 分钟"
        return REPEAT_LABELS[self.repeat]


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            cfg.setdefault("tasks", [])
            cfg.setdefault("real_epoch", None)
            return cfg
    except FileNotFoundError:
        pass
    except Exception as exc:  # 配置损坏时降级为全新配置
        print(f"读取配置失败: {exc}")
    return {"tasks": [], "real_epoch": None}


def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self, cfg: dict):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("900x700")
        self.minsize(780, 600)

        self.cfg = cfg
        self.tasks: list[Task] = []
        for raw in cfg.get("tasks", []):
            try:
                self.tasks.append(Task(**raw))
            except TypeError:
                continue  # 跳过不符合当前结构的旧记录
        self.real_clock = RealClock(cfg.get("real_epoch"))
        self.interval_last: dict[str, float] = {}
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.admin = is_admin()

        self._build_vars()
        self._build_ui()
        self._sync_mode_params()
        self._sync_repeat_params()
        self._refresh_tree()
        self._update_admin_label()
        self._log("程序已启动。" if self.admin else
                  "程序已启动。当前未以管理员身份运行，切换日期会失败；"
                  "请点击右上角「以管理员身份重启」。")
        if self.real_clock.fresh:
            self._log("已自动记录当前时间作为「真实时间」基准，建议先执行一次 NTP 校准。")
        self.after(500, self._tick)

    # ---------------- UI 构建 ----------------

    def _build_vars(self):
        self.var_name = tk.StringVar(value="我的任务")
        self.var_time = tk.StringVar(value="00:00")
        self.var_mode = tk.StringVar(value=MODE_LABELS["set"])
        self.var_y = tk.IntVar(value=datetime.now().year)
        self.var_m = tk.IntVar(value=1)
        self.var_d = tk.IntVar(value=1)
        self.var_days = tk.IntVar(value=1)
        self.var_repeat = tk.StringVar(value=REPEAT_LABELS["daily"])
        self.var_weekdays = [tk.BooleanVar(value=w < 5) for w in range(7)]
        self.var_interval = tk.IntVar(value=60)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # ---- 顶部：时钟与全局操作 ----
        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(2, weight=1)

        self.lbl_sys = ttk.Label(top, text="系统时间：--", font=("Microsoft YaHei UI", 12, "bold"))
        self.lbl_sys.grid(row=0, column=0, sticky="w")
        self.lbl_real = ttk.Label(top, text="真实时间：--", font=("Microsoft YaHei UI", 12))
        self.lbl_real.grid(row=0, column=1, sticky="w", padx=(24, 0))
        self.lbl_admin = ttk.Label(top, text="", foreground="#B45309")
        self.lbl_admin.grid(row=0, column=3, sticky="e")

        bar = ttk.Frame(top)
        bar.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Button(bar, text="恢复真实日期", command=self._restore_now).pack(side="left")
        ttk.Button(bar, text="NTP 校准真实时间", command=self._ntp_calibrate).pack(side="left", padx=8)
        ttk.Button(bar, text="记当前为真实时间", command=self._calibrate_system).pack(side="left")
        ttk.Button(bar, text="以管理员身份重启", command=self._relaunch_admin).pack(side="right")

        # ---- 任务列表 ----
        mid = ttk.Frame(self, padding=(10, 8))
        mid.grid(row=1, column=0, sticky="ew")
        cols = ("name", "time", "action", "repeat", "enabled")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=7)
        for col, text, width in (
            ("name", "任务名称", 180), ("time", "触发时间", 90),
            ("action", "动作", 180), ("repeat", "重复规则", 220),
            ("enabled", "状态", 80),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.tag_configure("off", foreground="#999999")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # ---- 任务编辑区 ----
        form = ttk.LabelFrame(self, text="任务编辑", padding=(10, 6))
        form.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))
        for c, w in ((0, 0), (1, 1), (2, 0), (3, 1)):
            form.grid_columnconfigure(c, weight=w)

        ttk.Label(form, text="任务名称").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        ttk.Entry(form, textvariable=self.var_name, width=20).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(form, text="触发时间").grid(row=0, column=2, sticky="e", padx=(16, 4))
        ttk.Entry(form, textvariable=self.var_time, width=10).grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(form, text="动作").grid(row=1, column=0, sticky="e", padx=4, pady=3)
        cb_mode = ttk.Combobox(form, textvariable=self.var_mode, state="readonly",
                               width=20, values=list(MODE_LABELS.values()))
        cb_mode.grid(row=1, column=1, sticky="w", padx=4)
        cb_mode.bind("<<ComboboxSelected>>", lambda _e: self._sync_mode_params())

        # 动作参数（同一单元格互斥显示）
        self.frm_set = ttk.Frame(form)
        self.frm_set.grid(row=1, column=2, columnspan=2, sticky="w", padx=(16, 4))
        ttk.Label(self.frm_set, text="目标日期").pack(side="left")
        ttk.Spinbox(self.frm_set, textvariable=self.var_y, from_=1970, to=9999, width=7).pack(side="left", padx=2)
        ttk.Label(self.frm_set, text="年").pack(side="left")
        ttk.Spinbox(self.frm_set, textvariable=self.var_m, from_=1, to=12, width=4).pack(side="left", padx=2)
        ttk.Label(self.frm_set, text="月").pack(side="left")
        ttk.Spinbox(self.frm_set, textvariable=self.var_d, from_=1, to=31, width=4).pack(side="left", padx=2)
        ttk.Label(self.frm_set, text="日").pack(side="left")

        self.frm_offset = ttk.Frame(form)
        self.frm_offset.grid(row=1, column=2, columnspan=2, sticky="w", padx=(16, 4))
        ttk.Label(self.frm_offset, text="偏移天数").pack(side="left")
        ttk.Spinbox(self.frm_offset, textvariable=self.var_days, from_=-9999, to=9999, width=8).pack(side="left", padx=4)
        ttk.Label(self.frm_offset, text="天（正数向后，负数向前）").pack(side="left")

        self.frm_restore = ttk.Frame(form)
        self.frm_restore.grid(row=1, column=2, columnspan=2, sticky="w", padx=(16, 4))
        ttk.Label(self.frm_restore, text="（无需参数，将把系统日期恢复为记录的真实时间）").pack(side="left")

        ttk.Label(form, text="重复规则").grid(row=2, column=0, sticky="e", padx=4, pady=3)
        cb_repeat = ttk.Combobox(form, textvariable=self.var_repeat, state="readonly",
                                 width=20, values=list(REPEAT_LABELS.values()))
        cb_repeat.grid(row=2, column=1, sticky="w", padx=4)
        cb_repeat.bind("<<ComboboxSelected>>", lambda _e: self._sync_repeat_params())

        self.frm_week = ttk.Frame(form)
        self.frm_week.grid(row=2, column=2, columnspan=2, sticky="w", padx=(16, 4))
        for i in range(7):
            ttk.Checkbutton(self.frm_week, text=WEEKDAY_NAMES[i],
                            variable=self.var_weekdays[i]).pack(side="left")

        self.frm_interval = ttk.Frame(form)
        self.frm_interval.grid(row=2, column=2, columnspan=2, sticky="w", padx=(16, 4))
        ttk.Label(self.frm_interval, text="间隔").pack(side="left")
        ttk.Spinbox(self.frm_interval, textvariable=self.var_interval,
                    from_=1, to=10080, width=7).pack(side="left", padx=4)
        ttk.Label(self.frm_interval, text="分钟").pack(side="left")

        btns = ttk.Frame(form)
        btns.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 2))
        for text, cmd in (
            ("新增任务", self._add_task), ("更新选中", self._update_task),
            ("删除选中", self._delete_task), ("启用/停用", self._toggle_task),
            ("立即执行", self._run_task_now),
        ):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=(0, 8))

        # ---- 日志 ----
        logf = ttk.LabelFrame(self, text="运行日志", padding=(6, 4))
        logf.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.txt_log = scrolledtext.ScrolledText(logf, height=10, state=tk.DISABLED,
                                                 font=("Consolas", 10), wrap="word")
        self.txt_log.pack(fill="both", expand=True)

    def _sync_mode_params(self):
        mode = LABEL_TO_MODE[self.var_mode.get()]
        for frm, show in ((self.frm_set, mode == "set"),
                          (self.frm_offset, mode == "offset"),
                          (self.frm_restore, mode == "restore")):
            if show:
                frm.grid()
            else:
                frm.grid_remove()

    def _sync_repeat_params(self):
        repeat = LABEL_TO_REPEAT[self.var_repeat.get()]
        for frm, show in ((self.frm_week, repeat == "weekly"),
                          (self.frm_interval, repeat == "interval")):
            if show:
                frm.grid()
            else:
                frm.grid_remove()

    # ---------------- 心跳：时钟刷新 + 调度 ----------------

    def _tick(self):
        now = datetime.now()
        self.lbl_sys.config(text=f"系统时间：{now:%Y-%m-%d %H:%M:%S}")
        self.lbl_real.config(text=f"真实时间：{self.real_clock.now():%Y-%m-%d %H:%M:%S}")
        self._drain_queue()
        self._run_scheduler(now)
        self.after(500, self._tick)

    def _run_scheduler(self, now: datetime):
        changed = False
        for t in list(self.tasks):
            if not t.enabled:
                continue
            if t.repeat == "interval":
                last = self.interval_last.get(t.id)
                if last is None:
                    self.interval_last[t.id] = time.perf_counter()
                    continue
                if time.perf_counter() - last >= max(1, t.interval_min) * 60:
                    self.interval_last[t.id] = time.perf_counter()
                    self._execute(t)
                continue
            if now.strftime("%H:%M") != t.time:
                continue
            if t.repeat == "workday" and now.weekday() >= 5:
                continue
            if t.repeat == "weekly" and now.weekday() not in t.weekdays:
                continue
            guard = now.strftime("%Y-%m-%d")
            if t.last_fired == guard:
                continue
            t.last_fired = guard
            self._execute(t)
            if t.repeat == "once":
                t.enabled = False
            changed = True
        if changed:
            self._refresh_tree()
            self._persist()

    def _execute(self, task: Task):
        now = datetime.now()
        if task.mode == "restore":
            target = self.real_clock.now()
        elif task.mode == "set":
            y, m, d = task.date
            try:
                target = now.replace(year=int(y), month=int(m), day=int(d))
            except ValueError as exc:
                self._log(f"[{task.name}] 切换失败：目标日期无效（{exc}）")
                return
        else:  # offset
            target = now + timedelta(days=int(task.days))
        ok, msg = set_system_time(target)
        if ok:
            self._log(f"[{task.name}] 系统日期已切换为 {target:%Y-%m-%d %H:%M:%S}")
        else:
            self._log(f"[{task.name}] 切换失败：{msg}")

    # ---------------- 全局操作 ----------------

    def _restore_now(self):
        self._execute(Task(id="", name="手动恢复", time="", mode="restore", repeat="once"))

    def _calibrate_system(self):
        self.real_clock.calibrate_to_system()
        self._persist()
        self._log("已把当前系统时间标记为「真实时间」基准。")

    def _ntp_calibrate(self):
        self._log("正在通过 NTP（ntp.aliyun.com）校准真实时间…")

        def worker():
            try:
                self.real_clock.calibrate_to_ntp()
                self.ui_queue.put((
                    "log",
                    f"NTP 校准成功，真实时间基准已更新（真实时间 "
                    f"{self.real_clock.now():%Y-%m-%d %H:%M:%S}）",
                ))
                self.ui_queue.put(("persist", None))
            except Exception as exc:
                self.ui_queue.put(("log", f"NTP 校准失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _relaunch_admin(self):
        if self.admin:
            messagebox.showinfo(APP_TITLE, "当前已以管理员身份运行。")
            return
        if getattr(sys, "frozen", False):  # PyInstaller 打包后的 exe
            exe, params = sys.executable, ""
        else:
            exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(exe):
                exe = sys.executable
            params = f'"{os.path.abspath(__file__)}"'
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        if rc > 32:
            self._log("已请求管理员权限重新启动程序…")
            self.after(300, self.destroy)
        else:
            messagebox.showerror(APP_TITLE, "提权启动失败（可能取消了 UAC 确认）。")

    def _update_admin_label(self):
        if self.admin:
            self.lbl_admin.config(text="已以管理员身份运行", foreground="#15803D")
        else:
            self.lbl_admin.config(text="未以管理员运行（切换日期会失败）", foreground="#B45309")

    # ---------------- 任务增删改 ----------------

    def _form_to_task(self, task_id: str | None = None) -> Task | None:
        name = self.var_name.get().strip() or "未命名任务"

        raw_time = self.var_time.get().strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", raw_time):
            messagebox.showwarning(APP_TITLE, "触发时间格式应为 HH:MM，例如 08:30")
            return None
        hh, mm = (int(x) for x in raw_time.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            messagebox.showwarning(APP_TITLE, "触发时间超出范围（小时 0-23，分钟 0-59）")
            return None

        mode = LABEL_TO_MODE[self.var_mode.get()]
        repeat = LABEL_TO_REPEAT[self.var_repeat.get()]

        date, days, interval = [2026, 1, 1], 0, 60
        try:
            if mode == "set":
                date = [int(self.var_y.get()), int(self.var_m.get()), int(self.var_d.get())]
            elif mode == "offset":
                days = int(self.var_days.get())
            if repeat == "interval":
                interval = int(self.var_interval.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning(APP_TITLE, "参数不完整，请检查日期 / 天数 / 间隔输入")
            return None

        weekdays = [i for i, v in enumerate(self.var_weekdays) if v.get()]
        if mode == "set":
            try:
                datetime(date[0], date[1], date[2])
            except ValueError as exc:
                messagebox.showwarning(APP_TITLE, f"指定日期无效：{exc}")
                return None
        if repeat == "weekly" and not weekdays:
            messagebox.showwarning(APP_TITLE, "每周重复至少需要勾选一个星期")
            return None
        if repeat == "interval" and interval < 1:
            messagebox.showwarning(APP_TITLE, "间隔至少为 1 分钟")
            return None

        return Task(id=task_id or uuid.uuid4().hex[:8], name=name,
                    time=f"{hh:02d}:{mm:02d}", mode=mode, repeat=repeat,
                    date=date, days=days, weekdays=weekdays, interval_min=interval)

    def _selected_task(self) -> Task | None:
        sel = self.tree.selection()
        if not sel:
            return None
        for t in self.tasks:
            if t.id == sel[0]:
                return t
        return None

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for t in self.tasks:
            time_text = "—" if t.repeat == "interval" else t.time
            self.tree.insert("", "end", iid=t.id, tags=() if t.enabled else ("off",),
                             values=(t.name, time_text, t.action_desc(),
                                     t.repeat_desc(), "已启用" if t.enabled else "已停用"))

    def _on_select(self, _event=None):
        t = self._selected_task()
        if not t:
            return
        self.var_name.set(t.name)
        self.var_time.set(t.time)
        self.var_mode.set(MODE_LABELS[t.mode])
        self._sync_mode_params()
        if t.mode == "set":
            self.var_y.set(t.date[0]), self.var_m.set(t.date[1]), self.var_d.set(t.date[2])
        elif t.mode == "offset":
            self.var_days.set(t.days)
        self.var_repeat.set(REPEAT_LABELS[t.repeat])
        self._sync_repeat_params()
        for i, v in enumerate(self.var_weekdays):
            v.set(i in t.weekdays)
        if t.repeat == "interval":
            self.var_interval.set(t.interval_min)

    def _add_task(self):
        task = self._form_to_task()
        if not task:
            return
        self.tasks.append(task)
        self._refresh_tree()
        self._persist()
        self.tree.selection_set(task.id)
        self._log(f"已新增任务「{task.name}」")

    def _update_task(self):
        cur = self._selected_task()
        if not cur:
            messagebox.showwarning(APP_TITLE, "请先在列表中选中要更新的任务")
            return
        task = self._form_to_task(task_id=cur.id)
        if not task:
            return
        self.tasks[self.tasks.index(cur)] = task
        self._refresh_tree()
        self._persist()
        self._log(f"已更新任务「{task.name}」")

    def _delete_task(self):
        cur = self._selected_task()
        if not cur:
            messagebox.showwarning(APP_TITLE, "请先选中要删除的任务")
            return
        if not messagebox.askyesno(APP_TITLE, f"确定删除任务「{cur.name}」？"):
            return
        self.tasks.remove(cur)
        self._refresh_tree()
        self._persist()
        self._log(f"已删除任务「{cur.name}」")

    def _toggle_task(self):
        cur = self._selected_task()
        if not cur:
            messagebox.showwarning(APP_TITLE, "请先选中任务")
            return
        cur.enabled = not cur.enabled
        self._refresh_tree()
        self._persist()
        self._log(f"任务「{cur.name}」已{'启用' if cur.enabled else '停用'}")

    def _run_task_now(self):
        cur = self._selected_task()
        if not cur:
            messagebox.showwarning(APP_TITLE, "请先选中任务")
            return
        self._execute(cur)

    # ---------------- 持久化 / 日志 / 消息 ----------------

    def _persist(self):
        self.cfg["tasks"] = [asdict(t) for t in self.tasks]
        self.cfg["real_epoch"] = self.real_clock.epoch
        try:
            save_config(self.cfg)
        except Exception as exc:
            self._log(f"保存配置失败：{exc}")

    def _drain_queue(self):
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "log":
                self._log(payload)
            elif kind == "persist":
                self._persist()

    def _log(self, msg: str):
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.insert(tk.END, f"[{datetime.now():%H:%M:%S}] {msg}\n")
        line_count = int(self.txt_log.index("end-1c").split(".")[0])
        if line_count > 500:
            self.txt_log.delete("1.0", f"{line_count - 500}.0")
        self.txt_log.see(tk.END)
        self.txt_log.config(state=tk.DISABLED)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    try:  # Win10 高分屏下让界面更清晰
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = App(load_config())
    app.mainloop()


if __name__ == "__main__":
    main()
