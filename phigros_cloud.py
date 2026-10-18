#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 延迟求值类型注解：LCClient 等类定义在引用它们的函数之后，
# 加此行后 3.13 及更早版本（立即求值注解）也能正常导入。
from __future__ import annotations
"""
Phigros 云存档工具 by FuwaMintNEKO
==================================
用于验证「能否远程读写 Phigros 云存档」这一安全问题而编写的学习工具。

背景（安全原理）:
  Phigros 云存档 = 客户端直连 LeanCloud（BaaS），
  存档 zip 内每个文件 = 1 字节版本号 + AES-256-CBC 密文（PKCS7）。
  zip 没有签名 / MAC，密钥硬编码在游戏 APK 里，且服务器不校验内容，
  因此任何持有 session token 的人都能下载 -> 解密 -> 修改 -> 重新加密上传。

本工具严格遵守以下边界:
  * 仅作用于 session token 对应的账号「自己」的存档
  * 写操作前自动下载原始存档备份
  * 仅供学习交流，请勿用于破坏性 / 盈利性 / 大流量用途

用法:
  python phigros_cloud.py info    <token> [--region cn|intl]            验证 token，显示玩家与存档摘要
  python phigros_cloud.py dump    <token> [--region cn|intl] [-o out.json]  下载并解密，列出全部曲目分数
  python phigros_cloud.py backup  <token> [--region cn|intl]            只读：下载原始存档 zip 到本地
  python phigros_cloud.py modify  <token> <曲目ID> <EZ|HD|IN|AT> <分数> <ACC> [--region] [--yes]
      修改指定曲目分数并上传回服务器（自动备份 + 上传后回读验证）
  python phigros_cloud.py upload  <token> <zip文件> [--region] [--yes]  上传本地制作好的存档 zip
  python phigros_cloud.py all-unlock <token> [--challenge 551] [--money-kib 9999] [--money-mib 9999]
      [--money-gib 999] [-o out.zip] [--region] [--yes]
      生成全开档：彩51 挑战码 + 货币全满 + 隐藏曲(SP/IG/RT)全难度解锁。
      默认只生成本地 zip；传 --yes 才上传并回读验证。

依赖: pip install requests pycryptodome
"""

import argparse
import base64
import hashlib
import io
import json
import os
import struct
import sys
import time
import zipfile
from datetime import datetime, timezone

import requests
from Crypto.Cipher import AES as PyAES

# ---------------------------------------------------------------------------
# 常量（逆向自游戏 APK / 公开逆向项目，详见 PhigrosLibraryCSharp）
# ---------------------------------------------------------------------------

REGIONS = {
    "cn": {
        "base": "https://rak3ffdi.cloud.tds1.tapapis.cn",
        "client_id": "rAK3FfdieFob2Nn8Am",
        "app_key": "Qr9AEqtuoSVS3zeD6iVbM4ZC0AtkJcQ89tywVyi0",
    },
    "intl": {
        "base": "https://kviehlel.cloud.ap-sg.tapapis.com",
        "client_id": "kviehleldgxsagpozb",
        "app_key": "tG9CTm0LDD736k9HMM9lBZrbeBGRmUkjSfNLDNib",
    },
}

AES_KEY = base64.b64decode("6Jaa0qVAJZuXkZCLiOa/Ax5tIZVu+taKUN1V1nqwkks=")  # AES-256
AES_IV = base64.b64decode("Kk/wisgNYwcAV8WVGMgyUw==")                       # AES-128 IV

USER_AGENT = "LeanCloud-CSharp-SDK/1.0.3"
DIFFS = ["EZ", "HD", "IN", "AT"]

# ---------------------------------------------------------------------------
# 数值校验：硬边界（类型/溢出，越界会损坏序列化，一律拦截）
#             vs 常规范围（游戏设计约定，仅提示不拦截）
# ---------------------------------------------------------------------------
INT32_MIN, INT32_MAX = -2 ** 31, 2 ** 31 - 1
UINT16_MAX = 65535
BYTE_MAX = 255
VARINT_MAX = 32767            # 本工具两字节 LEB128 上限 (0x7F | 0xFF << 7)
FLOAT32_MAX = 3.4028235e38    # IEEE-754 单精度上限

# 游戏设计常规范围（仅提示，不拦截）
SCORE_RANGE = (0, 1000000)
ACC_RANGE = (0.0, 100.0)


def check_int32(v: int, name: str):
    """int32 位宽硬校验：超出后 struct.pack('<i') 会溢出，一律拦截"""
    if not INT32_MIN <= v <= INT32_MAX:
        raise ValueError(f"{name} 超出 int32 存储范围 {INT32_MIN}~{INT32_MAX}")


def check_float32(v: float, name: str):
    """float32 位宽硬校验：超出后 struct.pack('<f') 会溢出，一律拦截"""
    if abs(v) > FLOAT32_MAX:
        raise ValueError(f"{name} 超出 float32 存储范围 ±{FLOAT32_MAX:g}")


def parse_int_field(s, name: str, vmin: int, vmax: int) -> int:
    """类型错误 + 位宽溢出硬校验（不通过抛 ValueError）"""
    try:
        v = int(s)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是整数")
    if not vmin <= v <= vmax:
        raise ValueError(f"{name} 超出范围 {vmin}~{vmax}")
    return v


