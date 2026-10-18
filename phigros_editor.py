# -*- coding: utf-8 -*-
"""
Phigros 存档图形化编辑器
========================
基于 phigros_decrypt.py 的解密/加密能力，提供 tkinter 图形界面：
  - 打开存档 XML -> 自动解密所有 key/value
  - 列表展示（Key / Value / 类型 / 状态）
  - 实时搜索过滤（支持 key/value/全部）
  - 选中条目后可编辑 Value（多行文本框）
  - "保存"按钮：备份原文件 -> 重新加密修改过的条目 -> 写回 XML
  - 双击列表条目可快速把 Value 美化为 JSON（如果是 JSON 的话）

依赖：仅 tkinter（Python 标准库，无需 pip install）
"""
import sys
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# 导入同目录的解密/加密模块
sys.path.insert(0, str(Path(__file__).parent))
from phigros_decrypt import (
    load_tables, PhigrosCipher, process_save, save_to_xml,
)

# 数值硬边界：Unity PlayerPrefs 的 int/float 是 C# int32/float32，
# 输入超出位宽会在游戏端解析异常，一律拦截。
INT32_MIN, INT32_MAX = -2 ** 31, 2 ** 31 - 1
FLOAT32_MAX = 3.4028235e38


# 默认查找 LocalSave_reference.cs 的位置
def find_cs_path():
    here = Path(__file__).parent
    candidates = [
        here / "LocalSave_reference.cs",
        here.parent / "LocalSave_reference.cs",
    ]
    for p in candidates:
        if p.exists():
            return p
    return here / "LocalSave_reference.cs"


