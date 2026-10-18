# -*- coding: utf-8 -*-
"""
Phigros 本地存档解密器
=====================
解密 Unity PlayerPrefs 存档 (com.PigeonGames.Phigros.v2.playerprefs.xml)

算法：自定义 AES 变体（CBC 模式）
  - 硬编码 sBox / invSBox / initialVector / rcon / tempArray
  - 15 轮：初始轮 + 13 主轮 + 最终轮
  - invSBox 兼作 round key 数组（切片 [224:240], [208:16], [0:16]）
  - MixColumns 用 rcon 查表（非标准 GF(2^8)）
  - ShiftRows 用 tempArray 置换

参考：https://github.com/yt6983138/PhigrosLibraryCSharp (LocalSave.cs, MIT)

流程：
  XML <string name="key">value</string>
  -> URL decode (得到 base64)
  -> base64 decode (得到密文字节)
  -> 自定义 AES-CBC 解密
  -> 去 PKCS7 padding
  -> UTF-8 明文 (key 如 "MARENOL.LeaF.0.Record.HD"，value 如 JSON)
"""
import re
import sys
import json
import base64
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path


# ============================================================
# 从 LocalSave_reference.cs 提取硬编码表
# ============================================================
def extract_byte_array(cs_src, name):
    """从 C# 源码提取 byte[] 字面量（允许 = 与 [ 之间有注释/换行）"""
    pattern = rf'byte\[\]\s+{name}\s*=[^[]*\[(.*?)\]\s*;'
    m = re.search(pattern, cs_src, re.DOTALL)
    if not m:
        raise ValueError(f"未找到表: {name}")
    nums = re.findall(r'\d+', m.group(1))
    return bytes(int(x) for x in nums)


def load_tables(cs_path):
    """从 C# 源码加载所有加密表"""
    src = Path(cs_path).read_text(encoding="utf-8")
    tables = {
        "sBox": extract_byte_array(src, "sBox"),
        "invSBox": extract_byte_array(src, "invSBox"),
        "initialVector": extract_byte_array(src, "initialVector"),
        "rcon": extract_byte_array(src, "rcon"),
        "tempArray": extract_byte_array(src, "tempArray"),
    }
    return tables


