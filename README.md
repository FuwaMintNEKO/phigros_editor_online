# Phigros 存档编辑器 by FuwaMintNEKO

一个用于查看和编辑 Phigros 本地存档（`playerprefs.xml`）的图形化工具。

## 功能

- 自动解密/加密 Phigros 的自定义 AES-CBC 存档格式
- 图形界面浏览所有存档条目（Key / Value / 类型 / 状态）
- 实时搜索过滤（支持按 Key、Value 或全部搜索）
- 编辑加密字符串、明文字符串（如 `unity.*`）和 int 类型条目
- JSON 格式化/压缩快捷按钮
- 保存时自动备份原文件（`.bak`），仅重新加密被修改的条目
- 命令行工具支持批量解密导出为 JSON

## 文件说明

| 文件 | 说明 |
|------|------|
| `phigros_editor.py` | 图形化编辑器（主程序） |
| `phigros_decrypt.py` | 解密/加密库 + 命令行工具 |
| `phigros_cloud.py` | 云存档命令行工具 |
| `phigros_cloud_gui.py` | 云存档图形编辑器 |
| `启动GUI.bat` | Windows 云存档界面启动入口 |

## 用法

### 图形界面

```bash
python phigros_editor.py
```

点击「打开存档」选择 `com.PigeonGames.Phigros.v2.playerprefs.xml` 文件，编辑 Value 后点击「应用修改到列表」，最后点「保存」写回。

### 命令行

```bash
# 解密存档并导出为 JSON
python phigros_decrypt.py <存档.xml> -o <输出.json>

# 显示详细过程
python phigros_decrypt.py <存档.xml> -v
```

## 依赖

- Python 3.8+
- 仅使用标准库（tkinter、xml、base64 等），无需安装第三方包

## 云存档工具（实验性）

`phigros_cloud.py` 和 `phigros_cloud_gui.py` 用于验证「能否远程读写 Phigros 云存档」这一安全问题。

```bash
# 图形界面（拉取 -> 编辑全部数据 -> 上传 -> 自动回读验证）
python phigros_cloud_gui.py

# 命令行
python phigros_cloud.py info    <session_token>            # 验证 token，显示摘要
python phigros_cloud.py dump    <session_token>            # 下载解密，列出全部曲目分数
python phigros_cloud.py backup  <session_token>            # 只读备份原始存档 zip
python phigros_cloud.py modify  <token> <曲目ID> <难度> <分数> <ACC>   # 改分数并上传
python phigros_cloud.py upload  <token> <存档.zip>         # 上传本地 zip
```

**重要提示**：测试前请让游戏本体完全退出（含后台进程），否则游戏会自动把本地存档上传覆盖你的修改——服务器无完整性校验、也不区分写入方，后写覆盖先写。

依赖：`python -m pip install -r requirements.txt`

个人存档 XML、云存档导出、备份 ZIP、摘要、解密数据和测试/诊断文件均由 `.gitignore` 排除，保留在本地。Session Token 需要运行时输入，请勿提交到仓库。

## 免责声明

本项目仅供学习与交流使用，不用于任何商业用途。

- Phigros 游戏的版权归 [PigeonGames](https://pigeongames.cn/) 所有
- 加密算法参考自 [PhigrosLibraryCSharp](https://github.com/yt6983138/PhigrosLibraryCSharp)（MIT 协议）
- 使用本工具修改存档可能导致数据损坏或账号风险，后果自负
- 如有侵权，请联系删除
