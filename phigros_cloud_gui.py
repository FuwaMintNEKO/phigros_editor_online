#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phigros 云存档图形编辑器 by FuwaMintNEKO
========================================
功能：拉取云存档 -> 完整显示/编辑所有数据 -> 上传回服务器（自动备份+回读验证）

完整显示以下数据（对应云端 zip 内每个条目）:
  * 玩家信息 + 服务器摘要（RKS / 挑战等级 / 各难度成就数）
  * gameRecord   曲目分数（可增删改）
  * settings     游戏设置（可编辑）
  * user         用户信息（可编辑）
  * gameProgress 游戏进度（可编辑）
  * gameKey      解锁钥匙（只读展示，编辑有风险）

用法:  python phigros_cloud_gui.py
依赖:  pip install requests pycryptodome
仅供学习交流使用，请勿用于破坏性 / 盈利性用途。
"""

import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

from phigros_cloud import (
    LCClient, load_cloud_save, build_save_zip, upload_save_zip, fetch_save,
    restore_save_zip, serialize_summary, describe_game_key_flag, DIFFS,
    check_int32, check_float32, parse_int_field, parse_float_field,
    INT32_MIN, INT32_MAX, UINT16_MAX, BYTE_MAX, VARINT_MAX,
    SCORE_RANGE, ACC_RANGE,
)

CHALLENGE_RANK = {0: "白", 1: "绿", 2: "蓝", 3: "红", 4: "金", 5: "彩虹"}
PROGRESS_FLAG_DESC = {
    "spasmodic_flag": "Spasmodic 解锁(EZ/HD/IN/AT, 0~255)",
    "igallta_flag": "Igallta 解锁(EZ/HD/IN/AT, 0~255)",
    "rrharil_flag": "Rrharil 解锁(EZ/HD/IN/AT, 0~255)",
    "song_record_flag": "曲目记录标志(bit0-6: 各曲IN的S评价, 0~255)",
}
FLAG_BIT_NAMES = [
    ("EZ", 1), ("HD", 2), ("IN", 4), ("AT", 8),
]

# 设置项常规范围（游戏设计约定，仅提示不拦截）
SETTINGS_FLOAT_RANGES = {
    "background_brightness": (0.0, 1.0),
    "music_volume": (0.0, 1.0),
    "effect_volume": (0.0, 1.0),
    "hit_sound_volume": (0.0, 1.0),
    "sound_offset": (-0.1, 0.1),
    "note_scale": (0.5, 2.0),
}


def fmt_flag(v: int) -> str:
    parts = []
    for name, bit in FLAG_BIT_NAMES:
        if v & bit:
            parts.append(name)
    return "|".join(parts) if parts else "无"


class CloudEditorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.data = None          # load_cloud_save 返回的完整存档 dict
        self.raw_zip_backup = None  # 拉取时的原始 zip（备份用）
        self.q = queue.Queue()    # 后台线程 -> UI 消息

        root.title("Phigros 云存档编辑器 by FuwaMintNEKO")
        root.geometry("1080x720")

        self._build_topbar()
        self._build_notebook()
        self._build_statusbar()

        self.root.after(100, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI 构建 ----------------

    def _build_topbar(self):
        bar = ttk.Frame(self.root, padding=6)
        bar.pack(fill="x")
        ttk.Label(bar, text="Session Token:").pack(side="left")
        self.token_var = tk.StringVar()
        tok = ttk.Entry(bar, textvariable=self.token_var, width=38)
        tok.pack(side="left", padx=4)
        ttk.Label(bar, text="服务器:").pack(side="left")
        self.region_var = tk.StringVar(value="cn")
        ttk.Combobox(bar, textvariable=self.region_var, values=["cn", "intl"],
                     width=5, state="readonly").pack(side="left", padx=4)
        ttk.Button(bar, text="拉取存档", command=self.cmd_fetch).pack(side="left", padx=4)
        ttk.Button(bar, text="备份原始存档", command=self.cmd_backup).pack(side="left", padx=4)
        ttk.Button(bar, text="从备份还原...", command=self.cmd_restore).pack(side="left", padx=4)
        ttk.Label(bar, text="提示：游戏若在后台运行，可能自动上传本地存档覆盖修改",
                  foreground="#b45309").pack(side="left", padx=10)

    def _build_statusbar(self):
        f = ttk.Frame(self.root, padding=4)
        f.pack(fill="x", side="bottom")
        ttk.Button(f, text="上传到服务器", command=self.cmd_upload).pack(side="right")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(f, textvariable=self.status_var, relief="sunken",
                  anchor="w").pack(fill="x")

    def _build_notebook(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=6, pady=4)
        self._build_tab_summary()
        self._build_tab_record()
        self._build_tab_settings()
        self._build_tab_user()
        self._build_tab_progress()
        self._build_tab_gamekey()

    # --- 摘要/玩家 ---
    def _build_tab_summary(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="玩家与摘要")
        self.summary_text = tk.Text(tab, state="disabled", wrap="none", font=("Consolas", 10))
        self.summary_text.pack(fill="both", expand=True, padx=6, pady=6)

    # --- 曲目分数 ---
    def _build_tab_record(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="曲目分数")

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)
        cols = ("id", "diff", "score", "acc", "fc")
        self.rec_tree = ttk.Treeview(wrap, columns=cols, show="headings", height=18)
        for c, t, w in [("id", "曲目ID", 380), ("diff", "难度", 50),
                        ("score", "分数", 100), ("acc", "ACC", 100), ("fc", "FC", 40)]:
            self.rec_tree.heading(c, text=t)
            self.rec_tree.column(c, width=w, anchor="w" if c == "id" else "center")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.rec_tree.yview)
        self.rec_tree.configure(yscrollcommand=vsb.set)
        self.rec_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.rec_tree.bind("<<TreeviewSelect>>", self._on_rec_select)

        edit = ttk.LabelFrame(tab, text="编辑选中记录", padding=6)
        edit.pack(fill="x", padx=6, pady=(0, 6))
        self.rec_sel_id = tk.StringVar()
        self.rec_sel_diff = tk.StringVar()
        self.rec_sel_score = tk.StringVar()
        self.rec_sel_acc = tk.StringVar()
        self.rec_sel_fc = tk.BooleanVar()
        ttk.Label(edit, text="曲目ID:").grid(row=0, column=0, sticky="e")
        ttk.Entry(edit, textvariable=self.rec_sel_id, width=44).grid(row=0, column=1, padx=2)
        ttk.Label(edit, text="难度:").grid(row=0, column=2, sticky="e")
        ttk.Combobox(edit, textvariable=self.rec_sel_diff, values=DIFFS,
                     width=5, state="readonly").grid(row=0, column=3, padx=2)
        ttk.Label(edit, text="分数(常规0~100w):").grid(row=0, column=4, sticky="e")
        ttk.Entry(edit, textvariable=self.rec_sel_score, width=10).grid(row=0, column=5, padx=2)
        ttk.Label(edit, text="ACC(常规0~100):").grid(row=0, column=6, sticky="e")
        ttk.Entry(edit, textvariable=self.rec_sel_acc, width=8).grid(row=0, column=7, padx=2)
        ttk.Checkbutton(edit, text="FC", variable=self.rec_sel_fc).grid(row=0, column=8, padx=4)
        ttk.Button(edit, text="应用修改", command=self._rec_apply).grid(row=0, column=9, padx=4)
        ttk.Button(edit, text="新增记录", command=self._rec_add).grid(row=0, column=10, padx=4)
        ttk.Button(edit, text="删除记录", command=self._rec_delete).grid(row=0, column=11, padx=4)

    # --- 设置 ---
    def _build_tab_settings(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="游戏设置")
        f = ttk.LabelFrame(tab, text="设置 (对应 settings 条目)", padding=8)
        f.pack(fill="x", padx=8, pady=8)

        self.set_bools = {}
        bool_rows = [
            ("chord_support", "和弦支持(未知)"),
            ("fc_ap_indicator_on", "FC/AP 指示器"),
            ("enable_hit_sound", "打击音效"),
            ("low_resolution_mode_on", "低分辨率模式"),
        ]
        for i, (key, label) in enumerate(bool_rows):
            var = tk.BooleanVar()
            ttk.Checkbutton(f, text=label, variable=var).grid(row=i, column=0, sticky="w", padx=4, pady=2)
            self.set_bools[key] = var

        ttk.Label(f, text="设备名称:").grid(row=0, column=1, sticky="e")
        self.set_device = tk.StringVar()
        ttk.Entry(f, textvariable=self.set_device, width=24).grid(row=0, column=2, padx=4)
        float_rows = [
            ("background_brightness", "背景亮度(常规0~1):"),
            ("music_volume", "音乐音量(常规0~1):"),
            ("effect_volume", "音效音量(常规0~1):"),
            ("hit_sound_volume", "打击音音量(常规0~1):"),
            ("sound_offset", "延迟偏移(秒,负=提前):"),
            ("note_scale", "音符大小(常规0.5~2):"),
        ]
        self.set_floats = {}
        for i, (key, label) in enumerate(float_rows):
            var = tk.StringVar()
            ttk.Label(f, text=label).grid(row=1 + i, column=1, sticky="e")
            ttk.Entry(f, textvariable=var, width=12).grid(row=1 + i, column=2, padx=4)
            self.set_floats[key] = var

    # --- 用户信息 ---
    def _build_tab_user(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="用户信息")
        f = ttk.LabelFrame(tab, text="用户信息 (对应 user 条目)", padding=8)
        f.pack(fill="x", padx=8, pady=8)
        ttk.Label(f, text="文本字段：任意 UTF-8 文本（建议简洁，避免特殊字符）",
                  foreground="#666").grid(row=0, column=1, columnspan=2, sticky="w", pady=(0, 4))
        self.user_show_id = tk.BooleanVar()
        ttk.Checkbutton(f, text="显示用户ID", variable=self.user_show_id).grid(row=1, column=0, sticky="w")
        ttk.Label(f, text="个人简介:").grid(row=2, column=0, sticky="ne")
        self.user_intro = tk.StringVar()
        ttk.Entry(f, textvariable=self.user_intro, width=48).grid(row=2, column=1, padx=4, pady=2)
        ttk.Label(f, text="头像ID:").grid(row=3, column=0, sticky="e")
        self.user_avatar = tk.StringVar()
        ttk.Entry(f, textvariable=self.user_avatar, width=24).grid(row=3, column=1, sticky="w", padx=4)
        ttk.Label(f, text="背景ID:").grid(row=4, column=0, sticky="e")
        self.user_background = tk.StringVar()
        ttk.Entry(f, textvariable=self.user_background, width=24).grid(row=4, column=1, sticky="w", padx=4)

    # --- 游戏进度 ---
    def _build_tab_progress(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="游戏进度")
        f = ttk.LabelFrame(tab, text="游戏进度 (对应 gameProgress 条目)", padding=8)
        f.pack(fill="x", padx=8, pady=8)

        self.prog_bools = {}
        prog_bool_rows = [
            ("is_first_run", "首次运行"),
            ("legacy_chapter_finished", "完成旧章节"),
            ("already_show_collection_tip", "已展示收藏提示"),
            ("already_show_auto_unlock_in_tip", "已展示IN自动解锁提示"),
        ]
        for i, (key, label) in enumerate(prog_bool_rows):
            var = tk.BooleanVar()
            ttk.Checkbutton(f, text=label, variable=var).grid(row=i, column=0, sticky="w", padx=4)
            self.prog_bools[key] = var

        self.prog_vars = {}
        rows = [
            ("completed", "已通关章节(空/3.0):", 12),
            ("song_update_info", "歌曲更新信息(0~32767):", 10),
            ("challenge", "挑战码(0~65535):", 10),
        ]
        for i, (key, label, w) in enumerate(rows):
            var = tk.StringVar()
            ttk.Label(f, text=label).grid(row=i, column=1, sticky="e")
            ttk.Entry(f, textvariable=var, width=w).grid(row=i, column=2, padx=4)
            self.prog_vars[key] = var

        ttk.Label(f, text="货币(各0~32767):").grid(row=0, column=3, sticky="e")
        self.money_vars = []
        for i, unit in enumerate(["KiB", "MiB", "GiB", "TiB", "PiB"]):
            var = tk.StringVar()
            # 单位名在上、输入框在下，避免 Entry 与 Label 同格重叠导致输入错位
            ttk.Label(f, text=unit, foreground="#666").grid(row=0, column=4 + i, pady=(8, 0))
            ttk.Entry(f, textvariable=var, width=7, justify="center").grid(
                row=1, column=4 + i, padx=2)
            self.money_vars.append((unit, var))

        self.flag_vars = {}
        for i, key in enumerate(["spasmodic_flag", "igallta_flag", "rrharil_flag", "song_record_flag"]):
            var = tk.StringVar()
            ttk.Label(f, text=f"{PROGRESS_FLAG_DESC[key]}:").grid(row=2 + i, column=3, sticky="e")
            ttk.Entry(f, textvariable=var, width=8).grid(row=2 + i, column=4, padx=4)
            ttk.Label(f, text="hex").grid(row=2 + i, column=5, sticky="w")
            self.flag_vars[key] = var

        self.prog_extra = tk.StringVar()
        ttk.Label(f, text="附加数据(Node2+):").grid(row=6, column=3, sticky="e")
        ttk.Entry(f, textvariable=self.prog_extra, width=16, state="readonly").grid(row=6, column=4, padx=4)

    # --- 解锁钥匙 ---
    def _build_tab_gamekey(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="解锁钥匙")
        tip = ttk.Label(tab, text="gameKey 条目（只读展示，编辑二进制标志位有损坏存档风险）：",
                        foreground="#666")
        tip.pack(anchor="w", padx=8, pady=(6, 0))
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=8, pady=6)
        cols = ("name", "flag", "desc")
        self.gk_tree = ttk.Treeview(wrap, columns=cols, show="headings")
        for c, t, w in [("name", "钥匙名", 360), ("flag", "标志(hex)", 80), ("desc", "含义", 420)]:
            self.gk_tree.heading(c, text=t)
            self.gk_tree.column(c, width=w, anchor="w")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.gk_tree.yview)
        self.gk_tree.configure(yscrollcommand=vsb.set)
        self.gk_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    # ---------------- 数据填充 ----------------

    def _set_status(self, msg: str):
        self.status_var.set(msg)

    def _log_summary(self, text: str):
        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", text)
        self.summary_text.configure(state="disabled")

    def _fill_all(self):
        """把 self.data 填充到所有标签页"""
        d = self.data
        # 摘要
        me = getattr(self, "_player_info", {})
        s = d.get("summary")
        lines = [f"昵称: {me.get('nickname', '?')}", f"用户名: {me.get('username', '?')}",
                 f"objectId: {me.get('objectId', '?')}", f"创建: {me.get('createdAt', '?')}",
                 f"更新: {me.get('updatedAt', '?')}", ""]
        if s:
            rank = CHALLENGE_RANK.get(s["challenge"] // 100, "?")
            lines += [f"=== 服务器摘要 (存档v{s['save_version']} / 游戏v{s['game_version']}) ===",
                      f"RKS: {s['rks']:.4f}   挑战码: {s['challenge']} (等级 {rank})   头像: {s['avatar']!r}"]
            for diff, c in zip(DIFFS, s["counts"]):
                lines.append(f"  {diff}: 通关 {c['cleared']}  全连 {c['fc']}  φ {c['phi']}")
        self._log_summary("\n".join(lines))

        # 曲目分数
        self.rec_tree.delete(*self.rec_tree.get_children())
        for r in d["game_record"]:
            self.rec_tree.insert("", "end", values=(
                r["id"], DIFFS[r["diff"]], r["score"], f"{r['acc']:.6f}",
                "√" if r["fc"] else ""))
        # 设置
        st = d["settings"]
        for key, var in self.set_bools.items():
            var.set(st["bools"][key])
        self.set_device.set(st["device_name"])
        for key, var in self.set_floats.items():
            var.set(f"{st[key]:g}")
        # 用户
        u = d["user"]
        self.user_show_id.set(u["show_user_id"])
        self.user_intro.set(u["intro"])
        self.user_avatar.set(u["avatar_id"])
        self.user_background.set(u["background_id"])
        # 进度
        gp = d["game_progress"]
        for key, var in self.prog_bools.items():
            var.set(gp["bools"][key])
        self.prog_vars["completed"].set(gp["completed"])
        self.prog_vars["song_update_info"].set(str(gp["song_update_info"]))
        self.prog_vars["challenge"].set(str(gp["challenge"]))
        for (unit, var), val in zip(self.money_vars, gp["money"]):
            var.set(str(val))
        for key, var in self.flag_vars.items():
            var.set(f"0x{gp[key]:02x}")
        self.prog_extra.set(gp["extra"].hex())
        # 解锁钥匙
        self.gk_tree.delete(*self.gk_tree.get_children())
        for k in d["game_key"]["keys"]:
            self.gk_tree.insert("", "end", values=(
                k["name"], k["flag_raw"].hex(), describe_game_key_flag(k["flag_raw"])))

    # ---------------- 操作 ----------------

    def _run_bg(self, fn, ok_msg=None):
        """在后台线程执行网络操作，结果经队列回 UI"""
        self._set_status("工作中...")
        threading.Thread(target=lambda: self._worker(fn, ok_msg), daemon=True).start()

    def _worker(self, fn, ok_msg):
        try:
            result = fn()
            self.q.put(("ok", ok_msg, result))
        except Exception as e:
            self.q.put(("err", str(e), None))

    def _drain_queue(self):
        try:
            while True:
                kind, msg, result = self.q.get_nowait()
                if kind == "ok":
                    self._set_status(msg)
                    if msg == "拉取完成":
                        self._fill_all()
                    elif msg == "上传成功":
                        self._set_status("上传成功，正在回读验证...")
                        self._start_verify()
                    elif msg == "验证完成":
                        self._after_verify(result)
                    elif msg == "还原成功":
                        messagebox.showinfo("还原成功", f"已用备份覆盖云存档 (fileId={result})")
                else:
                    self._set_status(f"错误: {msg}")
                    messagebox.showerror("错误", msg)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def cmd_fetch(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("提示", "请输入 session token")
            return
        region = self.region_var.get()

        def fn():
            client = LCClient(token, region)
            data = load_cloud_save(client)
            me = client.get_json("/1.1/users/me")
            self._player_info = me
            self.data = data
            self.raw_zip_backup = data["raw_zip"]
            return data
        self._run_bg(fn, "拉取完成")

    def cmd_backup(self):
        if self.raw_zip_backup is None:
            messagebox.showwarning("提示", "请先拉取存档")
            return
        path = f"backup_{int(time.time() * 1000)}.zip"
        with open(path, "wb") as f:
            f.write(self.raw_zip_backup)
        # 连同服务器 summary 一起保存，保证以后能完整还原（zip + summary 一致）
        if self.data and self.data.get("summary_b64"):
            with open(path + ".summary", "w") as f:
                f.write(self.data["summary_b64"])
            self._set_status(f"已备份到 {path} (+.summary)")
        else:
            self._set_status(f"已备份原始存档到 {path} ({len(self.raw_zip_backup)} 字节)")

    def cmd_restore(self):
        """从本地备份 zip 还原到服务器（连同 summary，保证两者一致）"""
        if not self.token_var.get().strip():
            messagebox.showwarning("提示", "请输入 session token")
            return
        path = filedialog.askopenfilename(title="选择备份 zip", filetypes=[("zip", "*.zip")])
        if not path:
            return
        with open(path, "rb") as f:
            zip_bytes = f.read()
        if not messagebox.askyesno("确认还原",
                                   f"将把 {os.path.basename(path)} 上传覆盖云存档，\n"
                                   "这会丢弃服务器当前内容。继续？"):
            return
        region = self.region_var.get()
        token = self.token_var.get().strip()
        # 备份时生成的 .summary 文件，优先完整还原；没有则自动从 zip 重建 challenge
        saved_summary = None
        spath = path + ".summary"
        if os.path.exists(spath):
            with open(spath) as f:
                saved_summary = f.read().strip()

        def fn():
            client = LCClient(token, region)
            obj_id, _, _ = fetch_save(client)
            file_id = restore_save_zip(client, obj_id, zip_bytes,
                                       saved_summary_b64=saved_summary)
            return file_id
        self._run_bg(fn, "还原成功")

    # 曲目分数编辑
    def _on_rec_select(self, _event=None):
        sel = self.rec_tree.selection()
        if not sel:
            return
        vals = self.rec_tree.item(sel[0], "values")
        self.rec_sel_id.set(vals[0])
        self.rec_sel_diff.set(vals[1])
        self.rec_sel_score.set(vals[2])
        self.rec_sel_acc.set(vals[3])
        self.rec_sel_fc.set(vals[4] == "√")

    def _read_rec_form(self):
        """读取曲目分数表单。类型/溢出错误抛 ValueError；常规范围超限弹确认框。
        返回 (id, diff, score, acc, fc)；用户取消返回 None。"""
        try:
            diff = DIFFS.index(self.rec_sel_diff.get())
        except ValueError:
            raise ValueError("难度无效")
        sid = self.rec_sel_id.get().strip()
        try:
            score = int(self.rec_sel_score.get())
        except ValueError:
            raise ValueError("分数必须是整数")
        check_int32(score, "分数")
        acc = parse_float_field(self.rec_sel_acc.get(), "ACC")
        # 常规范围（游戏设计约定）：仅提示，由用户决定是否继续
        warnings = []
        if not SCORE_RANGE[0] <= score <= SCORE_RANGE[1]:
            warnings.append(f"分数 {score} 超出常规范围 {SCORE_RANGE[0]}~{SCORE_RANGE[1]}")
        if not ACC_RANGE[0] <= acc <= ACC_RANGE[1]:
            warnings.append(f"ACC {acc} 超出常规范围 {ACC_RANGE[0]}~{ACC_RANGE[1]}")
        if warnings and not messagebox.askyesno(
                "范围提示（不拦截）",
                "以下数值超出游戏常规范围，仍可正常写入存档：\n\n"
                + "\n".join(warnings) + "\n\n仍要应用吗？"):
            return None
        return sid, diff, score, acc, self.rec_sel_fc.get()

    def _refresh_rec_tree(self):
        self.rec_tree.delete(*self.rec_tree.get_children())
        for r in self.data["game_record"]:
            self.rec_tree.insert("", "end", values=(
                r["id"], DIFFS[r["diff"]], r["score"], f"{r['acc']:.6f}",
                "√" if r["fc"] else ""))

    def _rec_apply(self):
        if self.data is None:
            messagebox.showwarning("提示", "请先拉取存档")
            return
        try:
            result = self._read_rec_form()
        except ValueError as e:
            messagebox.showerror("输入错误", str(e))
            return
        if result is None:
            return
        sid, diff, score, acc, fc = result
        for r in self.data["game_record"]:
            if r["id"] == sid and r["diff"] == diff:
                r["score"], r["acc"], r["fc"] = score, acc, fc
                self._refresh_rec_tree()
                self._set_status(f"已修改 {sid}.{DIFFS[diff]} -> {score} ({acc}%)")
                return
        # 不存在则提示新增
        messagebox.showinfo("提示", "该记录不存在，将作为新增处理")
        self._rec_add()

    def _rec_add(self):
        if self.data is None:
            messagebox.showwarning("提示", "请先拉取存档")
            return
        try:
            result = self._read_rec_form()
        except ValueError as e:
            messagebox.showerror("输入错误", str(e))
            return
        if result is None:
            return
        sid, diff, score, acc, fc = result
        if not sid:
            messagebox.showwarning("提示", "请输入曲目ID")
            return
        if any(r["id"] == sid and r["diff"] == diff for r in self.data["game_record"]):
            messagebox.showwarning("提示", "该记录已存在，请直接应用修改")
            return
        self.data["game_record"].append(
            {"id": sid, "diff": diff, "score": score, "acc": acc, "fc": fc})
        self._refresh_rec_tree()
        self._set_status(f"已新增 {sid}.{DIFFS[diff]}")

    def _rec_delete(self):
        if self.data is None:
            messagebox.showwarning("提示", "请先拉取存档")
            return
        sel = self.rec_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在表格中选择要删除的记录")
            return
        vals = self.rec_tree.item(sel[0], "values")
        sid, diff = vals[0], DIFFS.index(vals[1])
        self.data["game_record"] = [
            r for r in self.data["game_record"] if not (r["id"] == sid and r["diff"] == diff)]
        self._refresh_rec_tree()
        self._set_status(f"已删除 {sid}.{DIFFS[diff]}")

    # ---------------- 上传 ----------------

    def _collect_form_edits(self):
        """把表单控件里的值写回 self.data（曲目分数已在 _rec_apply 里更新）。

        返回 (data, warnings)：类型/溢出错误抛 ValueError（不可放行）；
        超出游戏常规范围的值收集进 warnings（仅提示，不拦截）。
        """
        if self.data is None:
            raise ValueError("尚未拉取存档")
        warnings = []
        # settings
        st = self.data["settings"]
        st["bools"] = {k: var.get() for k, var in self.set_bools.items()}
        st["device_name"] = self.set_device.get()
        for key, var in self.set_floats.items():
            try:
                v = float(var.get())
            except ValueError:
                raise ValueError(f"设置项 {key} 必须是数字")
            check_float32(v, key)
            lo, hi = SETTINGS_FLOAT_RANGES[key]
            if not lo <= v <= hi:
                warnings.append(f"设置项 {key}={v:g} 超出常规范围 {lo:g}~{hi:g}")
            st[key] = v
        # user
        u = self.data["user"]
        u["show_user_id"] = self.user_show_id.get()
        u["intro"] = self.user_intro.get()
        u["avatar_id"] = self.user_avatar.get()
        u["background_id"] = self.user_background.get()
        # gameProgress
        gp = self.data["game_progress"]
        gp["bools"] = {k: var.get() for k, var in self.prog_bools.items()}
        gp["completed"] = self.prog_vars["completed"].get()
        gp["song_update_info"] = parse_int_field(
            self.prog_vars["song_update_info"].get() or 0, "歌曲更新信息", 0, VARINT_MAX)
        gp["challenge"] = parse_int_field(
            self.prog_vars["challenge"].get() or 0, "挑战码", 0, UINT16_MAX)
        gp["money"] = [parse_int_field(v.get() or 0, f"货币({unit})", 0, VARINT_MAX)
                       for unit, v in self.money_vars]
        for key, var in self.flag_vars.items():
            try:
                v = int(var.get(), 16)
            except ValueError:
                raise ValueError(f"{key} 不是合法 hex")
            if not 0 <= v <= BYTE_MAX:
                raise ValueError(f"{key} 超出 byte 存储范围 0~255")
            gp[key] = v
        return self.data, warnings

    def cmd_upload(self):
        if self.data is None:
            messagebox.showwarning("提示", "请先拉取存档")
            return
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("提示", "请输入 session token")
            return
        region = self.region_var.get()
        try:
            data, warnings = self._collect_form_edits()
        except ValueError as e:
            messagebox.showerror("输入错误", str(e))
            return
        if warnings and not messagebox.askyesno(
                "范围提示（不拦截）",
                "以下数值超出游戏常规范围（仍可正常写入存档）：\n\n"
                + "\n".join(warnings) + "\n\n继续上传？"):
            return

        new_zip = build_save_zip(data)
        # 备份当前服务器内容
        bak = f"backup_{int(time.time() * 1000)}.zip"
        with open(bak, "wb") as f:
            f.write(self.raw_zip_backup)
        if not messagebox.askyesno(
                "确认上传",
                f"将修改后的存档上传覆盖云存档。\n"
                f"原始存档已自动备份到: {bak}\n"
                f"上传后会自动回读验证。\n\n"
                f"提示：若游戏本体仍在后台运行，它可能会自动上传本地存档覆盖本次修改，\n"
                f"此时回读验证会提示差异，可退出游戏后重试。\n\n"
                f"是否继续？"):
            return
        obj_id = data["object_id"]

        # 同步 _GameSave.summary：挑战码跟随 gameProgress.challenge，其余字段透传原值
        # （RKS/counts 需谱面定数表才能重算，保持原值，游戏端下次同步会自行纠正）
        summary_b64 = None
        if self.data.get("summary"):
            s = dict(self.data["summary"])
            s["challenge"] = data["game_progress"]["challenge"]
            data["summary"] = s  # 写回，供上传后全字段验证对比
            summary_b64 = serialize_summary(s)

        self._last_uploaded = data

        def fn():
            file_id = upload_save_zip(LCClient(token, region), obj_id, new_zip,
                                      summary_b64=summary_b64)
            return file_id
        self._run_bg(fn, "上传成功")

    def _start_verify(self):
        """上传后回读服务器存档，确认改动生效"""
        def verify():
            client = LCClient(self.token_var.get().strip(), self.region_var.get())
            return load_cloud_save(client)
        self._run_bg(verify, "验证完成")

    def _diff(self, a, b, prefix=""):
        """递归对比两个 dict/list，返回差异描述列表（跳过体积大且无意义的字段）。

        记录列表（如 game_record 按 (id,diff)、gameKey 按 name）按唯一键匹配对比，
        忽略顺序——存档序列化时会重新排序，位置对比会造成大量误报。
        """
        out = []
        if isinstance(a, dict) and isinstance(b, dict):
            for k in a:
                if k in ("raw_zip", "object_id", "summary_b64"):
                    continue
                out += self._diff(a[k], b.get(k), f"{prefix}{k}.")
        elif isinstance(a, list) and isinstance(b, list):
            if a != b:
                if a and isinstance(a[0], dict):
                    keyfn = None
                    if "id" in a[0] and "diff" in a[0]:
                        keyfn = lambda x: (x.get("id"), x.get("diff"))
                    elif "name" in a[0]:
                        keyfn = lambda x: x.get("name")
                    if keyfn:
                        da = {keyfn(x): x for x in a}
                        db = {keyfn(x): x for x in b}
                        for k, x in da.items():
                            if k not in db:
                                out.append(f"{prefix}服务器未保留: {k}")
                            else:
                                out += self._diff(x, db[k], f"{prefix}{k}.")
                        for k in db:
                            if k not in da:
                                out.append(f"{prefix}服务器多出: {k}")
                    else:
                        for i, (x, y) in enumerate(zip(a, b)):
                            if x != y:
                                out += self._diff(x, y, f"{prefix}[{i}].")
                        if len(a) != len(b):
                            out.append(f"{prefix}记录条数 {len(a)} -> {len(b)}")
                else:
                    out.append(f"{prefix}: {a} -> {b}")
        elif isinstance(a, float) and isinstance(b, float):
            # float32 精度损失属正常现象（如 1.3 存成 1.2999999523162842），用容差比较
            if abs(a - b) > 1e-5:
                out.append(f"{prefix}{a!r} -> {b!r}")
        elif a != b:
            out.append(f"{prefix}{a!r} -> {b!r}")
        return out

    def _after_verify(self, data2):
        """对比上传内容与服务器回读内容（全字段，不只曲目分数）"""
        up = getattr(self, "_last_uploaded", None)
        if up is None:
            return
        diffs = self._diff(up, data2)
        # 用回读结果刷新界面，反映服务器真实状态
        self.data = data2
        self.raw_zip_backup = data2["raw_zip"]
        self._fill_all()
        if not diffs:
            messagebox.showinfo(
                "验证通过",
                f"回读验证通过：服务器存档与上传内容一致"
                f"（含挑战码/货币/设置/用户信息）。\n"
                f"共 {len(data2['game_record'])} 条曲目记录。\n"
                f"结论：远程修改云存档 ✓ 确实生效（服务器无完整性校验）。")
        else:
            messagebox.showwarning(
                "验证提醒",
                "回读与上传内容存在差异：\n\n" + "\n".join(diffs[:15]) +
                "\n\n说明：上传本身已成功。若差异来自游戏后台自动同步（本地存档覆盖了修改），\n"
                "退出游戏（含后台）后重新「拉取 → 修改 → 上传」即可；\n"
                "新增的曲目记录属正常预期，一般会被服务器保留。")

    def _on_close(self):
        self.root.destroy()


def main():
    try:
        # 固定工作目录到脚本所在目录：
        # 备份/上传等写文件操作使用相对路径，双击或从别处启动时 cwd 可能不对，
        # 强制切回脚本目录可确保备份 zip 写入有权限且位置固定。
        os.chdir(Path(__file__).resolve().parent)
        root = tk.Tk()
        CloudEditorApp(root)

        # Tkinter 按钮回调里的异常默认只打到 stderr，双击(pythonw)时完全静默；
        # 重写 report_callback_exception：统一弹窗 + 追加写日志，让错误可见。
        def _report(exc, val, tb):
            import traceback
            msg = "".join(traceback.format_exception(exc, val, tb))
            try:
                with open(Path(__file__).with_name("gui_error.log"), "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except OSError:
                pass
            try:
                messagebox.showerror("操作出错", msg)
            except Exception:
                pass
        root.report_callback_exception = _report

        root.mainloop()
    except Exception:
        # 双击(pythonw)运行时看不到控制台，任何未捕获异常都弹窗并落盘，方便排查
        import traceback
        tb = traceback.format_exc()
        try:
            with open(Path(__file__).with_name("gui_error.log"), "w", encoding="utf-8") as f:
                f.write(tb)
        except OSError:
            pass
        try:
            tk.Tk().withdraw()
            messagebox.showerror("程序出错", tb)
        except Exception:
            pass


if __name__ == "__main__":
    main()