# ============================================================
# 自定义 AES 块变换（严格按 LocalSave.cs 实现）
# ============================================================
class PhigrosCipher:
    def __init__(self, tables):
        self.sBox = tables["sBox"]
        self.invSBox = tables["invSBox"]
        self.iv = tables["initialVector"]
        self.rcon = tables["rcon"]
        self.tempArray = tables["tempArray"]

        # 预计算 sBox 的真正逆（注意：源码 invSBox 是 round key 数组，并非 sBox 的逆）
        self.inv_sBox_true = [0] * 256
        for i, v in enumerate(self.sBox):
            self.inv_sBox_true[v] = i
        # 预计算 tempArray 的逆置换：inv_tempArray[tempArray[i]] = i
        self.inv_tempArray = [0] * 16
        for i, v in enumerate(self.tempArray):
            self.inv_tempArray[v] = i

    def _sub_bytes(self, block):
        return bytes(self.sBox[b] for b in block)

    def _shift_rows(self, block):
        # new[i] = old[tempArray[i]]
        return bytes(block[self.tempArray[i]] for i in range(16))

    def _add_round_key(self, block, round_key):
        return bytes(block[i] ^ round_key[i] for i in range(16))

    def _mix_columns(self, block):
        s = bytearray(block)
        rcon = self.rcon
        for n in range(0, 16, 4):
            i0, i1, i2, i3 = s[n], s[n+1], s[n+2], s[n+3]
            sBoxColumnSum = i0 ^ i1 ^ i2 ^ i3
            rconColumnSum = rcon[sBoxColumnSum]
            doubleRconColumnSumI0I2 = rcon[rcon[rconColumnSum ^ i0 ^ i2]] ^ sBoxColumnSum
            doubleRconColumnSumI1I3 = rcon[rcon[rconColumnSum ^ i1 ^ i3]] ^ sBoxColumnSum
            s[n+0] ^= doubleRconColumnSumI0I2 ^ rcon[i0 ^ i1]
            s[n+1] ^= doubleRconColumnSumI1I3 ^ rcon[i1 ^ i2]
            s[n+2] ^= doubleRconColumnSumI0I2 ^ rcon[i2 ^ i3]
            s[n+3] ^= doubleRconColumnSumI1I3 ^ rcon[i3 ^ i0]
        return bytes(s)

    # ---------- 加密用的逆变换 ----------
    # 注：源码中的 invSBox 实际是 round key 数组（共 240 字节），不是 sBox 的逆。
    # 真正的 sBox 逆需要自行构建：inv_sBox_true[sBox[i]] = i。
    # tempArray 的逆置换：inv_tempArray[tempArray[i]] = i。
    # MixColumns 矩阵经推导为 [[14,11,13,9],[9,14,11,13],[13,9,14,11],[11,13,9,14]]，
    # 即标准 AES 的 InvMixColumns；其逆为标准 AES 的 MixColumns [[2,3,1,1],...]。
    def _inv_sub_bytes(self, block):
        return bytes(self.inv_sBox_true[b] for b in block)

    def _inv_shift_rows(self, block):
        # shift_rows: new[i] = old[tempArray[i]]  =>  old[tempArray[i]] = new[i]
        # 即 old[j] = new[inv_tempArray[j]]，对位置 j 取 inv_tempArray[j]
        return bytes(block[self.inv_tempArray[i]] for i in range(16))

    @staticmethod
    def _gmul(a, b):
        """GF(2^8) 乘法，模 0x11b（标准 AES）"""
        p = 0
        for _ in range(8):
            if b & 1:
                p ^= a
            hi = a & 0x80
            a = (a << 1) & 0xFF
            if hi:
                a ^= 0x1b
            b >>= 1
        return p

    def _inv_mix_columns(self, block):
        s = bytearray(block)
        g = self._gmul
        for n in range(0, 16, 4):
            i0, i1, i2, i3 = s[n], s[n+1], s[n+2], s[n+3]
            s[n+0] = g(i0, 2) ^ g(i1, 3) ^ i2        ^ i3
            s[n+1] = i0        ^ g(i1, 2) ^ g(i2, 3) ^ i3
            s[n+2] = i0        ^ i1        ^ g(i2, 2) ^ g(i3, 3)
            s[n+3] = g(i0, 3) ^ i1        ^ i2        ^ g(i3, 2)
        return bytes(s)

    def decrypt(self, ciphertext):
        """
        解密密文字节，返回明文 UTF-8 字符串。
        CBC 模式：每块解密后 XOR 前一块密文（首块 XOR IV）。
        """
        if len(ciphertext) % 16 != 0:
            raise ValueError(f"密文长度 {len(ciphertext)} 不是 16 的倍数")
        invSBox = self.invSBox
        data = bytearray(ciphertext)
        state = bytearray(self.iv)  # CBC 链，初始为 IV

        for e in range(0, len(data), 16):
            block = bytes(data[e:e+16])
            # 初始轮
            block = self._add_round_key(block, invSBox[224:240])
            block = self._shift_rows(block)
            block = self._sub_bytes(block)
            # 主轮：i = 208, 192, ..., 16
            for i in range(208, 15, -16):
                block = self._add_round_key(block, invSBox[i:i+16])
                block = self._mix_columns(block)
                block = self._shift_rows(block)
                block = self._sub_bytes(block)
            # 最终轮
            block = self._add_round_key(block, invSBox[0:16])
            # CBC：XOR 前一块密文
            block = bytes(block[j] ^ state[j] for j in range(16))
            # 更新 state 为当前块的原始密文
            state = bytearray(data[e:e+16])
            # 写回明文
            data[e:e+16] = block

        # PKCS7 去填充
        pad = data[-1]
        if pad < 1 or pad > 16:
            # padding 异常，可能解密失败；返回原数据
            return bytes(data)
        return bytes(data[:-pad])

    def encrypt(self, plaintext):
        """
        加密明文字节，返回密文字节。CBC 模式 + PKCS7 填充。
        流程为 decrypt 的严格逆操作：
          1) PKCS7 填充到 16 倍数
          2) 每块：先 XOR 前一块密文（CBC），再做 decrypt 块变换的逆
             逆序：add_round_key(invSBox[0:16]) -> [inv_sub, inv_shift, inv_mix, add_round_key] x13
                   -> inv_sub, inv_shift, add_round_key(invSBox[224:240])
        """
        # PKCS7 填充（即便明文恰为 16 倍数也补一整块 16）
        pad = 16 - (len(plaintext) % 16)
        plaintext = bytes(plaintext) + bytes([pad]) * pad

        invSBox = self.invSBox
        out = bytearray()
        state = bytearray(self.iv)  # CBC 链，初始为 IV

        for e in range(0, len(plaintext), 16):
            block = bytes(plaintext[e:e+16])
            # CBC：先 XOR 前一块密文
            block = bytes(block[j] ^ state[j] for j in range(16))
            # 反向最终轮
            block = self._add_round_key(block, invSBox[0:16])
            # 反向主轮：i = 16, 32, ..., 208
            for i in range(16, 209, 16):
                block = self._inv_sub_bytes(block)
                block = self._inv_shift_rows(block)
                block = self._inv_mix_columns(block)
                block = self._add_round_key(block, invSBox[i:i+16])
            # 反向初始轮
            block = self._inv_sub_bytes(block)
            block = self._inv_shift_rows(block)
            block = self._add_round_key(block, invSBox[224:240])

            out.extend(block)
            state = bytearray(block)  # CBC 链更新为当前密文

        return bytes(out)


