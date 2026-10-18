# zotero-add

把一篇论文从"我知道它叫什么"一键变成"Zotero 里有条目 + 有 PDF + 手机上能下载"。

```
arXiv 编号 / 论文标题 / 项目短名(pi0) / GitHub 主页 / PDF 直链
        ↓
解析元数据 → 建条目 → 下载 PDF(校验) → 落本地 storage → 传 WebDAV → 回读校验
```

---

## 为什么需要这个工具

Zotero 的同步拆成**两条互不相干**的通道：元数据（走 Zotero 官方 API）和文件（走官方存储**或**你自己的 WebDAV）。

绝大多数"自动化入库"脚本只做前一半，然后用 Web API 上传 PDF。对 WebDAV 用户来说这是个陷阱：

1. Web API 只能把文件写进 **Zotero 官方存储**；
2. 上传后脚本还会把 `md5` 写回条目元数据；
3. 客户端一看"元数据有 md5、本地文件也对得上"，判定**已同步**；
4. 于是它**永远不会**把文件传到 WebDAV。

结果是电脑上双击能开，手机上一直报：

```
The attached file is not available on the WebDAV server.
```

`zotero-add` 绕开了这条路：**本地落盘 + 直接 PUT 到 WebDAV + 回读 md5 校验**，格式严格对齐 Zotero 客户端自己写的那一套（`<key>.zip` + `<key>.prop`）。

---

## 特性

| | |
|---|---|
| **短名可解析** | `pi0` 这种 arXiv 自己都搜不到的短名，走 Bing 定位编号（`2410.24164`） |
| **多镜像下载** | arXiv → Semantic Scholar openAccessPdf → GitHub README 内链接 |
| **完整性校验** | `%PDF` 头 + 尾部 `%%EOF` + 尺寸下限，不满足自动重下（直连 arXiv 常被截断） |
| **真·云端同步** | 按客户端格式直传 WebDAV，回读比对 md5，`verify` 通过才算成功 |
| **幂等** | 全库查重（arXiv 编号 + 标题归一化），已存在则只补分类归属，不重复建条目 |
| **标题清洗** | `$π_0$: A Vision-Language-Action...` → `π0: A Vision-Language-Action...` |
| **零依赖** | 纯 Python 标准库，不需要 `pyzotero` |

---

## 环境要求

- Python 3.9+
- 本机安装 Zotero 桌面端（用于自动发现数据目录、解密 WebDAV 凭据）
- Zotero Web API key（免费）
- 文件同步后端为 **WebDAV**（坚果云 / Nextcloud / Synology 等）

> 用 Zotero 官方存储的话，云端同步这一步会自动跳过，只做本地入库。

---

## 安装

```bash
git clone https://github.com/<your-name>/zotero-add.git
cd zotero-add
```

没有 `pip install`，因为不用第三方库。

### 1. 配置 Zotero API

打开 https://www.zotero.org/settings/keys ：

- 页面顶部记下 **userID**（纯数字）
- Create new private key → 勾选 **Allow library access** + **Allow write access**

```bash
mkdir -p ~/.zotero
cp config.example.yaml ~/.zotero/config.yaml
# 填入 api_key 与 user_id
```

也可以直接用环境变量，优先级更高：

```bash
export ZOTERO_API_KEY=xxxxxxxx
export ZOTERO_USER_ID=1234567
```

### 2. 环境自检

```bash
python scripts/zotero_sync.py doctor
```

跑完检查这几项（详见 [references/zotero_setup.md](references/zotero_setup.md)）：

| 字段 | 期望 |
|---|---|
| `zotero_home` / `data_dir` / `storage_dir` | 指向**真实**数据目录，`storage_entries` 与你实际附件数量对得上 |
| `webdav.protocol` | `webdav` |
| `webdav.credential_ok` | `true` |
| `webdav.reachable` | `true` |

> `dataDir` 指向的才是真库。默认的 `C:\Users\<u>\Zotero` 在某些机器上只是历史快照，往那儿写文件客户端看不见。

---

## 快速开始

```bash
# 只查候选，不写入
python scripts/zotero_ingest.py search "pi0"

# 正式入库 + 同步
python scripts/zotero_ingest.py add "pi0" --collection "VLA" --pick 1

# 校验云端能不能下载（手机端能不能开）
python scripts/zotero_ingest.py verify <item_key>
```

`search` 会返回打分后的候选列表。当第一名与第二名分差小于 0.15 时会标记 `need_confirm: true`，此时应该让人选，而不是自己拍板。

### 参数

| 参数 | 说明 |
|---|---|
| `--collection` | 分类名，先精确后子串匹配；找不到会返回 `available_collections` |
| `--pick N` | 多候选时选第 N 篇 |
| `--yes` | 取第一名不再确认 |
| `--type` | `preprint`(默认) / `journalArticle` / `conferencePaper` / `report` |
| `--tags` | 逗号分隔 |
| `--pdf-repair` | 条目已存在时重新下载并重传 PDF、规范化标题 |
| `--align` | 对齐本地库，省一次云端下载（会短暂重启 Zotero） |
| `--hint` | 补充检索词帮助消歧 |