class PhigrosEditor:
    def __init__(self, root):
        self.root = root
        root.title("Phigros 存档编辑器 by FuwaMintNEKO")
        root.geometry("1100x720")
        root.minsize(900, 560)

        # 数据
        self.cipher = None          # PhigrosCipher 实例
        self.entries = []           # 解密后的条目列表
        self.current_xml_path = None
        self.tree_to_index = {}     # Treeview item id -> entries 下标
        self.current_selection = None  # 当前选中的 entries 下标

        # 先初始化 cipher
        try:
            cs_path = find_cs_path()
            tables = load_tables(cs_path)
            self.cipher = PhigrosCipher(tables)
        except Exception as e:
            messagebox.showerror(
                "加密表加载失败",
                f"无法加载 LocalSave_reference.cs：\n{e}\n\n"
                "请确保该文件与本程序在同一目录。"
            )
            root.after(100, root.destroy)
            return

        # 构建 UI
        self._build_ui()
        self._update_status("就绪。请点击 [打开存档] 选择 playerprefs.xml 文件。")

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        # 顶部工具栏
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(top, text="打开存档", command=self.open_file).pack(side=tk.LEFT)
        ttk.Button(top, text="保存（加密写回）", command=self.save_file).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top, text="另存为…", command=self.save_as_file).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top, text="重新加载", command=self.reload_file).pack(side=tk.LEFT, padx=(6, 0))

        self.file_label = ttk.Label(top, text="（未打开文件）", foreground="#666")
        self.file_label.pack(side=tk.LEFT, padx=(14, 0))

        # 搜索栏
        search_frame = ttk.Frame(self.root, padding=(8, 4))
        search_frame.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(search_frame, text="搜索：").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._on_search_change)
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))

        ttk.Label(search_frame, text="范围：").pack(side=tk.LEFT)
        self.search_scope = ttk.Combobox(
            search_frame, state="readonly", width=10,
            values=["全部", "Key", "Value"],
        )
        self.search_scope.set("全部")
        self.search_scope.bind("<<ComboboxSelected>>", lambda e: self._on_search_change())
        self.search_scope.pack(side=tk.LEFT)

        self.match_count_label = ttk.Label(search_frame, text="", foreground="#666")
        self.match_count_label.pack(side=tk.LEFT, padx=(10, 0))

        # 中部列表（Treeview）
        list_frame = ttk.Frame(self.root)
        list_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        columns = ("idx", "key", "value", "type", "status")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.heading("idx", text="#")
        self.tree.heading("key", text="Key")
        self.tree.heading("value", text="Value")
        self.tree.heading("type", text="类型")
        self.tree.heading("status", text="状态")
        self.tree.column("idx", width=50, stretch=False, anchor="e")
        self.tree.column("key", width=320, stretch=True)
        self.tree.column("value", width=420, stretch=True)
        self.tree.column("type", width=60, stretch=False, anchor="center")
        self.tree.column("status", width=70, stretch=False, anchor="center")

        # 滚动条
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        # 修改过的条目用黄色背景标记
        self.tree.tag_configure("modified", background="#fff3b0")
        self.tree.tag_configure("plain", foreground="#888")
        self.tree.tag_configure("error", foreground="#c0392b")

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        # 底部编辑区
        edit_frame = ttk.LabelFrame(self.root, text="编辑选中条目的 Value", padding=(8, 6))
        edit_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 4))
        edit_frame.columnconfigure(1, weight=1)

        ttk.Label(edit_frame, text="Key：").grid(row=0, column=0, sticky="nw", pady=(0, 4))
        self.key_display = tk.Text(edit_frame, height=2, wrap="word",
                                   state="disabled", background="#f4f4f4")
        self.key_display.grid(row=0, column=1, sticky="ew", pady=(0, 4))

        ttk.Label(edit_frame, text="Value：").grid(row=1, column=0, sticky="nw")
        self.value_edit = tk.Text(edit_frame, height=6, wrap="none")
        self.value_edit.grid(row=1, column=1, sticky="ew")
        # Value 编辑框的滚动条
        val_vsb = ttk.Scrollbar(edit_frame, orient="vertical", command=self.value_edit.yview)
        val_vsb.grid(row=1, column=2, sticky="ns")
        self.value_edit.configure(yscrollcommand=val_vsb.set)

        btn_row = ttk.Frame(edit_frame)
        btn_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Button(btn_row, text="应用修改到列表", command=self.apply_edit).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="放弃修改（恢复原值）", command=self.discard_edit).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="格式化 JSON", command=self.format_json).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="压缩 JSON", command=self.compress_json).pack(side=tk.LEFT, padx=(6, 0))
        self.edit_hint = ttk.Label(btn_row, text="", foreground="#666")
        self.edit_hint.pack(side=tk.LEFT, padx=(14, 0))

        # 底部状态栏
        status_frame = ttk.Frame(self.root, padding=(8, 4))
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar()
        ttk.Label(status_frame, textvariable=self.status_var, foreground="#333").pack(side=tk.LEFT)
        self.modified_count_var = tk.StringVar(value="已修改：0")
        ttk.Label(status_frame, textvariable=self.modified_count_var, foreground="#c0392b").pack(side=tk.RIGHT)

    # ---------------- 工具栏动作 ----------------
    def open_file(self):
        path = filedialog.askopenfilename(
            title="选择 Phigros 存档文件",
            filetypes=[("XML 文件", "*.xml"), ("所有文件", "*.*")],
            initialdir=str(Path.home()),
        )
        if not path:
            return
        self._load_file(path)

    def reload_file(self):
        if self.current_xml_path:
            self._load_file(self.current_xml_path)

    def _load_file(self, path):
        try:
            entries = process_save(path, self.cipher)
        except Exception as e:
            messagebox.showerror("解密失败", f"读取或解密文件失败：\n{e}")
            return
        self.entries = entries
        self.current_xml_path = path
        self.file_label.config(text=str(path))
        self._populate_tree(entries)
        total = len(entries)
        enc = sum(1 for e in entries if e["type"] == "string" and e.get("encrypted"))
        plain_str = sum(1 for e in entries if e["type"] == "string" and not e.get("encrypted"))
        non_str = sum(1 for e in entries if e["type"] != "string")
        self._update_status(f"已加载 {total} 条；加密 {enc}；明文 {plain_str}；非 string {non_str} 条。")
        self._update_modified_count()

    def save_file(self):
        if not self.current_xml_path:
            messagebox.showwarning("未保存", "请先打开一个存档文件。")
            return
        self._do_save(self.current_xml_path)

    def save_as_file(self):
        if not self.entries:
            messagebox.showwarning("未保存", "请先打开一个存档文件。")
            return
        path = filedialog.asksaveasfilename(
            title="另存为",
            defaultextension=".xml",
            filetypes=[("XML 文件", "*.xml"), ("所有文件", "*.*")],
            initialfile="playerprefs_new.xml",
        )
        if not path:
            return
        # 另存为时，先把当前文件内容复制到目标，再用 save_to_xml 写回（保证未修改条目原样）
        try:
            import shutil
            shutil.copy2(self.current_xml_path, path)
        except Exception as e:
            messagebox.showerror("错误", f"复制原文件失败：\n{e}")
            return
        # 备份选项关闭（因为是另存为新文件，不需要 .bak）
        self._do_save(path, backup=False)

    def _do_save(self, path, backup=True):
        modified = sum(1 for e in self.entries
                       if not e.get("value_error")
                       and e["value"] != e.get("original_value"))
        if modified == 0:
            if not messagebox.askyesno("提示", "没有条目被修改，仍然要写回文件吗？"):
                return
        try:
            n = save_to_xml(path, self.cipher, self.entries, backup=backup)
        except Exception as e:
            messagebox.showerror("保存失败", f"加密或写回失败：\n{e}")
            return
        # 保存成功后：更新 original_value，刷新状态
        for e in self.entries:
            e["original_value"] = e["value"]
        self._populate_tree(self._current_filter_entries())
        bak_msg = f"；备份：{path}.bak" if backup else ""
        self._update_status(f"已保存到 {path}；更新 {n} 条{bak_msg}。")
        self._update_modified_count()
        messagebox.showinfo("保存成功", f"已更新 {n} 条并写回：\n{path}")

    # ---------------- 列表填充与搜索 ----------------
    def _populate_tree(self, entries_subset=None):
        # 清空
        self.tree.delete(*self.tree.get_children())
        self.tree_to_index.clear()
        if entries_subset is None:
            for i, e in enumerate(self.entries):
                self._insert_entry(i, e)
        else:
            # 用 id() 集合做 O(1) 查找，避免 O(n²) 的 dict 相等比较
            subset_ids = {id(e) for e in entries_subset}
            for i, e in enumerate(self.entries):
                if id(e) in subset_ids:
                    self._insert_entry(i, e)

    def _entry_status_and_tags(self, e):
        """计算条目的状态文本和标签列表"""
        modified = (not e.get("value_error")
                    and e["value"] != e.get("original_value"))
        if e.get("value_error"):
            status = "错误"
        elif modified:
            status = "已改"
        elif e["type"] != "string":
            status = e["type"]  # int / float 等
        elif not e.get("encrypted", True):
            status = "明文"
        else:
            status = "—"

        tags = []
        if modified:
            tags.append("modified")
        if e["type"] != "string" or not e.get("encrypted", True):
            tags.append("plain")
        if e.get("value_error"):
            tags.append("error")
        return status, tags

    def _insert_entry(self, idx, e):
        key_text = e.get("key") or f"[未解密] {e['raw_key'][:40]}"
        val_text = e.get("value")
        if val_text is None:
            val_text = f"[错误] {e.get('value_error') or ''}"
        # 截断显示（避免单行过长）
        key_disp = key_text if len(key_text) <= 200 else key_text[:200] + "…"
        val_disp = val_text if len(val_text) <= 500 else val_text[:500] + "…"

        status, tags = self._entry_status_and_tags(e)

        item = self.tree.insert(
            "", "end",
            values=(idx + 1, key_disp, val_disp, e["type"], status),
            tags=tags,
        )
        self.tree_to_index[item] = idx

    def _current_filter_entries(self):
        q = self.search_var.get().strip().lower()
        scope = self.search_scope.get()
        if not q:
            return list(self.entries)
        result = []
        for e in self.entries:
            k = (e.get("key") or "").lower()
            v = (e.get("value") or "").lower()
            if scope == "Key":
                if q in k:
                    result.append(e)
            elif scope == "Value":
                if q in v:
                    result.append(e)
            else:
                if q in k or q in v:
                    result.append(e)
        return result

    def _on_search_change(self, *args):
        # 重新填充为过滤后的子集
        self._populate_tree(self._current_filter_entries())
        n = len(self.tree.get_children())
        self.match_count_label.config(text=f"匹配 {n} 条")

    # ---------------- 选中与编辑 ----------------
    def _on_tree_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        if item not in self.tree_to_index:
            return
        idx = self.tree_to_index[item]
        self.current_selection = idx
        e = self.entries[idx]

        # 显示 Key
        self.key_display.config(state="normal")
        self.key_display.delete("1.0", "end")
        key_text = e.get("key") or f"[未解密] {e['raw_key']}"
        self.key_display.insert("1.0", key_text)
        self.key_display.config(state="disabled")

        # 显示 Value
        self.value_edit.delete("1.0", "end")
        val = e.get("value")
        if val is None:
            val = f"[错误] {e.get('value_error') or ''}"
        self.value_edit.insert("1.0", val)

        # 提示
        if e.get("value_error"):
            self.edit_hint.config(text=f"该条目无法解密：{e['value_error']}，不可编辑。")
        elif e["type"] == "int":
            self.edit_hint.config(
                text=f"int 类型：请输入整数（范围 {INT32_MIN}~{INT32_MAX}，溢出将拒绝应用）。")
        elif e["type"] == "float":
            self.edit_hint.config(
                text=f"float 类型：请输入数字（float32 范围 ±{FLOAT32_MAX:g}，溢出将拒绝应用）。")
        elif e["type"] == "string" and not e.get("encrypted", True):
            self.edit_hint.config(text="明文字符串，可编辑 Value。改完点 [应用修改到列表]。")
        elif e["type"] != "string":
            self.edit_hint.config(text=f"{e['type']} 类型，可编辑 Value。改完点 [应用修改到列表]。")
        else:
            self.edit_hint.config(text="可编辑 Value。改完点 [应用修改到列表]。")

    def _on_tree_double_click(self, event):
        # 双击尝试格式化 JSON（如果 Value 是 JSON）
        if self.current_selection is None:
            return
        self.format_json()

    def apply_edit(self):
        if self.current_selection is None:
            messagebox.showinfo("提示", "请先在列表中选择一条记录。")
            return
        idx = self.current_selection
        e = self.entries[idx]
        if e.get("value_error"):
            messagebox.showwarning("不可编辑", f"该条目无法解密：{e['value_error']}")
            return
        new_val = self.value_edit.get("1.0", "end-1c")
        # 数值类型硬校验：类型错误 / 位宽溢出一律不放行（写错会破坏存档）
        if e["type"] == "int":
            try:
                v = int(new_val.strip())
            except ValueError:
                messagebox.showerror(
                    "类型错误",
                    f"该条目是 int 类型，输入 \"{new_val.strip()}\" 不是合法整数，已拒绝应用。")
                return
            if not INT32_MIN <= v <= INT32_MAX:
                messagebox.showerror(
                    "溢出",
                    f"int 超出存储范围 {INT32_MIN}~{INT32_MAX}，已拒绝应用。")
                return
        elif e["type"] == "float":
            try:
                v = float(new_val.strip())
            except ValueError:
                messagebox.showerror(
                    "类型错误",
                    f"该条目是 float 类型，输入 \"{new_val.strip()}\" 不是合法数字，已拒绝应用。")
                return
            if abs(v) > FLOAT32_MAX:
                messagebox.showerror(
                    "溢出",
                    f"float 超出 float32 存储范围 ±{FLOAT32_MAX:g}，已拒绝应用。")
                return
        e["value"] = new_val
        # 更新 Treeview 行
        self._update_tree_row(idx)
        self._update_modified_count()
        self.edit_hint.config(text="已应用到列表。点击 [保存] 写回文件。")

    def discard_edit(self):
        if self.current_selection is None:
            return
        idx = self.current_selection
        e = self.entries[idx]
        e["value"] = e.get("original_value")
        self.value_edit.delete("1.0", "end")
        self.value_edit.insert("1.0", e["value"] or "")
        self._update_tree_row(idx)
        self._update_modified_count()
        self.edit_hint.config(text="已恢复为原始值。")

    def format_json(self):
        if self.current_selection is None:
            return
        e = self.entries[self.current_selection]
        if e.get("value_error"):
            return
        cur = self.value_edit.get("1.0", "end-1c").strip()
        if not cur:
            return
        try:
            obj = json.loads(cur)
            pretty = json.dumps(obj, ensure_ascii=False, indent=2)
            self.value_edit.delete("1.0", "end")
            self.value_edit.insert("1.0", pretty)
            self.edit_hint.config(text="JSON 已格式化（未应用到列表，需点 [应用修改到列表]）。")
        except json.JSONDecodeError as ex:
            self.edit_hint.config(text=f"非合法 JSON：{ex}")

    def compress_json(self):
        if self.current_selection is None:
            return
        e = self.entries[self.current_selection]
        if e.get("value_error"):
            return
        cur = self.value_edit.get("1.0", "end-1c").strip()
        if not cur:
            return
        try:
            obj = json.loads(cur)
            compact = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            self.value_edit.delete("1.0", "end")
            self.value_edit.insert("1.0", compact)
            self.edit_hint.config(text="JSON 已压缩（未应用到列表，需点 [应用修改到列表]）。")
        except json.JSONDecodeError as ex:
            self.edit_hint.config(text=f"非合法 JSON：{ex}")

    def _update_tree_row(self, idx):
        # 找到对应的 tree item 并更新
        for item, i in self.tree_to_index.items():
            if i == idx:
                e = self.entries[idx]
                key_text = e.get("key") or f"[未解密] {e['raw_key'][:40]}"
                val_text = e.get("value") or ""
                key_disp = key_text if len(key_text) <= 200 else key_text[:200] + "…"
                val_disp = val_text if len(val_text) <= 500 else val_text[:500] + "…"
                status, tags = self._entry_status_and_tags(e)
                self.tree.item(item, values=(idx + 1, key_disp, val_disp, e["type"], status), tags=tags)
                break

    # ---------------- 状态栏 ----------------
    def _update_status(self, msg):
        self.status_var.set(msg)

    def _update_modified_count(self):
        n = sum(1 for e in self.entries
                if not e.get("value_error")
                and e["value"] != e.get("original_value"))
        self.modified_count_var.set(f"已修改：{n}")


def main():
    root = tk.Tk()
    # 尝试使用更现代的主题
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    app = PhigrosEditor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