# ============================================================
# XML 存档解析与解密
# ============================================================
def decrypt_string(cipher, url_encoded_str):
    """
    解密单个存档字符串：
    URL-encoded base64 -> base64 decode -> AES 解密 -> UTF-8
    """
    b64 = urllib.parse.unquote(url_encoded_str)
    try:
        ciphertext = base64.b64decode(b64)
    except Exception as e:
        return None, f"base64 解码失败: {e}"
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        # 非加密条目（如 unity 自带的明文 key）
        return None, "非 16 字节倍数，疑似明文"
    try:
        plaintext = cipher.decrypt(ciphertext)
        return plaintext, None
    except Exception as e:
        return None, f"解密失败: {e}"


def process_save(xml_path, cipher, output_path=None, verbose=False):
    """解析 XML，解密所有 key/value。

    条目分类：
      - string + encrypted=True : 加密字符串（key/value 均为 URL-encoded base64 密文）
      - string + encrypted=False: 明文字符串（如 unity.player_session_count，值不加密）
      - int / float / ...        : 数值类型，值在 value 属性中，明文可编辑
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()

    entries = []
    for elem in root:
        tag = elem.tag
        name = elem.get("name", "")
        text = (elem.text or "").strip()

        if tag == "string":
            # 尝试解密 key 和 value
            key_plain, key_err = decrypt_string(cipher, name)
            val_plain, val_err = decrypt_string(cipher, text)

            if key_plain is not None and val_plain is not None:
                # 加密条目：key/value 均成功解密
                key_str = key_plain.decode("utf-8", errors="replace")
                val_str = val_plain.decode("utf-8", errors="replace")
                entry = {
                    "type": "string",
                    "encrypted": True,
                    "raw_key": name,
                    "raw_value": text,
                    "key": key_str,
                    "value": val_str,
                    "original_value": val_str,
                    "key_error": None,
                    "value_error": None,
                }
            else:
                # 明文条目：key 或 value 无法解密（如 unity.player_session_count=1）
                # 直接使用原始文本作为 key 和 value
                entry = {
                    "type": "string",
                    "encrypted": False,
                    "raw_key": name,
                    "raw_value": text,
                    "key": name,
                    "value": text,
                    "original_value": text,
                    "key_error": None,
                    "value_error": None,
                }
        else:
            # int / float 等类型：值在 value 属性中
            val_attr = elem.get("value", "")
            entry = {
                "type": tag,
                "encrypted": False,
                "raw_key": name,
                "raw_value": val_attr,
                "key": name,
                "value": val_attr,
                "original_value": val_attr,
                "key_error": None,
                "value_error": None,
            }
        entries.append(entry)

    return entries


def encrypt_string(cipher, plaintext_str):
    """加密单个字符串：UTF-8 -> AES 加密 -> base64 -> URL encode
    返回与原存档格式一致的可写入 <string> 文本（URL-encoded base64）。
    """
    plaintext = plaintext_str.encode("utf-8")
    ciphertext = cipher.encrypt(plaintext)
    b64 = base64.b64encode(ciphertext).decode("ascii")
    # safe="" 把 / + = 都转义，与原存档一致
    return urllib.parse.quote(b64, safe="")


def save_to_xml(xml_path, cipher, entries, backup=True):
    """根据 entries 重新加密并写回 XML。

    修改逻辑（按 raw_key 精确匹配，避免误改共享同一密文的其他条目）：
      - 加密 string (encrypted=True)：重新加密 value，写入 <string> 文本
      - 明文 string (encrypted=False)：直接写入 <string> 文本（不加密）
      - int / float 等：写入 value 属性

    backup=True 时先备份原文件到 .bak（若 .bak 已存在则不覆盖）。
    返回被修改的条目数。
    """
    import shutil

    xml_path = Path(xml_path)
    if backup:
        bak = xml_path.with_suffix(xml_path.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(xml_path, bak)

    # 重新解析原 XML 以保留顺序与未修改条目
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # 建立 raw_key -> (kind, new_value) 的映射（仅修改过的条目）
    #   kind="text": 设置 elem.text（string 类型）
    #   kind="attr": 设置 value 属性（int/float 等类型）
    # 同一新明文只加密一次（加密结果确定，可复用）
    updates = {}
    encrypt_cache = {}   # plaintext -> url_encoded_base64
    modified_count = 0
    for e in entries:
        if e["value"] == e.get("original_value"):
            continue
        raw_key = e["raw_key"]

        if e["type"] == "string" and e.get("encrypted", True):
            # 加密字符串：重新加密
            new_val = e["value"]
            if new_val not in encrypt_cache:
                try:
                    encrypt_cache[new_val] = encrypt_string(cipher, new_val)
                except Exception as ex:
                    raise RuntimeError(
                        f"加密失败 (key={e.get('key') or raw_key[:30]}): {ex}"
                    )
            updates[raw_key] = ("text", encrypt_cache[new_val])
        elif e["type"] == "string":
            # 明文字符串：直接写入
            updates[raw_key] = ("text", e["value"])
        else:
            # int / float 等：更新 value 属性
            updates[raw_key] = ("attr", e["value"])
        modified_count += 1

    # 应用更新：按 name 属性精确匹配
    for elem in root:
        name = elem.get("name", "")
        if name in updates:
            kind, new_val = updates[name]
            if kind == "text":
                elem.text = new_val
            else:
                elem.set("value", new_val)

    # 写回（保持 UTF-8 + XML 声明，与原文件一致）
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    return modified_count


# ============================================================
# 主程序
# ============================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Phigros 本地存档解密器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例：
  python phigros_decrypt.py com.PigeonGames.Phigros.v2.playerprefs.xml
  python phigros_decrypt.py 存档.xml -o 明文.json
  python phigros_decrypt.py 存档.xml -v   # 显示详细解密过程
""",
    )
    parser.add_argument("xml", help="Phigros playerprefs XML 文件路径")
    parser.add_argument("-o", "--output", help="输出 JSON 文件路径（默认 输出_decrypted.json）")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示详细过程")
    parser.add_argument("--cs", default="LocalSave_reference.cs",
                        help="LocalSave.cs 源码路径（用于提取加密表）")
    args = parser.parse_args()

    xml_path = Path(args.xml)
    if not xml_path.exists():
        print(f"[错误] 文件不存在: {xml_path}", file=sys.stderr)
        sys.exit(1)

    cs_path = Path(args.cs)
    if not cs_path.exists():
        print(f"[错误] 缺少加密表源码: {cs_path}", file=sys.stderr)
        print("请确保 LocalSave_reference.cs 在同目录", file=sys.stderr)
        sys.exit(1)

    # 加载表
    print("[1/3] 加载加密表...")
    tables = load_tables(cs_path)
    for name, data in tables.items():
        print(f"  {name}: {len(data)} 字节")

    cipher = PhigrosCipher(tables)

    # 解析并解密
    print(f"\n[2/3] 解密存档: {xml_path.name}")
    entries = process_save(str(xml_path), cipher, verbose=args.verbose)

    # 统计
    total = len(entries)
    enc_str = sum(1 for e in entries if e["type"] == "string" and e.get("encrypted"))
    plain_str = sum(1 for e in entries if e["type"] == "string" and not e.get("encrypted"))
    non_string = sum(1 for e in entries if e["type"] != "string")

    print(f"  共 {total} 条")
    print(f"  加密 string 条目: {enc_str}")
    print(f"  明文 string 条目: {plain_str}")
    print(f"  非 string 条目: {non_string}")

    # 输出
    print(f"\n[3/3] 输出结果...")
    output = Path(args.output) if args.output else xml_path.with_name(
        xml_path.stem + "_decrypted.json"
    )

    # 构造可读输出
    result = {
        "source": str(xml_path.name),
        "total": total,
        "entries": [],
    }
    for e in entries:
        result["entries"].append({
            "key": e["key"],
            "value": e["value"],
            "type": e["type"],
            "encrypted": e.get("encrypted", False),
            "raw_key": e["raw_key"][:80],
            "key_error": e["key_error"],
            "value_error": e["value_error"],
        })

    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  已保存: {output}")

    # 控制台预览前若干条
    print(f"\n{'='*70}")
    print("预览（前 30 条成功解密的条目）：")
    print(f"{'='*70}")
    shown = 0
    for e in entries:
        if shown >= 30:
            break
        if e["key"] and e["value"]:
            k = e["key"]
            v = e["value"]
            if len(v) > 100:
                v = v[:100] + "..."
            print(f"\n  KEY:   {k}")
            print(f"  VALUE: {v}")
            shown += 1

    if args.verbose:
        print(f"\n{'='*70}")
        print("全部条目（含失败）：")
        print(f"{'='*70}")
        for i, e in enumerate(entries):
            k = e["key"] or f"[未解密: {e['key_error']}]"
            v = e["value"] or f"[未解密: {e['value_error']}]"
            print(f"  [{i+1}] {e['type']}")
            print(f"      key:   {k}")
            print(f"      value: {v}")


if __name__ == "__main__":
    main()
