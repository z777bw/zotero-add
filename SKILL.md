---
name: zotero-add
description: 一键把论文收进 Zotero 指定分类并同步到云端：输入 arXiv 编号/链接、论文标题、项目短名（如 pi0、RISE）、GitHub 主页或 PDF 直链，自动解析元数据、多镜像下载 PDF 并做完整性校验、写入 Zotero 分类、落到本地 storage 目录，再按 Zotero 的 WebDAV 存储格式上传到坚果云，最后回读校验 md5。当用户说"把这篇论文加到 Zotero"、"存到我的某个分类/文件夹"、"下载这个论文到文献库"、"加完顺便同步到手机/坚果云"时使用。
agent_created: true
metadata:
  requires: '{"bins": ["python"], "env": ["ZOTERO_API_KEY", "ZOTERO_USER_ID"]}'
---

# zotero-add：论文 → PDF → Zotero 分类 → 云端同步

一条命令走完四步，任何一步失败都不会留下"元数据有、文件没有"的半成品而不告知：

```
解析元数据 → 建条目/附件 → 下载 PDF(校验) → 落本地 storage → 传 WebDAV → 回读校验
```

## 命令速查

```bash
PY="python3"           # Python 3.9+；Windows 下用 python
SK="<repo_dir>"        # 本仓库根目录，scripts/ 在其下
```

| 场景 | 命令 |
|---|---|
| 环境自检 | `$PY $SK/scripts/zotero_sync.py doctor` |
| 只看候选，不写入 | `$PY $SK/scripts/zotero_ingest.py search "<query>"` |
| 正式入库 | `$PY $SK/scripts/zotero_ingest.py add "<query>" --collection "<分类>"` |
| 已有条目补下/补传 PDF | 在上面加 `--pdf-repair` |
| 校验云端能否下载 | `$PY $SK/scripts/zotero_ingest.py verify <item_key>` |

## 步骤 0：环境自检（首次或换机器必做）

```bash
$PY $SK/scripts/zotero_sync.py doctor
```

必须全部满足，否则先解决再往下走：

| 字段 | 期望 | 不满足怎么办 |
|---|---|---|
| `zotero_home` | 指向装有 `zotero.exe` 的目录 | 在 `~/.zotero/config.yaml` 写 `zotero_home: <路径>` |
| `data_dir` / `storage_dir` | 指向 Zotero **真实**数据目录 | 若 `storage_entries` 与你实际附件数差很多，说明解析到了旧快照，检查 `prefs.js` |
| `webdav.protocol` | `webdav` | 若为 `zotero`（官方存储），本 skill 的云端同步不适用，只能本地入库 |
| `webdav.credential_ok` | `true` | 见 [references/zotero_setup.md](references/zotero_setup.md) 第三节 |
| `webdav.reachable` | `true` | 检查坚果云应用密码 / 网络 / 配额 |

## 步骤 1：解析论文

```bash
$PY $SK/scripts/zotero_ingest.py search "pi0"
$PY $SK/scripts/zotero_ingest.py search "2410.24164"
$PY $SK/scripts/zotero_ingest.py search "https://github.com/xxx/yyy"
```

解析链（`--mode auto` 默认按此顺序，可用 `--mode` 强制指定）：

1. 查询串里含 arXiv 编号/链接 → 直接按编号取元数据
2. GitHub 仓库地址 → 读 README 里的 arXiv 链接
3. 其它（标题、项目名）→ **网页搜索定位 arXiv 编号**（Bing，对 `pi0` 这类短名最有效）
   → arXiv 标题短语搜索 → Semantic Scholar（常限流，失败不致命），合并去重后打分

打分要点：标题归一化后**以查询串开头**的给高分（`$π_0$: ...` 归一化为 `pi0: ...`，即项目名式标题）；
仅在标题中部提到该词的（如粒子物理的 `pi0 pi0 散射`）只给中低分。

**候选确认规则（硬性）**：

- 返回 `need_confirm: true`（第一名与第二名分差 < 0.15）→ **必须**把候选表列给用户，等其选定后再 `--pick N`；禁止自行拍板
- 返回 `need_confirm: false`（唯一且明显）→ 可直接入库，但在汇报里写明选了哪篇
- `ok: false` → 把 `error`/`notes` 原样告知，建议改用 arXiv 编号或 PDF 直链；**不要**扩大搜索范围乱猜

短名实在搜不到时，用 `--hint` 补领域词（`search "pi0" --hint "robot manipulation"`），或直接改用 arXiv 编号。

## 步骤 2：入库 + 同步

```bash
$PY $SK/scripts/zotero_ingest.py add "2410.24164" --collection "VLA"
$PY $SK/scripts/zotero_ingest.py add "pi0" --collection "VLA" --pick 1
```

参数：

| 参数 | 说明 |
|---|---|
| `--collection` | 分类名，先精确后子串匹配；找不到会返回 `available_collections` |
| `--pick N` | 多候选时选第 N 篇（配合步骤 1 的候选表） |
| `--yes` | 取第一名，不再确认（仅在你已判断无歧义时用） |
| `--type` | `preprint`(默认) / `journalArticle` / `conferencePaper` / `report` |
| `--tags` | 逗号分隔 |
| `--pdf-repair` | 库中已有该条目时，重新下载并重传 PDF、规范化标题（不重复建条目） |
| `--align` | 额外对齐本地库，省一次云端下载；会短暂重启 Zotero，默认关闭 |
| `--hint` | 补充检索词，帮助消歧 |

