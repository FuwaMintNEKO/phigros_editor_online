# Phigros 云存档编辑器

通过 **Session Token** 连接 Phigros 云存档服务，下载、解密并编辑账号的云端存档，再将修改后的数据重新加密上传。提供桌面图形界面和命令行工具，支持 `cn`、`intl` 两个服务器区域。

作者：**FuwaMintNEKO**。主要使用流程为：输入 Session Token → 拉取云存档 → 修改数据 → 上传覆盖云存档 → 回读验证。

## 功能

| 功能 | 支持内容 |
| --- | --- |
| 云存档读取 | 使用 Session Token 获取对应账号的玩家信息、存档及服务器摘要 |
| 曲目成绩编辑 | 新增、修改、删除 EZ / HD / IN / AT 记录，编辑分数、ACC 和 FC 状态 |
| 游戏设置编辑 | 背景亮度、音乐/音效/打击音音量、延迟偏移、音符大小、设备名称及部分开关 |
| 用户信息编辑 | 存档中的个人简介、头像 ID、背景 ID、是否显示用户 ID |
| 游戏进度编辑 | 挑战码、各档货币、章节进度、歌曲更新信息及隐藏曲解锁标志等 |
| 解锁钥匙查看 | 只读展示 `gameKey` 中的钥匙名称、标志及说明 |
| 云端写入 | 重新序列化、加密并上传存档，更新云端存档文件引用和同步时间 |
| 上传后验证 | 图形界面自动重新拉取云存档，对比编辑内容与服务器返回数据 |
| 备份与还原 | 下载原始 ZIP，保存配套 `.summary` 摘要，并从备份还原云存档 |
| 命令行工具 | 查询、导出成绩、备份、修改成绩、上传 ZIP，以及生成进度修改档 |

图形界面会在上传前保存拉取时的原始 ZIP 备份；「备份原始存档」按钮还会保存配套 `.summary` 文件。建议先手动备份，并将这两个文件一起保留。

## 安装与启动

需要 Python 3，以及可用的 Tkinter 桌面环境。云存档功能依赖 `requests` 和 `pycryptodome`：

```bash
git clone https://github.com/FuwaMintNEKO/phigros_editor_online.git
cd phigros_editor_online
python -m pip install -r requirements.txt
python phigros_cloud_gui.py
```

Windows 也可双击 `启动GUI.bat`。当前批处理固定使用 `C:\Python314\pythonw.exe`，如果 Python 安装在其他位置，请使用上面的命令启动，或自行调整批处理路径。

本项目提供 Python 桌面程序，通过网络修改云端存档；仓库目前未提供浏览器 Web 界面。

## 图形界面使用

1. 完全退出游戏，包括后台进程，避免游戏自动同步覆盖本次修改。
2. 在 **Session Token** 输入框填入自己账号的有效令牌，选择服务器 `cn` 或 `intl`。
3. 点击 **拉取存档**，查看玩家信息和当前云端数据。账号需已有云存档记录。
4. 点击 **备份原始存档**，保留生成的 ZIP 和同名 `.summary` 文件。
5. 在对应标签页编辑数据。曲目成绩需点击 **应用修改**；也可新增或删除记录。
6. 点击 **上传到服务器**，确认覆盖后，程序会上传并自动回读验证，提示是否存在差异。
7. 如需恢复，点击 **从备份还原...**，选择备份 ZIP；程序会优先读取旁边的同名 `.summary` 文件。

修改在上传后才会写入云端。恢复备份同样会覆盖当前云存档。

## 命令行使用

以下 `YOUR_SESSION_TOKEN` 为占位符，请在本机替换为自己的令牌。每个子命令都支持 `--region cn` 或 `--region intl`，默认 `cn`。

