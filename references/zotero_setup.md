# Zotero 首次配置与 API 说明

## 一、获取 Web API 凭据（一次性，约 2 分钟）

1. 浏览器登录 https://www.zotero.org
2. 打开 https://www.zotero.org/settings/keys
3. 页面顶部显示 **Your userID for use in API calls is `XXXXXXX`** —— 记下这个数字（下文称 `user_id`）
4. 点 **Create new private key**
   - Key Name：随便写，如 `workbuddy-agent`
   - 权限勾选：**Allow library access**（必须有）
   - 个人库权限勾选：**Allow write access**（必须有，否则只能读）
   - 如需写入群组库：在下方对应群组也勾上 Allow write access
5. 保存后复制生成的 key（形如 `P9NiFoyLeZu2bZNvvuQPDWm5`，**只显示一次**）

> 若 key 丢失或怀疑泄露，回到该页面 **Regenerate** 即可，旧 key 立即失效。

## 二、写入配置文件

```bash
mkdir -p ~/.zotero
printf 'api_key: <你的key>\nuser_id: <你的userID>\n' > ~/.zotero/config.yaml
```

验证：

```bash
"<python>" <skill_dir>/scripts/zotero_add.py check
```

成功返回 `{"ok": true, "user_id": ..., "collection_count": N, "collections": [...]}`。

环境变量 `ZOTERO_API_KEY` / `ZOTERO_USER_ID` 优先级高于配置文件，适合临时切换账号。

## 三、文件同步后端（决定 PDF 能不能到手机）

元数据走 Zotero 官方 API，**文件**则可能走 Zotero 官方存储或你的 WebDAV，二者互不相通：

```bash
grep -E "sync\.storage\.(protocol|url|username)|dataDir|useDataDir" \
  "$APPDATA/Zotero/Zotero/Profiles/<profile>/prefs.js"
```

- `protocol = zotero` → 官方存储（需订阅配额），本 skill 的云端同步不适用
- `protocol = webdav` → 用你自己的网盘（坚果云等），格式见 [storage_sync.md](storage_sync.md)

### WebDAV 密码从哪来

默认从 Zotero 的 `logins.json` 解密（`PK11SDR_Decrypt` + Zotero 自带 `nss3.dll`），
**只认 `realm = Zotero Storage Server`** 那条。库里往往还有 `Zotero Web API`（API Key）
和 `...(encrypted)` 变体，取错就会 401。

不想依赖解密时，显式写进配置（明文，注意文件权限）：

```yaml
# ~/.zotero/config.yaml
api_key: <你的key>
user_id: <你的userID>
webdav_protocol: webdav
webdav_url: dav.jianguoyun.com/dav
webdav_user: you@example.com
webdav_password: <坚果云「应用密码」，不是登录密码>
```

坚果云应用密码在：网页版 → 账户信息 → 安全选项 → **添加应用**（一个应用一个密码，可单独吊销）。

验证：

```bash
"<python>" <skill_dir>/scripts/zotero_sync.py doctor
```

关注 `webdav.credential_ok` 与 `webdav.reachable` 两项，都为 true 才能继续。

## 四、关键 API 端点

| 用途 | 方法与路径 |
|---|---|
| 列分类 | `GET /users/<uid>/collections` |
| 分类内条目（去重查询） | `GET /users/<uid>/collections/<key>/items?q=<title>` |
| 全库查重 | `GET /users/<uid>/items/top?q=<arxiv 或标题>` |
| 建条目 | `POST /users/<uid>/items`（body 为数组，返回 `successful["0"].key`） |
| 建附件条目 | `POST /users/<uid>/items`，`itemType=attachment, linkMode=imported_file, parentItem=<key>` |
| 改条目（补 md5/mtime、改标题、加分类） | `PATCH /users/<uid>/items/<key>`，**必须带 `If-Unmodified-Since-Version: <version>`**，否则 428 |
| 取条目 version | `GET /users/<uid>/items/<key>` → `version` |

统一请求头：`Zotero-API-Key: <key>`、`Zotero-API-Version: 3`。

> ⚠️ **不要用 `POST /items/<attKey>/file` 那套四步上传给 WebDAV 用户传 PDF**。
> 它只把文件写进 Zotero 官方存储；WebDAV 用户拿到的是一个"元数据说已同步、云端却没有"的坏状态，
> 手机端会一直报 `not available on the WebDAV server`。正确做法见 [storage_sync.md](storage_sync.md)。

## 五、常见错误

| 现象 | 原因 / 处理 |
|---|---|
| HTTP 403 `Write access denied` | key 没勾 **Allow write access**，重新生成 |
| HTTP 404 on collections | `user_id` 填错（注意是 userID 数字，不是用户名） |
| HTTP 428 `object version property` | 按 key 写条目缺 `If-Unmodified-Since-Version` |
| HTTP 401 on WebDAV | 取到了 API Key 而非 Storage 密码；或坚果云应用密码被重置/删除 |
| HTTP 403 / 507 on WebDAV | 权限问题或**当月上传流量超额** |
| 手机报 `not available on the WebDAV server` | 文件没真正上 WebDAV，跑 `verify` 确认后 `add --pdf-repair` 重传 |
| 群组库写入失败 | 需要 group id 而非 user id，端点变为 `/groups/<gid>/items`；本技能默认只支持个人库 |

## 六、PDF 获取的可信度分级

1. **arXiv PDF**（`https://arxiv.org/pdf/<id>`）——最可靠，无墙、无需登录
2. **Semantic Scholar `openAccessPdf`** —— 开放获取，可能指向出版社 PDF
3. **GitHub README 中的 arXiv 链接** —— 项目主页常挂论文，命中后回到第 1 类
4. 其它来源（出版社、ResearchGate）—— 常有 paywall 或需登录，**不要尝试绕过**；拿不到就只入库元数据并告知用户

下载后必须校验：文件头 `%PDF`、尾部 8KB 内出现 `%%EOF`、尺寸大于 20KB。
任一不满足就换镜像重下——本机直连 arXiv 容易被截断，拿到半个 PDF 比拿不到更糟。
`zotero_ingest.py` 已内置这项校验与三次重试，无需手工处理。