执行阶段（返回 JSON 的 `stages` 里逐条可见）：
`resolve → pick → collection → dedupe → create-item → create-attachment → download → local-storage → webdav-upload → webdav-verify → metadata-md5 → align-local`

判定成功的标准是 **`ok: true` 且 `cloud.ok: true`**。只有 `ok` 为真但 `cloud.ok` 为假时，说明文件只在本地，**手机端一定打不开**，必须明确告知用户。

行为细节：

- 附件条目创建后，PDF 写入 `<storage_dir>/<附件key>/<清洗后的标题>.pdf`（`$π_0$` 之类 LaTeX 包装会被清洗成 `π0`）
- 云端写两件东西：`<附件key>.zip`（单条目 deflate，条目名即上面那个文件名）与 `<附件key>.prop`（`mtime` + 内层 PDF 的 `md5`）
- 随后用 `PATCH` 把 `md5`/`mtime` 写回附件条目元数据（必须带 `If-Unmodified-Since-Version`，否则 428）
- 去重按 arXiv 编号 + 标题归一化在全库范围查，命中则只补分类归属，不重复建条目

## 步骤 3：校验（推荐每次都做）

```bash
$PY $SK/scripts/zotero_ingest.py verify <item_key>
```

从 WebDAV 把 zip 拉回来，解开后比对内层 PDF 的 md5 与 `.prop` 记录：
`cloud_ok: true` 才代表手机端能下载。`local` 为 `null` 表示桌面端还没把条目同步下来（正常，客户端会自行拉取）。

## 步骤 4：汇报

一段简洁结果，包含：

- 标题 + 第一作者 + 年份 + arXiv 编号
- 归入的分类名
- PDF：字节数、是否已落本地 storage（给绝对路径）
- 云端：`webdav-verify` 是否通过（`md5` 前后一致）
- Zotero item key / attachment key
- 若 `skipped`（已存在）或降级（仅元数据），说明原因与补救命令

## 批量场景

多篇时逐篇串行 `add`（不要并发写 API，有频率限制），最终汇总成一张表：

```
| # | 论文 | 分类 | PDF | 云端 | 备注 |
```

任一失败不中断其余条目，最后单独列出失败项与原始错误。

## 硬性约束（违反会直接导致"手机打不开"）

1. **绝不用 Zotero Web API 上传文件本体**。那套四步上传只对「Zotero 官方存储」有效；
   用户用的是 WebDAV（坚果云）时文件会进错地方，而且写回 md5 后客户端会误判「已同步」，
   从此永远不上传 → 手机端报 `not available on the WebDAV server`。
   本 skill 的正确做法是：本地落盘 + 直接 PUT 到 WebDAV + 回读校验。
2. **压缩包格式必须与客户端一致**：`<key>.zip` 单条目 deflate，条目名 = 目标文件名；
   `.prop` 为 `<properties version="1"><mtime>毫秒</mtime><hash>内层PDF的md5</hash></properties>`。
   格式错会导致客户端校验失败、反复重下。
3. **下载必须验完整性**：`%PDF` 头 + `%%EOF` 尾 + 尺寸下限，任一不满足就换镜像重下。
   本机直连 arXiv 常被截断，拿到半个 PDF 比拿不到更糟。
4. **不绕过 paywall**：没有开放 PDF 就只入库元数据，并告知用户手动拖入。
5. **不改 Zotero 数据库**，除非走 `--align`（该路径会先备份 `zotero.sqlite`，且必须在客户端关闭时执行）。
6. 只支持个人库；群组库端点不同（`/groups/<gid>/items`）。

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `cloud.ok: false`，`HTTP 401` | 凭据取错（可能取到 `Zotero Web API` 的 API Key）或应用密码已重置，见 setup 文档第三节 |
| `HTTP 403 / 507` | 坚果云权限或**当月上传流量超额**，换月或升级 |
| `HTTP 428 ... object version property` | 写回条目没带 `If-Unmodified-Since-Version`，用 `patch_item()` |
| 手机报 `not available on the WebDAV server` | 文件根本没上 WebDAV，跑 `verify` 确认后重传（`add --pdf-repair`） |
| 客户端反复下载同一个附件 | `.prop` 里的 `hash` 与 zip 内层 PDF 的 md5 不一致，重传 |
| `未找到分类` | 分类名变了或层级不同，用返回的 `available_collections` 纠正 |
| 条目建成功但标题带着 `$...$` | 是 arXiv 原始标题，`clean_title()` 会清洗；已有条目用 `--pdf-repair` 顺带修正 |

## 相关文件

- `scripts/zotero_ingest.py` —— 主流水线（`search` / `add` / `verify` / `doctor`）
- `scripts/zotero_sync.py` —— 本地 storage + WebDAV 存储层（`doctor` / `upload` / `verify` / `align` / `close` / `start`）
- `scripts/find_paper.py` —— 元数据解析（arXiv API / abs 页 / Semantic Scholar / GitHub README）
- `scripts/zotero_add.py` —— 早期只用元数据的写入工具，**附件上传路径已废弃**，勿用其 `--pdf`
- `references/zotero_setup.md` —— 首次配置
- `references/storage_sync.md` —— 存储与同步格式的白盒说明