```bash
# 查看玩家信息、RKS、挑战码和成绩统计等服务器摘要
python phigros_cloud.py info YOUR_SESSION_TOKEN --region cn

# 列出云存档曲目成绩，并将成绩和摘要导出为 JSON
python phigros_cloud.py dump YOUR_SESSION_TOKEN -o cloud_dump.json

# 下载原始 ZIP 备份及配套摘要，不修改服务器
python phigros_cloud.py backup YOUR_SESSION_TOKEN

# 修改指定曲目、难度的分数和 ACC；不存在时新增记录
# 此操作自动备份，确认后上传，并回读检查该曲目成绩
python phigros_cloud.py modify YOUR_SESSION_TOKEN MARENOL.LeaF.0 IN 950000 98.5

# 用本地 ZIP 覆盖云存档，存在同名 .summary 时一并恢复摘要
python phigros_cloud.py upload YOUR_SESSION_TOKEN backup_example.zip

# 查看所有命令与参数
python phigros_cloud.py --help
```

`modify` 和 `upload` 默认询问确认，添加 `--yes` 可跳过确认。`upload` 子命令本身不会自动备份当前服务器存档，请先执行 `backup`。

### 进度修改档生成

`all-unlock` 根据当前云存档生成修改版 ZIP：设置挑战码、KiB / MiB / GiB 货币及部分隐藏曲和歌曲记录标志，保留原有曲目成绩。该命令针对现有字段实现，实际游戏中的解锁效果取决于版本和其他进度条件。

```bash
# 默认只下载、备份并生成本地 ZIP，不上传
python phigros_cloud.py all-unlock YOUR_SESSION_TOKEN -o all_unlock.zip

# 自定义挑战码和货币；添加 --yes 后立即上传并回读检查进度字段
python phigros_cloud.py all-unlock YOUR_SESSION_TOKEN --challenge 551 --money-kib 9999 --money-mib 9999 --money-gib 999 --yes
```

默认挑战码为 `551`，货币 KiB / MiB / GiB 分别为 `9999 / 9999 / 999`，TiB / PiB 设为 `0`。当前实现会沿用原始摘要，因此不保证此命令生成的挑战码与服务器摘要同步更新。

## 数据与实现限制

- Session Token 用作账号会话凭据，工具将其作为 `X-LC-Session` 请求头访问对应区域的云存档接口。请仅使用自己的令牌，并妥善保管；命令行参数可能被终端历史记录保存。
- 工具解析并重建 `settings`、`user`、`gameProgress`、`gameRecord` 和 `gameKey`。对新增字段、其他游戏版本或不同结构的存档，兼容性需另行验证。
- 图形界面上传时会同步摘要中的挑战码；RKS 和各难度统计沿用原值，工具不会根据修改后的成绩自动重算 RKS，在同步云存档后再进行任何同步到云端存档的行为后机器人查分显示正常。账号昵称等服务器资料在界面中用于查看，用户信息编辑针对存档内的字段。
- 数值校验会拦截部分类型错误和序列化位宽溢出；超出游戏常规范围的数值通常仅提示，不代表游戏能够正确处理。
- 游戏后台同步可能覆盖修改。上传成功及回读一致，只说明当时服务器返回的数据一致，后续仍可能被游戏再次同步。

## 项目文件

| 文件 | 用途 |
| --- | --- |
| `phigros_cloud_gui.py` | 云存档图形编辑器，主要入口 |
| `phigros_cloud.py` | 云存档接口、解析/序列化、加解密及命令行工具 |
| `requirements.txt` | 云存档工具的第三方依赖 |
| `启动GUI.bat` | Windows 图形界面启动脚本 |
| `phigros_editor.py` | 附带的本地 `playerprefs.xml` 编辑器 |
| `phigros_decrypt.py` | 本地存档加解密库及 JSON 导出工具 |

附带的本地存档编辑器可通过 `python phigros_editor.py` 启动；它与上述 Session Token 云存档工作流使用不同入口。

个人存档 XML、云端导出、ZIP 备份、摘要、解密数据及测试/诊断文件已由 `.gitignore` 排除，保留在本地。请勿将个人令牌或存档提交到仓库。

## 说明与致谢

本项目仅供学习与交流，不用于商业用途。修改或覆盖云存档可能造成数据损坏或账号风险，请保留备份。

- Phigros 游戏版权归 [PigeonGames](https://pigeongames.cn/) 所有。
- 加密算法参考 [PhigrosLibraryCSharp](https://github.com/yt6983138/PhigrosLibraryCSharp)（MIT 协议）。
- 如有侵权，请联系删除。