### 返回结果怎么看

`stages` 里逐条列出执行情况：

```
resolve → pick → collection → dedupe → create-item → create-attachment
        → download → local-storage → webdav-upload → webdav-verify
        → metadata-md5 → align-local
```

**成功的标准是 `ok: true` 且 `cloud.ok: true`。** 只有 `ok` 为真、`cloud.ok` 为假，说明文件只在本地，手机端一定打不开。

---

## 命令总览

| 命令 | 作用 |
|---|---|
| `python scripts/zotero_sync.py doctor` | 路径 / 凭据 / 连通性自检 |
| `python scripts/zotero_sync.py upload --key <att> --pdf <path>` | 单独上传一个文件到 WebDAV |
| `python scripts/zotero_sync.py verify --key <att>` | 回读校验 |
| `python scripts/zotero_sync.py align --key <att> --pdf <path>` | 本地库对齐（需先关 Zotero） |
| `python scripts/zotero_sync.py close` / `start` | 关闭 / 启动 Zotero |
| `python scripts/zotero_ingest.py search/add/verify/doctor` | 主流水线 |

---

## 实现要点

想自己排查问题的话，这两份文档把黑盒打开了：

- [references/storage_sync.md](references/storage_sync.md) — 本地布局、WebDAV 上的 zip/prop 格式、客户端判定逻辑、`syncState` 取值
- [references/zotero_setup.md](references/zotero_setup.md) — 凭据获取、API 端点、错误码对照表

几个容易踩的点：

**WebDAV 路径没有 userID 层级**

```
<webdav_url>/zotero/<附件key>.zip
<webdav_url>/zotero/<附件key>.prop
```

**`.prop` 里记的是 zip 内层 PDF 的 md5，不是 zip 自身的**

```xml
<properties version="1"><mtime>1792302720724</mtime><hash>9f2c8e1a...</hash></properties>
```

zip 内层 md5 / prop 的 `hash` / 条目元数据 `md5` 三者必须完全相等，否则客户端判定文件损坏并反复重下。

**`PATCH` 条目必须带版本号**

```
PATCH /users/<uid>/items/<key>
If-Unmodified-Since-Version: <version>
```

漏了就是 `HTTP 428 Precondition Required`。

**`logins.json` 里不止一条凭据**

同时存在 `Zotero Web API`（API Key）和 `Zotero Storage Server`（WebDAV 密码）。只认后者，取错就是 `401`。

---

## 作为 AI Agent Skill 使用

仓库根目录的 [SKILL.md](SKILL.md) 是给 AI 编码代理看的指令文件（Claude Code / WorkBuddy / Cursor 等格式兼容）。把它放进对应的 skills 目录，代理就能在你说"把这篇文章加到 Zotero 的 VLA 分类"时自动跑完整条流水线。

```bash
# 例：WorkBuddy
cp -r zotero-add ~/.workbuddy/skills/
```

---

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 手机报 `not available on the WebDAV server` | 文件没真上 WebDAV。`verify` 确认后 `add --pdf-repair` 重传 |
| `cloud.ok: false` + `HTTP 401` | 凭据取错（拿到 API Key）或应用密码已重置 |
| `HTTP 403 / 507` | WebDAV 权限问题，或**当月上传流量超额**（坚果云免费版有上限） |
| `HTTP 428` | 写回条目没带 `If-Unmodified-Since-Version` |
| 客户端反复下载同一附件 | `.prop` 的 `hash` 与 zip 内层 md5 不一致，重传 |
| 条目建成功但标题带 `$...$` | arXiv 原始标题，`clean_title()` 会清洗；老条目用 `--pdf-repair` 修正 |
| `未找到分类` | 分类名变了，用返回的 `available_collections` 纠正 |

---

## 安全

- WebDAV 密码从 `logins.json` 解密（`PK11SDR_Decrypt` + Zotero 自带 `nss3.dll`），**进程内使用，不落盘、不打印**。
- 想避免依赖解密，才在 `~/.zotero/config.yaml` 写 `webdav_password`（明文，注意 `chmod 600`）。
- API key 只需"库读写"，不需要别的权限；怀疑泄露随时 Regenerate。

## 限制

- 只支持**个人库**；群组库端点不同（`/groups/<gid>/items`）。
- 云端同步仅对 **WebDAV** 后端有效；官方存储用户只能本地入库。
- 不绕过 paywall：拿不到开放 PDF 时只入库元数据，并明确告知。
- 批量入库请**串行**执行，并发写 API 会触发限流。

## License

[MIT](LICENSE)
