# Zotero 存储与同步格式（白盒说明）

这份文档解释「为什么文件必须同时出现在本地 storage 和 WebDAV，且格式必须完全一致」。
读懂它就能自己排查「电脑能开、手机打不开」这类问题。

## 一、两条独立的同步通道

Zotero 的同步拆成两个互不相干的通道，**任何一条断了都会出现"有条目没文件"**：

| 通道 | 内容 | 服务端 | 本文关注点 |
|---|---|---|---|
| 元数据同步 | 条目字段、分类归属、附件条目本身 | `api.zotero.org`（Zotero 官方，必须） | — |
| 文件同步 | PDF 二进制 | 二选一：Zotero 官方存储 / **WebDAV** | ✅ 本节 |

配置在 `prefs.js`：

```
extensions.zotero.sync.storage.protocol = "webdav"   # 或 "zotero"
extensions.zotero.sync.storage.url      = "dav.jianguoyun.com/dav"
extensions.zotero.sync.storage.username = "<账号>"
extensions.zotero.dataDir               = "D:\\Zotero\\data"
extensions.zotero.useDataDir            = true
```

> 注意：`dataDir` 指向的才是真实库。默认路径 `C:\Users\<u>\Zotero` 可能只是历史快照，
> 往那里写文件客户端看不见。`doctor` 会报 `data_dir` 与 `storage_entries`，先核对再动手。

## 二、本地存储布局

```
<dataDir>/storage/<附件key>/<文件名>.pdf
```

- 目录名是附件条目的 8 位 key，一个目录内通常只有一个文件
- 数据库里对应 `itemAttachments.path = storage:<文件名>`（是相对 key 目录的路径，不是绝对路径）
- `itemAttachments.syncState`：`0`=待上传 `1`=待下载 `2`=已同步 `3`=强制上传 `4`=强制下载 `5`=冲突

## 三、WebDAV 上的存储布局

```
<webdav_url>/zotero/<附件key>.zip
<webdav_url>/zotero/<附件key>.prop
```

- **不包含 userID 层级**（这是最容易写错的地方）
- `<key>.zip`：标准 zip，**单条目**，deflate 压缩，条目名就是目标文件名（含中文/希腊字母时用 UTF-8 标志）
- `<key>.prop`：纯文本 XML，记录"这个文件是什么版本"

```xml
<properties version="1"><mtime>1792302720724</mtime><hash>9f2c8e1a4b6d0f3e5c7a9b1d2e4f6a8c</hash></properties>
```

| 字段 | 含义 |
|---|---|
| `mtime` | 毫秒时间戳，与附件条目元数据的 `mtime` 一致 |
| `hash` | **zip 内层 PDF**（不是 zip 本身）的 md5，必须与条目元数据 `md5` 一致 |

三者（zip 内层 md5 / prop 的 hash / 条目元数据 md5）必须完全相等，否则客户端会判定文件损坏并反复重下。

## 四、客户端的行为逻辑

1. 元数据同步后，附件条目带 `md5` + `mtime`
2. 本地 `storageHash` 为空或与 `md5` 不等 → `syncState=1`（待下载）→ 去 WebDAV GET `<key>.prop`，比对 `hash`
3. 下载 zip、解压、校验 md5、写入 `storage/<key>/<文件名>`、置 `syncState=2`
4. 若本地文件比 `storageHash` 新 → `syncState=0`（待上传）→ 打包 zip 上传，成功后写回 prop

**关键陷阱**：如果用 Web API 上传文件并写回 `md5`，客户端会认为「文件已在云端」而跳过上传。
但 Web API 只能写进 Zotero 官方存储，对 WebDAV 用户来说文件根本不在那儿 → 手机端永远 404。
正确做法就是本 skill 做的事：**本地落盘 + 直接 PUT WebDAV + 回读校验**。

## 五、附件的三种状态自查

```bash
PY="<python>"; SK="<skill_dir>"
$PY $SK/scripts/zotero_sync.py doctor                       # 路径与连通性
$PY $SK/scripts/zotero_ingest.py verify <item_key>          # 云端可下载性（回读 md5）
$PY $SK/scripts/zotero_sync.py align --key <att_key> --pdf <本地pdf>   # 本地库对齐（需关闭 Zotero）
```

`verify` 通过 = 手机能下载；`align` 通过 = 桌面端双击即开（不用等云端下载）。

## 六、安全说明

- WebDAV 密码从 Zotero 的 `logins.json` 解密（`PK11SDR_Decrypt`，用 Zotero 自带的 `nss3.dll`），
  进程内使用，**不落盘、不打印**。
- 只认 `realm = Zotero Storage Server`；库里同时存在 `Zotero Web API`（API Key）和
  `...(encrypted)` 变体，取错就会 401。
- 想避免依赖解密，可在 `~/.zotero/config.yaml` 显式写 `webdav_password`。
- `align` 会先备份 `zotero.sqlite`(+wal/shm) 到 `backup-<时间戳>` 目录，再改 4 个字段。