def parse_float_field(s, name: str) -> float:
    """类型错误 + float32 溢出硬校验（不通过抛 ValueError）"""
    try:
        v = float(s)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是数字")
    check_float32(v, name)
    return v

# ---------------------------------------------------------------------------
# AES 工具
# ---------------------------------------------------------------------------


def aes_decrypt(data: bytes) -> bytes:
    """AES-256-CBC 解密并去除 PKCS7 填充"""
    plain = PyAES.new(AES_KEY, PyAES.MODE_CBC, AES_IV).decrypt(data)
    pad = plain[-1]
    if pad < 1 or pad > 16:
        raise ValueError(f"PKCS7 填充非法: {pad}")
    return plain[:-pad]


def aes_encrypt(data: bytes) -> bytes:
    """PKCS7 填充后 AES-256-CBC 加密"""
    pad = 16 - (len(data) % 16)
    data = data + bytes([pad]) * pad
    return PyAES.new(AES_KEY, PyAES.MODE_CBC, AES_IV).encrypt(data)


# ---------------------------------------------------------------------------
# 变长整数 / 字符串（LEB128 风格，最多 2 字节）
# ---------------------------------------------------------------------------


def read_varint(data: bytes, pos: int):
    b = data[pos]
    if b < 128:
        return b, pos + 1
    val = (b & 0x7F) | (data[pos + 1] << 7)
    return val, pos + 2


def write_varint(val: int) -> bytes:
    if val < 128:
        return bytes([val])
    return bytes([(val & 0x7F) | 0x80, (val >> 7) & 0xFF])


def write_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return write_varint(len(b)) + b


# ---------------------------------------------------------------------------
# gameRecord 解析 / 序列化（版本 1）
# ---------------------------------------------------------------------------


def parse_game_record(data: bytes):
    """解密后的 gameRecord -> [{id, diff, score, acc, fc}]"""
    records = []
    pos = 0
    count, pos = read_varint(data, pos)
    for _ in range(count):
        id_len, pos = read_varint(data, pos)
        sid = data[pos:pos + id_len].decode("utf-8", "replace")
        pos += id_len
        rec_len = data[pos]
        pos += 1
        end = pos + rec_len
        exists = data[pos]
        pos += 1
        fc_flag = data[pos]
        pos += 1
        for d in range(4):
            if exists & (1 << d):
                score, = struct.unpack_from("<i", data, pos)
                pos += 4
                acc, = struct.unpack_from("<f", data, pos)
                pos += 4
                records.append({
                    "id": sid, "diff": d,
                    "score": score, "acc": round(acc, 6),
                    "fc": bool(fc_flag & (1 << d)),
                })
        pos = end
    return records


def serialize_game_record(records: list) -> bytes:
    """[{id, diff, score, acc, fc}] -> 二进制（按 id 分组，组内按难度升序）"""
    groups = {}
    order = []
    for r in records:
        if r["id"] not in groups:
            groups[r["id"]] = []
            order.append(r["id"])
        groups[r["id"]].append(r)

    out = bytearray()
    out += write_varint(len(order))
    for sid in order:
        g = sorted(groups[sid], key=lambda x: x["diff"])
        exists = 0
        fc_flag = 0
        for r in g:
            exists |= 1 << r["diff"]
            if r["fc"]:
                fc_flag |= 1 << r["diff"]
        out += write_string(sid)
        out += bytes([2 + 8 * len(g), exists, fc_flag])
        for r in g:
            out += struct.pack("<i", r["score"])
            out += struct.pack("<f", r["acc"])
    return bytes(out)


# ---------------------------------------------------------------------------
# Summary 解析（仅用于显示；写回时保持服务器原值不修改）
# ---------------------------------------------------------------------------


def parse_summary(b64: str):
    b = base64.b64decode(b64)
    pos = 0
    save_version = b[pos]; pos += 1
    challenge, = struct.unpack_from("<H", b, pos); pos += 2
    rks, = struct.unpack_from("<f", b, pos); pos += 4
    game_version, pos = read_varint(b, pos)
    alen, pos = read_varint(b, pos)
    avatar = b[pos:pos + alen].decode("utf-8", "replace"); pos += alen
    counts = []
    for _ in range(4):
        c, fc, phi = struct.unpack_from("<hhh", b, pos); pos += 6
        counts.append({"cleared": c, "fc": fc, "phi": phi})
    return {
        "save_version": save_version,
        "challenge": challenge,
        "rks": rks,
        "game_version": game_version,
        "avatar": avatar,
        "counts": counts,
    }


def serialize_summary(s: dict) -> str:
    """把 summary dict 序列化回 base64（与 parse_summary 互逆）。

    用于上传时同步 _GameSave 对象的 summary 字段。
    注意：RKS / counts 需依赖谱面定数表与游玩记录口径才能重算，
    本工具不重算它们，只透传原值；游戏本体下次同步时会自行重算纠正。
    """
    w = Writer()
    w.write_byte(s["save_version"])
    w.write_ushort(s["challenge"])
    w.write_float(s["rks"])
    w.write_varint(s["game_version"])
    w.write_string(s["avatar"])
    for c in s["counts"]:
        w.write_short(c["cleared"])
        w.write_short(c["fc"])
        w.write_short(c["phi"])
    return base64.b64encode(w.dump()).decode()


# ---------------------------------------------------------------------------
# 通用二进制读写器（对照 C# ByteReader/ByteWriter）
# ---------------------------------------------------------------------------


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    @property
    def has_more(self) -> bool:
        return self.pos < len(self.data)

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    def read_byte(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_bytes(self, n: int) -> bytes:
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def read_varint(self) -> int:
        b = self.read_byte()
        if b < 128:
            return b
        return (b & 0x7F) | (self.read_byte() << 7)

    def read_string(self) -> str:
        return self.read_bytes(self.read_varint()).decode("utf-8", "replace")

    def read_short(self) -> int:
        v, = struct.unpack_from("<h", self.data, self.pos)
        self.pos += 2
        return v

    def read_ushort(self) -> int:
        v, = struct.unpack_from("<H", self.data, self.pos)
        self.pos += 2
        return v

    def read_float(self) -> float:
        v, = struct.unpack_from("<f", self.data, self.pos)
        self.pos += 4
        return v


class Writer:
    def __init__(self):
        self.parts = []

    def write_byte(self, b: int):
        self.parts.append(bytes([b & 0xFF]))

    def write_bytes(self, b: bytes):
        self.parts.append(bytes(b))

    def write_varint(self, v: int):
        self.parts.append(write_varint(v))

    def write_string(self, s: str):
        self.parts.append(write_string(s))

    def write_short(self, v: int):
        self.parts.append(struct.pack("<h", v))

    def write_ushort(self, v: int):
        self.parts.append(struct.pack("<H", v))

    def write_float(self, v: float):
        self.parts.append(struct.pack("<f", v))

    def dump(self) -> bytes:
        return b"".join(self.parts)


def _pack4_bools(d: dict, names: list) -> int:
    """把 4 个 bool 打包成一个字节（bit0~bit3）"""
    packed = 0
    for i, name in enumerate(names):
        if d[name]:
            packed |= 1 << i
    return packed


def _unpack4_bools(packed: int, names: list) -> dict:
    return {name: bool((packed >> i) & 1) for i, name in enumerate(names)}


# ---------------------------------------------------------------------------
# settings 条目（版本1）
#   packed bools: ChordSupport / FcApIndicatorOn / EnableHitSound / LowResolutionModeOn
#   string DeviceName + 6 float: 背景亮度/音乐音量/音效音量/打击音音量/延迟偏移/音符大小
# ---------------------------------------------------------------------------

SETTINGS_BOOL_NAMES = ["chord_support", "fc_ap_indicator_on", "enable_hit_sound", "low_resolution_mode_on"]
SETTINGS_FLOAT_NAMES = [
    "background_brightness", "music_volume", "effect_volume",
    "hit_sound_volume", "sound_offset", "note_scale",
]


def parse_settings(data: bytes) -> dict:
    r = Reader(data)
    return {
        "bools": _unpack4_bools(r.read_byte(), SETTINGS_BOOL_NAMES),
        "device_name": r.read_string(),
        **{name: r.read_float() for name in SETTINGS_FLOAT_NAMES},
    }


def serialize_settings(s: dict) -> bytes:
    w = Writer()
    w.write_byte(_pack4_bools(s["bools"], SETTINGS_BOOL_NAMES))
    w.write_string(s["device_name"])
    for name in SETTINGS_FLOAT_NAMES:
        w.write_float(s[name])
    return w.dump()


# ---------------------------------------------------------------------------
# user 条目（版本1）
#   byte ShowUserId + string Intro + string AvatarId + string BackgroundId
# ---------------------------------------------------------------------------


def parse_user_info(data: bytes) -> dict:
    r = Reader(data)
    return {
        "show_user_id": bool(r.read_byte()),
        "intro": r.read_string(),
        "avatar_id": r.read_string(),
        "background_id": r.read_string(),
    }


def serialize_user_info(u: dict) -> bytes:
    w = Writer()
    w.write_byte(1 if u["show_user_id"] else 0)
    w.write_string(u["intro"])
    w.write_string(u["avatar_id"])
    w.write_string(u["background_id"])
    return w.dump()


# ---------------------------------------------------------------------------
# gameProgress 条目（版本4）
# ---------------------------------------------------------------------------

PROGRESS_BOOL_NAMES = [
    "is_first_run", "legacy_chapter_finished",
    "already_show_collection_tip", "already_show_auto_unlock_in_tip",
]


def parse_game_progress(data: bytes) -> dict:
    r = Reader(data)
    g = {
        "bools": _unpack4_bools(r.read_byte(), PROGRESS_BOOL_NAMES),
        "completed": r.read_string(),          # 如 "" 或 "3.0"
        "song_update_info": r.read_varint(),
        "challenge": r.read_ushort(),
        "money": [r.read_varint() for _ in range(5)],  # KiB MiB GiB TiB PiB
        "spasmodic_flag": r.read_byte(),       # DifficultyUnlockFlag
        "igallta_flag": r.read_byte(),
        "rrharil_flag": r.read_byte(),
        "song_record_flag": r.read_byte(),     # SongRecordFlag
    }
    # 剩余字节：Node2(可选) 等新版本数据，原样保留以保证往返一致
    g["extra"] = r.read_bytes(r.remaining)
    return g


def serialize_game_progress(g: dict) -> bytes:
    w = Writer()
    w.write_byte(_pack4_bools(g["bools"], PROGRESS_BOOL_NAMES))
    w.write_string(g["completed"])
    w.write_varint(g["song_update_info"])
    w.write_ushort(g["challenge"])
    for m in g["money"]:
        w.write_varint(m)
    w.write_byte(g["spasmodic_flag"])
    w.write_byte(g["igallta_flag"])
    w.write_byte(g["rrharil_flag"])
    w.write_byte(g["song_record_flag"])
    w.write_bytes(g["extra"])
    return w.dump()


# ---------------------------------------------------------------------------
# gameKey 条目（版本3）
#   varint count + 每项[byte len + string + GameKeyFlag(byte len + byte type + payload)]
#   + byte LanotaReadKeys + 可选 Node2
# ---------------------------------------------------------------------------

GAMEKEY_FLAG_NAMES = {
    0: "已读收藏碎片数", 1: "解锁单曲", 2: "已解锁收藏碎片数",
    3: "解锁插画", 4: "解锁头像",
}


def parse_game_key(data: bytes) -> dict:
    r = Reader(data)
    count = r.read_varint()
    keys = []
    for _ in range(count):
        name = r.read_bytes(r.read_byte()).decode("utf-8", "replace")
        flag_len = r.read_byte()
        flag_raw = r.read_bytes(flag_len)
        keys.append({"name": name, "flag_raw": flag_raw})
    lanota = r.read_byte() if r.has_more else 0
    extra = r.read_bytes(r.remaining)
    return {"keys": keys, "lanota_read_keys": lanota, "extra": extra}


def serialize_game_key(gk: dict) -> bytes:
    w = Writer()
    w.write_varint(len(gk["keys"]))
    for k in gk["keys"]:
        name_bytes = k["name"].encode("utf-8")
        w.write_byte(len(name_bytes))
        w.write_bytes(name_bytes)
        w.write_byte(len(k["flag_raw"]))
        w.write_bytes(k["flag_raw"])
    w.write_byte(gk["lanota_read_keys"])
    w.write_bytes(gk["extra"])
    return w.dump()


def describe_game_key_flag(flag_raw: bytes) -> str:
    """把 GameKeyFlag 原始字节翻译成人话，仅用于显示"""
    if not flag_raw:
        return "空"
    t = flag_raw[0]
    parts = []
    idx = 0
    for bit in range(8):
        if t & (1 << bit):
            val = flag_raw[1 + idx] if idx < len(flag_raw) - 1 else None
            idx += 1
            parts.append(f"{GAMEKEY_FLAG_NAMES.get(bit, f'bit{bit}')}={val}")
    return ", ".join(parts) if parts else f"未知type={t:#x}"


# ---------------------------------------------------------------------------
# 存档整体加载 / 重建
# ---------------------------------------------------------------------------


def load_cloud_save(client: LCClient) -> dict:
    """下载并完整解析云存档，返回可编辑的 dict（读操作，不改服务器）"""
    obj_id, zip_bytes, summary_b64 = fetch_save(client)
    entries = decrypt_zip(zip_bytes)
    return {
        "object_id": obj_id,
        "raw_zip": zip_bytes,                  # 原始 zip，用于备份
        "summary_b64": summary_b64,
        "summary": parse_summary(summary_b64) if summary_b64 else None,
        "versions": {n: entries[n][0] for n in entries},
        "settings": parse_settings(entries["settings"][1]),
        "user": parse_user_info(entries["user"][1]),
        "game_progress": parse_game_progress(entries["gameProgress"][1]),
        "game_record": parse_game_record(entries["gameRecord"][1]),
        "game_key": parse_game_key(entries["gameKey"][1]),
    }


def build_save_zip(data: dict) -> bytes:
    """把编辑后的存档 dict 重新序列化并加密为 zip"""
    entries = {
        "settings": (data["versions"]["settings"], serialize_settings(data["settings"])),
        "user": (data["versions"]["user"], serialize_user_info(data["user"])),
        "gameProgress": (data["versions"]["gameProgress"], serialize_game_progress(data["game_progress"])),
        "gameRecord": (data["versions"]["gameRecord"], serialize_game_record(data["game_record"])),
        "gameKey": (data["versions"]["gameKey"], serialize_game_key(data["game_key"])),
    }
    return encrypt_zip(entries)


def upload_save_zip(client: LCClient, obj_id: str, zip_bytes: bytes, summary_b64: str = None) -> str:
    """上传存档 zip 并更新 _GameSave 记录，返回 fileId。

    流程（与游戏 SDK 一致，2026 年现行版 LeanCloud/TapTap 接口）:
      1. POST /1.1/fileTokens          -> 拿到 qiniu 上传令牌 + key + 文件 objectId
      2. 用令牌把文件内容直传 qiniu     -> 文件对象随即获得可访问 URL
      3. PUT /1.1/classes/_GameSave/{obj_id}  -> 更新 gameFile 指针（可同时更新 summary 字段）

    summary_b64: 可选。游戏端把摘要（RKS/挑战码/统计）单独存在 _GameSave.summary，
                 若不同步更新，游戏端读取挑战码等会继续用旧值。

    同步时间：游戏端「立即同步」弹窗显示的云端同步时间取自 _GameSave.modifiedAt
    （游戏上传时自写的时间戳）。我们直接 PUT zip 时若不更新它，游戏会认为云端
    仍是旧存档（云端时间不新），从而不弹选择框、直接用本地覆盖云端。
    因此这里把 modifiedAt 更新为当前时间，让游戏端认为云端是刚同步过的新存档。
    """
    filename = ".save"
    ft = client.post_json("/1.1/fileTokens", {
        "name": filename,
        "mime_type": "application/octet-stream",
        "metaData": {"prefix": "gamesaves", "size": len(zip_bytes)},
        "prefix": "gamesaves",
        "provider": "qiniu",
    })
    file_id = ft.get("objectId")
    upload_url = ft.get("upload_url", "https://upload.qiniup.com")
    # 直传 qiniu
    r = requests.post(upload_url,
                      data={"token": ft.get("token"), "key": ft.get("key")},
                      files={"file": (filename, io.BytesIO(zip_bytes), "application/octet-stream")},
                      timeout=60)
    r.raise_for_status()
    # 更新 _GameSave 记录：指向新文件 + 刷新云端同步时间（并同步 summary）
    now = datetime.now(timezone.utc)
    iso = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    payload = {
        "gameFile": {"__type": "File", "id": file_id, "name": filename},
        "modifiedAt": {"__type": "Date", "iso": iso},
    }
    if summary_b64:
        payload["summary"] = summary_b64
    client.put_json(f"/1.1/classes/_GameSave/{obj_id}", payload)
    return file_id


def restore_save_zip(client: LCClient, obj_id: str, zip_bytes: bytes, saved_summary_b64: str = None) -> str:
    """还原存档 zip，并保证 _GameSave.summary 与 zip 内容一致。

    - 有备份的 summary（saved_summary_b64，由 backup 命令生成 .summary 文件）则完整还原；
    - 否则从 zip 内 gameProgress.challenge 重建 summary 的 challenge 字段
      （RKS/counts 无法从 zip 精确重建，保持服务器现值，游戏本体下次同步会自行重算纠正）。
    返回上传后的 fileId。
    """
    summary_b64 = saved_summary_b64
    if not summary_b64:
        try:
            entries = decrypt_zip(zip_bytes)
            challenge = parse_game_progress(entries["gameProgress"][1])["challenge"]
            save = client.get_json(f"/1.1/classes/_GameSave/{obj_id}")
            cur = save.get("summary", "")
            if cur:
                s = parse_summary(cur)
                s["challenge"] = challenge
                summary_b64 = serialize_summary(s)
        except Exception:
            summary_b64 = None  # 同步失败不阻塞还原本身
    return upload_save_zip(client, obj_id, zip_bytes, summary_b64=summary_b64)


# ---------------------------------------------------------------------------
# LeanCloud HTTP 客户端
# ---------------------------------------------------------------------------


class LCClient:
    def __init__(self, token: str, region: str = "cn"):
        cfg = REGIONS[region]
        self.base = cfg["base"]
        self.app_key = cfg["app_key"]
        self.session = requests.Session()
        self.session.headers.update({
            "X-LC-Id": cfg["client_id"],
            "X-LC-Key": cfg["app_key"],
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "X-LC-Session": token,
        })

    def _sign(self) -> str:
        """LeanCloud 请求签名: MD5(timestamp_ms + appKey),timestamp_ms"""
        ts = int(time.time() * 1000)
        digest = hashlib.md5(f"{ts}{self.app_key}".encode()).hexdigest()
        return f"{digest},{ts}"

    def _ensure_ok(self, r: requests.Response, what: str):
        if not r.ok:
            try:
                body = r.json()
            except Exception:
                body = r.text[:500]
            raise RuntimeError(f"{what} 失败 [{r.status_code}]: {body}")

    def get_json(self, path: str, params=None) -> dict:
        r = self.session.get(self.base + path, params=params)
        self._ensure_ok(r, "GET " + path)
        return r.json()

    def put_json(self, path: str, payload: dict) -> dict:
        self.session.headers["X-LC-Sign"] = self._sign()
        r = self.session.put(self.base + path, json=payload)
        self.session.headers.pop("X-LC-Sign", None)
        self._ensure_ok(r, "PUT " + path)
        return r.json()

    def post_json(self, path: str, payload: dict) -> dict:
        self.session.headers["X-LC-Sign"] = self._sign()
        r = self.session.post(self.base + path, json=payload)
        self.session.headers.pop("X-LC-Sign", None)
        self._ensure_ok(r, "POST " + path)
        return r.json()


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------


def fetch_save(client: LCClient):
    """返回 (save_object_id, 存档 zip bytes, summary_base64)"""
    me = client.get_json("/1.1/users/me")
    uid = me.get("objectId")
    where = json.dumps({"user": {"__type": "Pointer", "className": "_User", "objectId": uid}})
    results = client.get_json("/1.1/classes/_GameSave", params={"where": where}).get("results", [])
    if not results:
        raise RuntimeError("该账号没有云存档记录（可能从未上传过）")
    save = results[0]
    zip_url = save["gameFile"]["url"]
    obj_id = save["objectId"]
    summary = save.get("summary", "")
    r = requests.get(zip_url)
    r.raise_for_status()
    return obj_id, r.content, summary


def decrypt_zip(zip_bytes: bytes) -> dict:
    """zip -> {文件名: (版本字节, 解密后的数据)}"""
    entries = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            raw = z.read(name)
            version = raw[0]
            entries[name] = (version, aes_decrypt(raw[1:]))
    return entries


def encrypt_zip(entries: dict) -> bytes:
    """{文件名: (版本字节, 明文)} -> zip bytes"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, (version, plain) in entries.items():
            z.writestr(name, bytes([version]) + aes_encrypt(plain))
    return buf.getvalue()


def human_player(client: LCClient) -> str:
    me = client.get_json("/1.1/users/me")
    return (
        f"昵称: {me.get('nickname')}\n"
        f"用户名: {me.get('username')}\n"
        f"objectId: {me.get('objectId')}\n"
        f"创建时间: {me.get('createdAt')}\n"
        f"更新时间: {me.get('updatedAt')}"
    )


def _confirm(msg: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_info(args):
    client = LCClient(args.token, args.region)
    print("=== 玩家信息 ===")
    print(human_player(client))
    obj_id, zip_bytes, summary = fetch_save(client)
    print("\n=== 云存档 ===")
    print(f"存档记录 objectId: {obj_id}")
    print(f"存档 zip 大小: {len(zip_bytes)} 字节")
    if summary:
        try:
            s = parse_summary(summary)
            print("\n=== 摘要（服务器端 summary 字段）===")
            print(f"存档版本: {s['save_version']}  游戏版本: {s['game_version']}")
            print(f"RKS: {s['rks']:.4f}  Challenge: {s['challenge']}  头像: {s['avatar']}")
            for d, c in zip(DIFFS, s["counts"]):
                print(f"  {d}: 通关 {c['cleared']}  全连 {c['fc']}  φ {c['phi']}")
        except Exception as e:
            print(f"（summary 解析失败，仅显示：{e}）")
            print("summary base64:", summary[:80], "...")


def cmd_dump(args):
    client = LCClient(args.token, args.region)
    obj_id, zip_bytes, summary = fetch_save(client)
    entries = decrypt_zip(zip_bytes)
    print(f"zip 内条目: {list(entries.keys())}")
    records = parse_game_record(entries["gameRecord"][1])
    print(f"\n共 {len(records)} 条曲目记录：")
    for r in records:
        print(f"  {r['id']:<40} {DIFFS[r['diff']]:<3} score={r['score']:>7} acc={r['acc']:>8.4f} {'FC' if r['fc'] else ''}")
    if args.output:
        data = {
            "objectId": obj_id,
            "summary": parse_summary(summary) if summary else None,
            "records": records,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n已保存到 {args.output}")


def cmd_backup(args):
    client = LCClient(args.token, args.region)
    obj_id, zip_bytes, summary = fetch_save(client)
    path = f"backup_{int(time.time() * 1000)}.zip"
    with open(path, "wb") as f:
        f.write(zip_bytes)
    if summary:
        with open(path + ".summary", "w") as f:
            f.write(summary)
        print(f"已备份原始存档到: {path} ({len(zip_bytes)} 字节, objectId={obj_id})")
        print(f"同时保存摘要: {path}.summary（还原时一并恢复 summary 字段）")
    else:
        print(f"已备份原始存档到: {path} ({len(zip_bytes)} 字节, objectId={obj_id}, 无 summary)")


def cmd_modify(args):
    if args.diff not in DIFFS:
        print(f"难度必须是 {'/'.join(DIFFS)}")
        sys.exit(1)
    # 硬边界：int32 / float32 位宽溢出会破坏序列化，一律拦截（类型由 argparse 保证）
    try:
        check_int32(args.score, "分数")
    except ValueError as e:
        print(f"[错误] {e}")
        sys.exit(1)
    try:
        check_float32(args.acc, "ACC")
    except ValueError as e:
        print(f"[错误] {e}")
        sys.exit(1)
    # 常规范围（游戏设计约定）：仅提示，不拦截
    if not SCORE_RANGE[0] <= args.score <= SCORE_RANGE[1]:
        print(f"[提示] 分数 {args.score} 超出常规范围 {SCORE_RANGE[0]}~{SCORE_RANGE[1]}，将按原样写入")
    if not ACC_RANGE[0] <= args.acc <= ACC_RANGE[1]:
        print(f"[提示] ACC {args.acc} 超出常规范围 {ACC_RANGE[0]}~{ACC_RANGE[1]}，将按原样写入")

    print("=" * 60)
    print("  警告：此操作会把修改后的存档上传到服务器！")
    print("  将自动备份原始存档；只影响本 token 对应账号。")
    print("  仅供学习交流使用，后果自负。")
    print("=" * 60)

    client = LCClient(args.token, args.region)
    obj_id, zip_bytes, _ = fetch_save(client)

    # 备份
    bak_path = f"backup_{int(time.time() * 1000)}.zip"
    with open(bak_path, "wb") as f:
        f.write(zip_bytes)
    print(f"\n[1/4] 已备份原始存档: {bak_path}")

    # 解密
    entries = decrypt_zip(zip_bytes)
    records = parse_game_record(entries["gameRecord"][1])

    target = next((r for r in records if r["id"] == args.song and r["diff"] == DIFFS.index(args.diff)), None)
    if target:
        old = f"score={target['score']}, acc={target['acc']}, fc={target['fc']}"
        target["score"] = args.score
        target["acc"] = args.acc
        print(f"[2/4] 找到记录 {args.song}.{args.diff}，修改: ({old}) -> (score={args.score}, acc={args.acc})")
    else:
        records.append({"id": args.song, "diff": DIFFS.index(args.diff),
                        "score": args.score, "acc": args.acc, "fc": False})
        print(f"[2/4] 未找到 {args.song}.{args.diff}，新增一条记录 score={args.score}, acc={args.acc}")

    # 重新序列化并加密
    entries["gameRecord"] = (entries["gameRecord"][0], serialize_game_record(records))
    new_zip = encrypt_zip(entries)
    print(f"[3/4] 已重新加密，新 zip {len(new_zip)} 字节")

    if not _confirm("\n确认上传到服务器？", args.yes):
        print("已取消。")
        return

    # 上传文件 + 更新记录
    file_id = upload_save_zip(client, obj_id, new_zip)
    print(f"[4/4] 上传成功 (fileId={file_id})")

    # 回读验证
    print("\n=== 回读验证 ===")
    obj_id2, zip_bytes2, _ = fetch_save(client)
    entries2 = decrypt_zip(zip_bytes2)
    records2 = parse_game_record(entries2["gameRecord"][1])
    found = next((r for r in records2 if r["id"] == args.song and r["diff"] == DIFFS.index(args.diff)), None)
    if found and found["score"] == args.score:
        print(f"验证通过：服务器上的 {args.song}.{args.diff} 分数已是 {found['score']}，acc={found['acc']}")
        print("结论：远程修改云存档内容 ✓ 确实可行（服务器无完整性校验）")
    else:
        print(f"验证失败！期望 score={args.score}，服务器返回: {found}")
        sys.exit(1)


def cmd_upload(args):
    print("=" * 60)
    print("  警告：此操作会把指定 zip 上传到服务器并覆盖云存档！")
    print("  仅供学习交流使用，后果自负。")
    print("=" * 60)
    with open(args.zipfile, "rb") as f:
        zip_bytes = f.read()
    client = LCClient(args.token, args.region)
    obj_id, _, _ = fetch_save(client)
    if not _confirm(f"将用 {args.zipfile} ({len(zip_bytes)} 字节) 覆盖云存档，确认？", args.yes):
        print("已取消。")
        return
    # 若旁边有同名 .summary 文件（backup 命令生成的），一并还原 summary
    saved_summary = None
    spath = args.zipfile + ".summary"
    if os.path.exists(spath):
        with open(spath) as f:
            saved_summary = f.read().strip()
    file_id = restore_save_zip(client, obj_id, zip_bytes, saved_summary_b64=saved_summary)
    print(f"上传成功 (fileId={file_id})")


def cmd_all_unlock(args):
    """生成全开档：彩51 挑战码 + 货币全满 + 隐藏曲(SP/IG/RT)全难度解锁。

    只修改 gameProgress（不动曲目成绩/收藏），默认只把全开档 zip 存到本地，
    传 --yes 才会真正上传覆盖云存档，并回读验证。
    """
    if not 0 <= args.challenge <= UINT16_MAX:
        print(f"[错误] challenge 超出范围 0~{UINT16_MAX}")
        sys.exit(1)
    if not 0 <= args.money_kiB <= VARINT_MAX or not 0 <= args.money_miB <= VARINT_MAX \
            or not 0 <= args.money_giB <= VARINT_MAX:
        print(f"[错误] money 各档需在 0~{VARINT_MAX} 之间")
        sys.exit(1)

    print("=" * 60)
    print("  全开档生成：只修改 gameProgress，不影响曲目成绩与收藏碎片。")
    print("  challenge=彩51 / 货币全满 / SP·IG·RT 全难度解锁")
    print("=" * 60)

    client = LCClient(args.token, args.region)
    obj_id, zip_bytes, summary = fetch_save(client)

    # 备份
    bak_path = f"backup_{int(time.time() * 1000)}.zip"
    with open(bak_path, "wb") as f:
        f.write(zip_bytes)
    if summary:
        with open(bak_path + ".summary", "w") as f:
            f.write(summary)
    print(f"[1/5] 已备份原始存档: {bak_path}")

    # 解密 + 修改 gameProgress
    entries = decrypt_zip(zip_bytes)
    gp = parse_game_progress(entries["gameProgress"][1])
    old = {
        "challenge": gp["challenge"],
        "money": list(gp["money"]),
        "spasmodic_flag": gp["spasmodic_flag"],
        "igallta_flag": gp["igallta_flag"],
        "rrharil_flag": gp["rrharil_flag"],
        "song_record_flag": gp["song_record_flag"],
    }
    gp["challenge"] = args.challenge
    gp["money"] = [args.money_kiB, args.money_miB, args.money_giB, 0, 0]
    gp["spasmodic_flag"] = 0xFF
    gp["igallta_flag"] = 0xFF
    gp["rrharil_flag"] = 0xFF
    gp["song_record_flag"] = 0xFF
    entries["gameProgress"] = (entries["gameProgress"][0], serialize_game_progress(gp))
    new_zip = encrypt_zip(entries)

    out = args.output or f"all_unlock_{int(time.time() * 1000)}.zip"
    with open(out, "wb") as f:
        f.write(new_zip)
    if summary:
        with open(out + ".summary", "w") as f:
            f.write(summary)
    print(f"[2/5] 全开档已生成: {out} ({len(new_zip)} 字节)")
    print("      gameProgress 改动:")
    print(f"        challenge: {old['challenge']} -> {gp['challenge']}")
    print(f"        money(KiB,MiB,GiB): {old['money']} -> {gp['money']}")
    print(f"        SP/IG/RT flag: {old['spasmodic_flag']}/{old['igallta_flag']}/{old['rrharil_flag']}"
          f" -> FF/FF/FF")
    print(f"        song_record_flag: {old['song_record_flag']} -> FF")

    if not args.yes:
        print("[3/5] 未上传。先检查本地 zip，确认后上传：")
        print(f"      python phigros_cloud.py upload {args.token} {out} --yes")
        print("      或先用 phigros_cloud_gui.py 检查该 zip 内容再决定")
        return

    file_id = restore_save_zip(client, obj_id, new_zip, saved_summary_b64=summary)
    print(f"[3/5] 已上传 (fileId={file_id})")

    # 回读验证
    print("[4/5] 回读验证...")
    _, zip_bytes2, _ = fetch_save(client)
    entries2 = decrypt_zip(zip_bytes2)
    gp2 = parse_game_progress(entries2["gameProgress"][1])
    ok = (gp2["challenge"] == gp["challenge"] and
          gp2["money"] == gp["money"] and
          gp2["spasmodic_flag"] == 0xFF and gp2["igallta_flag"] == 0xFF
          and gp2["rrharil_flag"] == 0xFF)
    print(f"  回读 challenge={gp2['challenge']} money={gp2['money']} "
          f"flags={gp2['spasmodic_flag']}/{gp2['igallta_flag']}/{gp2['rrharil_flag']}")
    print(f"[5/5] {'验证通过' if ok else '验证失败！请检查'}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Phigros 云存档学习工具（远程读写验证）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = dict(region_default="cn")
    p_info = sub.add_parser("info", help="验证 token 并显示玩家信息与存档摘要")
    p_info.add_argument("token")
    p_info.add_argument("--region", choices=["cn", "intl"], default="cn")

    p_dump = sub.add_parser("dump", help="下载并解密存档，列出全部曲目分数")
    p_dump.add_argument("token")
    p_dump.add_argument("--region", choices=["cn", "intl"], default="cn")
    p_dump.add_argument("-o", "--output", help="同时保存到 JSON 文件")

    p_bak = sub.add_parser("backup", help="只读：下载原始存档 zip 到本地")
    p_bak.add_argument("token")
    p_bak.add_argument("--region", choices=["cn", "intl"], default="cn")

    p_mod = sub.add_parser("modify", help="修改指定曲目分数并上传回服务器")
    p_mod.add_argument("token")
    p_mod.add_argument("song", help="曲目 ID，如 MARENOL.LeaF.0")
    p_mod.add_argument("diff", choices=DIFFS, help="难度")
    p_mod.add_argument("score", type=int, help="分数 0~1000000")
    p_mod.add_argument("acc", type=float, help="ACC 0~100")
    p_mod.add_argument("--region", choices=["cn", "intl"], default="cn")
    p_mod.add_argument("--yes", action="store_true", help="跳过确认")

    p_up = sub.add_parser("upload", help="上传本地 zip 覆盖云存档")
    p_up.add_argument("token")
    p_up.add_argument("zipfile")
    p_up.add_argument("--region", choices=["cn", "intl"], default="cn")
    p_up.add_argument("--yes", action="store_true", help="跳过确认")

    p_au = sub.add_parser("all-unlock", help="生成全开档：彩51 + 货币全满 + 隐藏曲全解锁")
    p_au.add_argument("token")
    p_au.add_argument("--challenge", type=int, default=551, help="挑战码（百位=框色，后两位=等级），默认 551=彩51")
    p_au.add_argument("--money-kib", type=int, default=9999, dest="money_kiB", help="货币 KiB 档（默认 9999）")
    p_au.add_argument("--money-mib", type=int, default=9999, dest="money_miB", help="货币 MiB 档（默认 9999）")
    p_au.add_argument("--money-gib", type=int, default=999, dest="money_giB", help="货币 GiB 档（默认 999）")
    p_au.add_argument("-o", "--output", help="输出 zip 路径（默认 all_unlock_<时间戳>.zip）")
    p_au.add_argument("--region", choices=["cn", "intl"], default="cn")
    p_au.add_argument("--yes", action="store_true", help="生成后立即上传并回读验证")

    args = parser.parse_args()
    handler = {
        "info": cmd_info, "dump": cmd_dump, "backup": cmd_backup,
        "modify": cmd_modify, "upload": cmd_upload,
        "all-unlock": cmd_all_unlock,
    }[args.cmd]
    try:
        handler(args)
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
