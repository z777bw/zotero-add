#!/usr/bin/env python3
"""zotero_ingest.py —— 「指出论文 → 下 PDF → 进 Zotero 分类 → 同步到 WebDAV」一条龙。

子命令：
  search   只解析论文（看候选，不写入）
  add      完整流水线：解析 → 去重 → 建条目 → 下 PDF → 落本地 storage → 传坚果云 → 校验
  verify   校验某条已有附件在云端是否可下载（回读 zip 比对 md5）
  doctor   环境自检（透传 zotero_sync.py doctor）

解析优先级：arXiv 编号 > GitHub 仓库 > PDF 直链 > 标题
  标题模式下依次尝试：网页搜索（Bing → arXiv 链接）> arXiv 标题短语 > Semantic Scholar，
  合并去重后打分排序；有歧义时必须由调用方 --pick 指定，禁止脚本自行拍板。

所有输出都是单个 JSON 对象，ok 字段表示成败；候选待确认时 exit code = 3。
仅用标准库。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import find_paper as FP  # noqa: E402
import zotero_sync as S  # noqa: E402

UA_WEB = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
          "Accept-Language": "en-US,en;q=0.9"}
GREEK = {"π": "pi", "ρ": "rho", "τ": "tau", "α": "alpha", "β": "beta", "γ": "gamma",
         "δ": "delta", "θ": "theta", "λ": "lambda", "μ": "mu", "σ": "sigma", "φ": "phi"}


# --------------------------------------------------------------------------- #
# 文本归一化 & 打分
# --------------------------------------------------------------------------- #
def norm(s):
    s = (s or "").lower()
    for k, v in GREEK.items():
        s = s.replace(k, v)
    s = re.sub(r"\$|\\[a-z]+|[{}\^_]", "", s)
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", s)
    return " ".join(s.split())


def tight(s):
    return norm(s).replace(" ", "")


def score(query, meta, source_rank):
    """source_rank: 0 = 搜索第一名。返回 0~1。

    关键区分度来自「名字出现在标题开头」——论文以项目名命名时（如 "$π_0$: ..."）
    标题归一化后以查询串开头；而仅在正文/标题中部提到该词的（如粒子物理论文里的
    pi0 pi0 散射）不应拿到同样高的分。
    """
    q, t = norm(query), norm(meta.get("title"))
    qt, tt = tight(query), tight(meta.get("title"))
    s = 0.0
    if qt and tt:
        if qt == tt or tt.startswith(qt + ":") or tt.startswith(qt):
            s = 0.95
        elif qt in t.split():
            s = 0.80
        elif qt in tt:
            s = 0.65
        else:
            qa, ta = set(q.split()), set(t.split())
            if qa:
                s = 0.30 + 0.45 * len(qa & ta) / len(qa)
    if source_rank is not None:  # 网页搜索名次加成：名次越靠前越可信
        s = max(s, 0.97 - 0.06 * source_rank)
    if meta.get("arxiv_id") and re.match(r"^\d{4}\.\d{4,5}", meta["arxiv_id"]):
        s += 0.01
    return round(min(s, 1.0), 3)


# --------------------------------------------------------------------------- #
# 解析：多源合并
# --------------------------------------------------------------------------- #
def web_arxiv_ids(query, limit=5):
    """Bing 搜索 '<query> arxiv'，抽取命中的 arXiv 编号（按出现顺序去重）。"""
    ids = []
    for engine in ("https://www.bing.com/search?q=",
                   "https://cn.bing.com/search?q=",
                   "https://search.marcia.cc/search?q="):
        try:
            url = engine + urllib.parse.quote(query + " arxiv")
            raw = urllib.request.urlopen(
                urllib.request.Request(url, headers=UA_WEB), timeout=25).read()
            txt = raw.decode("utf-8", "replace")
            for i in re.findall(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", txt):
                if i not in ids:
                    ids.append(i)
            if ids:
                break
        except Exception:
            continue
    return ids[:limit]


def resolve(query, mode="auto", hint="", limit=5, log=None):
    """返回 (candidates, notes)。candidates 已按分数降序。"""
    notes, cands, seen = [], [], {}

    def add(meta, source_rank):
        if not meta or not meta.get("title"):
            return
        key = meta.get("arxiv_id") or tight(meta["title"])
        if key in seen:
            old = seen[key]
            if source_rank is not None and (old.get("_rank") is None or source_rank < old["_rank"]):
                old["_rank"] = source_rank
                old["score"] = max(old["score"], score(query, meta, source_rank))
            return
        m = dict(meta)
        m["_rank"] = source_rank
        m["score"] = score(query, meta, source_rank)
        seen[key] = m
        cands.append(m)

    # 1) 显式 arXiv 编号
    m = FP.ARXIV_URL_RE.search(query) or FP.ARXIV_TEXT_RE.search(query)
    aid = m.group(1) if m else (query.strip() if FP.ARXIV_ID_RE.match(query.strip()) else None)
    if aid:
        meta, err = FP.arxiv_lookup(aid)
        if meta:
            add(meta, 0)
            return sorted(cands, key=lambda c: -c["score"]), ["direct-arxiv:" + aid]
        notes.append("arXiv %s 查询失败：%s" % (aid, err))
        return [], notes

    # 2) 网页搜索定位编号（对 pi0 这类短名最有效）
    wq = (query + " " + hint).strip()
    ids = web_arxiv_ids(wq)
    if ids:
        notes.append("web-search-hits:" + ",".join(ids))
    for rank, i in enumerate(ids):
        try:
            meta, _ = FP.arxiv_lookup(i)
        except Exception:
            meta = None
        if meta:
            add(meta, rank)

    # 3) arXiv 标题 / 全文短语
    try:
        for hit in FP.arxiv_search(wq) or []:
            add(hit, None)
    except Exception as e:
        notes.append("arxiv-search: %s" % e)

    # 4) Semantic Scholar（常限流，失败不致命）
    try:
        s2, err = FP.s2_search_ex(wq)
        for hit in s2 or []:
            add(hit, None)
        if err:
            notes.append(err)
    except Exception as e:
        notes.append("s2: %s" % e)

    if not cands:
        notes.append("未匹配到论文，请改用 arXiv 编号 / PDF 直链 / 更完整的标题")
        return [], notes
    cands.sort(key=lambda c: -c["score"])
    return cands[:limit], notes


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def clean_title(t):
    """去掉 arXiv 标题里的 LaTeX 包装：$π_0$: xxx → π0: xxx"""
    if not t:
        return t
    s = t.replace("$", "")
    s = re.sub(r"\\[a-zA-Z]+", "", s)
    s = re.sub(r"[{}]", "", s)
    s = re.sub(r"[_^]\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or t


def safe_name(title, maxlen=110):
    s = clean_title(title)
    s = re.sub(r'[\\/:*?"<>|]+', " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen].rstrip(" .") or "paper") + ".pdf"


def pdf_ok(data):
    return (bool(data) and data.startswith(b"%PDF") and len(data) > 20000
            and b"%%EOF" in data[-8192:])


def download_pdf(urls, dest, log=None):
    """多镜像、多次重试 + 完整性校验。返回 (ok, bytes, url, errs)。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    errs = []
    if isinstance(urls, str):
        urls = [urls]
    for url in urls:
        if not url:
            continue
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={
                    **UA_WEB, "Referer": "https://arxiv.org/"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = r.read()
                if pdf_ok(data):
                    with open(dest, "wb") as f:
                        f.write(data)
                    return True, len(data), url, errs
                head = data[:14].decode("utf-8", "replace").strip()
                errs.append("%s: 不完整/非PDF(%d bytes, head=%r)" % (url, len(data), head))
                if head.lower().startswith("<!do"):
                    break  # HTML 拦截页，换镜像
            except Exception as e:
                errs.append("%s: %s" % (url, e))
            time.sleep(2 + 2 * attempt)
    return False, 0, None, errs


def pdf_sources(meta):
    out = []
    if meta.get("arxiv_id") and re.match(r"^\d{4}\.\d{4,5}", meta["arxiv_id"]):
        aid = meta["arxiv_id"]
        nov = re.sub(r"v\d+$", "", aid)
        out += ["https://arxiv.org/pdf/%s" % aid,
                "https://arxiv.org/pdf/%sv1" % nov if not nov.endswith("v1") else None,
                "https://export.arxiv.org/pdf/%s" % nov]
    if meta.get("pdf_url"):
        out.insert(0, meta["pdf_url"])
    if meta.get("abs_url", "").startswith("http") and meta["abs_url"].lower().endswith(".pdf"):
        out.insert(0, meta["abs_url"])
    return [u for u in dict.fromkeys(out) if u]


# --------------------------------------------------------------------------- #
# Zotero API 写入
# --------------------------------------------------------------------------- #
def build_item(meta, collection_key, item_type, tags):
    creators = []
    for name in meta.get("authors") or []:
        n = (name or "").strip()
        if not n:
            continue
        if "," in n:
            last, first = [x.strip() for x in n.split(",", 1)]
            creators.append({"creatorType": "author", "firstName": first, "lastName": last})
        else:
            parts = n.split()
            if len(parts) >= 2:
                creators.append({"creatorType": "author",
                                 "firstName": " ".join(parts[:-1]), "lastName": parts[-1]})
            else:
                creators.append({"creatorType": "author", "firstName": "", "lastName": n})
    item = {
        "itemType": item_type,
        "title": clean_title(meta["title"]),
        "creators": creators,
        "abstractNote": meta.get("abstract") or "",
        "date": meta.get("date") or meta.get("year") or "",
        "url": meta.get("abs_url") or meta.get("pdf_url") or "",
        "collections": [collection_key],
        "tags": [{"tag": t.strip()} for t in tags.split(",") if t.strip()],
    }
    if item_type == "preprint":
        item["repository"] = "arXiv" if meta.get("arxiv_id") else ""
    if meta.get("arxiv_id"):
        item["archiveID"] = "arXiv:" + re.sub(r"v\d+$", "", meta["arxiv_id"])
        item["extra"] = "arXiv: " + meta["arxiv_id"]
    return item


def request_with_headers(method, path, body=None, headers=None):
    """带响应头的请求（用于取 Last-Modified-Version）。"""
    z = ZOT
    h = {"Zotero-API-Key": z.key, "Zotero-API-Version": "3"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(z.base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s on %s %s: %s"
                           % (e.code, method, path,
                              e.read()[:400].decode("utf-8", "replace")))


class _Z(object):
    def __init__(self, key, uid):
        self.key, self.uid, self.base = key, uid, S.API_BASE

    def req(self, method, path, body=None, headers=None):
        s, raw, _ = request_with_headers(method, path, body, headers)
        try:
            return s, json.loads(raw or b"{}")
        except ValueError:
            return s, raw


S.API_BASE = "https://api.zotero.org"
ZOT = None


def api_version(key):
    _, raw, _ = request_with_headers("GET", "/users/%s/items/%s" % (ZOT.uid, key))
    d = json.loads(raw or b"{}")
    return d.get("version") or (d.get("data") or {}).get("version")


def patch_item(key, body):
    """按 key 写回条目必须带 If-Unmodified-Since-Version，否则 428。"""
    v = api_version(key)
    return request_with_headers("PATCH", "/users/%s/items/%s" % (ZOT.uid, key),
                                body=body,
                                headers={"If-Unmodified-Since-Version": str(v)})


def api_collections():
    out, start = [], 0
    while True:
        _, data = ZOT.req("GET", "/users/%s/collections?limit=100&start=%d" % (ZOT.uid, start))
        out.extend({"key": c["key"], "name": c["data"]["name"],
                    "parent": c["data"].get("parentCollection") or None} for c in data)
        if len(data) < 100:
            break
        start += 100
    return out


def api_find_collection(name):
    cols = api_collections()
    low = (name or "").strip().lower()
    for c in cols:
        if c["name"].strip().lower() == low:
            return c, cols
    for c in cols:
        if low and low in c["name"].strip().lower():
            return c, cols
    return None, cols


def _same_paper(it, meta):
    d = it.get("data", {})
    if d.get("itemType") in ("attachment", "note"):
        return False
    aid = re.sub(r"v\d+$", "", meta.get("arxiv_id") or "")
    if aid:
        hay = " ".join(str(d.get(f) or "") for f in ("archiveID", "extra", "url", "DOI"))
        if aid in hay:
            return True
    return bool(d.get("title")) and tight(d["title"]) == tight(meta.get("title"))


def api_find_existing(meta):
    """全库查重：同 arXiv 编号或同标题。

    注意 Zotero 的 q 搜索默认只查 标题/作者/年份，对 extra 字段和带 LaTeX 的标题不可靠，
    因此先用 qmode=everything 快查，未命中再全库扫描兜底。
    """
    probes = []
    if meta.get("arxiv_id"):
        probes.append(re.sub(r"v\d+$", "", meta["arxiv_id"]))
    if meta.get("title"):
        probes.append(clean_title(meta["title"]))
    for q in probes:
        try:
            _, hits = ZOT.req("GET", "/users/%s/items/top?q=%s&qmode=everything&limit=25"
                              % (ZOT.uid, urllib.parse.quote(q)))
        except Exception:
            continue
        for it in hits or []:
            if _same_paper(it, meta):
                return it
    # 兜底：分页扫全库
    start = 0
    while start < 1500:
        try:
            _, hits = ZOT.req("GET", "/users/%s/items/top?limit=100&start=%d"
                              % (ZOT.uid, start))
        except Exception:
            break
        if not hits:
            break
        for it in hits:
            if _same_paper(it, meta):
                return it
        if len(hits) < 100:
            break
        start += 100
    return None


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def ambiguous(cands):
    """第一名与第二名分差过小就认为有歧义。"""
    if len(cands) <= 1:
        return False
    return (cands[0].get("score") or 0) - (cands[1].get("score") or 0) < 0.15


def cmd_search(a):
    if a.mode in ("arxiv", "github", "pdf"):
        if a.mode == "arxiv":
            m = FP.ARXIV_URL_RE.search(a.query) or FP.ARXIV_TEXT_RE.search(a.query)
            q = m.group(1) if m else a.query.strip()
            meta, err = FP.arxiv_lookup(q)
            cands = [dict(meta, score=1.0)] if meta else []
            notes = [err] if err else []
        elif a.mode == "github":
            papers, err = FP.from_github(a.query)
            cands = [dict(p, score=0.9) for p in (papers or [])]
            notes = [err] if err else []
        else:
            cands = [{"source": "direct_pdf", "title": os.path.basename(a.query),
                      "arxiv_id": None, "authors": [], "date": "", "abstract": "",
                      "abs_url": a.query, "pdf_url": a.query, "score": 1.0}]
            notes = []
    else:
        cands, notes = resolve(a.query, a.mode, a.hint, a.limit)
    brief = [{"i": i + 1, "title": c.get("title"), "year": c.get("year"),
              "arxiv": c.get("arxiv_id"), "authors": (c.get("authors") or [])[:3],
              "score": c.get("score"), "pdf": bool(pdf_sources(c))}
             for i, c in enumerate(cands)]
    return {"ok": bool(cands), "need_confirm": ambiguous(cands),
            "candidates": brief, "notes": notes}


def cmd_add(a):
    conf = S.load_conf()
    key, uid = conf.get("api_key"), conf.get("user_id")
    if not (key and uid):
        return {"ok": False, "error": "缺少 Zotero 凭据（~/.zotero/config.yaml 的 api_key / user_id）"}
    global ZOT
    ZOT = _Z(key, uid)

    stages = []

    def stage(step, **kw):
        rec = {"stage": step, "t": time.strftime("%H:%M:%S")}
        rec.update(kw)
        stages.append(rec)
        return rec

    # ---- 1. 解析 ----
    if a.mode in ("arxiv", "github", "pdf"):
        sres = cmd_search(a)
        cands = sres["candidates"]
        full = None
        if cands:
            if a.mode == "arxiv":
                m = FP.ARXIV_URL_RE.search(a.query) or FP.ARXIV_TEXT_RE.search(a.query)
                q = m.group(1) if m else a.query.strip()
                full, _ = FP.arxiv_lookup(q)
            elif a.mode == "github":
                papers, _ = FP.from_github(a.query)
                full = (papers or [None])[0]
            else:
                full = {"source": "direct_pdf", "title": os.path.basename(a.query),
                        "authors": [], "date": "", "abstract": "",
                        "abs_url": a.query, "pdf_url": a.query, "arxiv_id": None}
        else:
            full = None
        cand_list = [full] if full else []
        notes = sres["notes"]
    else:
        cand_list, notes = resolve(a.query, a.mode, a.hint, a.limit)
    stage("resolve", count=len(cand_list), notes=notes)
    if not cand_list:
        return {"ok": False, "stage": "resolve", "error": "未解析到论文", "notes": notes}

    if a.pick:
        idx = a.pick - 1
        if idx < 0 or idx >= len(cand_list):
            return {"ok": False, "error": "--pick 超出候选范围（1..%d）" % len(cand_list)}
        meta = cand_list[idx]
    elif ambiguous(cand_list) and not a.yes:
        stage("confirm", needed=True)
        return {"ok": False, "need_confirm": True, "stage": "resolve",
                "candidates": [{"i": i + 1, "title": c.get("title"), "year": c.get("year"),
                                "arxiv": c.get("arxiv_id"), "score": c.get("score")}
                               for i, c in enumerate(cand_list)],
                "error": "命中多篇候选，请用 --pick N 指定（或 --yes 取第一名）",
                "stages": stages}
    else:
        meta = cand_list[0]
    stage("pick", title=meta.get("title"), arxiv=meta.get("arxiv_id"),
          score=meta.get("score"))

    # ---- 2. 分类 ----
    col, cols = api_find_collection(a.collection)
    if not col:
        return {"ok": False, "stage": "collection",
                "error": "未找到分类: %s" % a.collection,
                "available_collections": sorted({c["name"] for c in cols})}
    stage("collection", name=col["name"], key=col["key"])

    # ---- 3. 去重（命中已有条目时：不重复建，按需复用做 PDF 修复） ----
    exist = api_find_existing(meta)
    item_key, att_key = None, None
    if exist:
        item_key = exist["key"]
        d = exist["data"]
        already = col["key"] in (d.get("collections") or [])
        if not already:
            try:
                patch_item(item_key, {"collections": list(d.get("collections") or [])
                                      + [col["key"]]})
                stage("dedupe", action="已存在，补挂到分类", item_key=item_key)
            except Exception as e:
                stage("dedupe", action="已存在，补挂失败", error=str(e)[:200])
        else:
            stage("dedupe", action="已存在且已在分类内", item_key=item_key)
        att = _first_pdf_attachment(item_key)
        if not a.pdf_repair:
            return {"ok": True, "skipped": True, "reason": "库中已存在该论文",
                    "item_key": item_key, "collection": col["name"],
                    "in_collection": already,
                    "attachment_key": (att or {}).get("key"),
                    "stages": stages,
                    "hint": "如需重下/重传 PDF，加 --pdf-repair"}
        att_key = (att or {}).get("key")
        if att_key:
            stage("attachment", action="复用已有附件", att_key=att_key)
        # 顺手把 LaTeX 包装过的标题规范化
        new_title = clean_title(meta.get("title"))
        if new_title and d.get("title") and new_title != d.get("title") \
                and "$" in (d.get("title") or ""):
            try:
                patch_item(item_key, {"title": new_title})
                stage("normalize-title", title=new_title)
            except Exception as e:
                stage("normalize-title", error=str(e)[:140])
    else:
        # ---- 4. 建父条目 ----
        item = build_item(meta, col["key"], a.type, a.tags)
        try:
            _, resp = ZOT.req("POST", "/users/%s/items" % ZOT.uid, body=[item])
        except RuntimeError:
            for f in ("repository", "archiveID"):  # 该字段不被接受时降级重试
                item.pop(f, None)
            _, resp = ZOT.req("POST", "/users/%s/items" % ZOT.uid, body=[item])
        ok = (resp or {}).get("successful", {})
        if "0" not in ok:
            return {"ok": False, "stage": "create-item", "error": "创建条目失败",
                    "zotero_response": resp, "stages": stages}
        item_key = ok["0"]["key"]
        stage("create-item", item_key=item_key)

    # ---- 5. 附件条目 ----
    if not att_key:
        _, aresp = ZOT.req("POST", "/users/%s/items" % ZOT.uid, body=[{
            "itemType": "attachment", "linkMode": "imported_file",
            "title": "Full Text PDF", "contentType": "application/pdf",
            "parentItem": item_key}])
        aok = (aresp or {}).get("successful", {})
        if "0" in aok:
            att_key = aok["0"]["key"]
            stage("create-attachment", att_key=att_key)
        else:
            stage("create-attachment", error=str(aresp)[:200])

    result = {"ok": True, "item_key": item_key, "attachment_key": att_key,
              "collection": col["name"], "collection_key": col["key"],
              "title": clean_title(meta.get("title")), "arxiv": meta.get("arxiv_id"),
              "authors": meta.get("authors"), "date": meta.get("date"),
              "repaired": bool(exist),
              "pdf": None, "cloud": None, "stages": stages}

    if not att_key:
        result["warning"] = "附件条目创建失败，仅入库元数据"
        return result

    # ---- 6. 下载 PDF ----
    fname = safe_name(meta["title"])
    tmpdir = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), "zotero-ingest")
    tmp_pdf = os.path.join(tmpdir, att_key + ".pdf")
    okdl, nbytes, used, errs = download_pdf(pdf_sources(meta), tmp_pdf)
    stage("download", ok=okdl, bytes=nbytes, url=used, errors=errs[-3:] if errs else [])
    if not okdl:
        result["warning"] = "PDF 下载失败，仅入库元数据（可稍后用 --pdf-repair 补）"
        result["download_errors"] = errs[-5:]
        return result

    with open(tmp_pdf, "rb") as f:
        data = f.read()
    md5 = S.md5_bytes(data)
    mtime = int(os.path.getmtime(tmp_pdf) * 1000)

    # ---- 7. 落本地 storage（客户端本地即可打开） ----
    loc = S.local_put(att_key, data, fname)
    stage("local-storage", ok=loc.get("ok"), path=loc.get("path"), bytes=len(data))
    result["pdf"] = {"bytes": len(data), "md5": md5, "filename": fname,
                     "local_path": loc.get("path")}

    # ---- 8. 同步到云端 WebDAV ----
    cloud = {"ok": False}
    try:
        S.ensure_dav_folder()
        up = S.upload_attachment(att_key, tmp_pdf, fname)
        stage("webdav-upload", ok=up.get("ok"), zip_bytes=up.get("zip_bytes"),
              error=up.get("error"))
        if up.get("ok"):
            ver = S.verify_remote(att_key, md5)
            stage("webdav-verify", ok=ver.get("ok"), inner_md5=ver.get("inner_md5"))
            cloud = {"ok": bool(ver.get("ok")), "backend": "webdav",
                     "zip_bytes": up.get("zip_bytes"),
                     "verified": bool(ver.get("ok")), "verify": ver}
        else:
            cloud = {"ok": False, "backend": "webdav", "error": up.get("error")}
    except Exception as e:
        stage("webdav-upload", ok=False, error=str(e)[:300])
        cloud = {"ok": False, "backend": "webdav", "error": str(e)[:300]}
    result["cloud"] = cloud

    # ---- 9. 元数据补 md5/mtime（让其它设备知道文件已在云端） ----
    try:
        patch_item(att_key, {"md5": md5, "mtime": mtime})
        stage("metadata-md5", ok=True)
    except Exception as e:
        stage("metadata-md5", ok=False, error=str(e)[:160])

    # ---- 10. 可选：本地库对齐（省一次云端下载，需短暂关闭 Zotero） ----
    if a.align:
        was_running = S.zotero_running()
        if was_running:
            S.close_zotero()
        row = S.find_local_attachment(att_key)
        if not row and was_running:  # 客户端还没把条目同步下来：重启一次催它拉元数据
            S.start_zotero()
            S.wait_local_attachment(att_key, a.align_wait)
            S.close_zotero()
            row = S.find_local_attachment(att_key)
        if row:
            al = S.align_local(att_key, fname, md5, mtime)
            stage("align-local", ok=al.get("ok"),
                  detail=al.get("error") or al.get("rows"), backup=al.get("backup"))
            result["aligned"] = bool(al.get("ok"))
        else:
            stage("align-local", skipped="本地库尚未同步到该条目，交给客户端自行拉取")
        if was_running:
            S.start_zotero()
    else:
        stage("align-local", skipped="未开启（客户端下次同步会自行从云端拉取）")

    try:
        os.remove(tmp_pdf)
    except OSError:
        pass
    result["ok"] = bool(cloud.get("ok"))
    if not result["ok"]:
        result["warning"] = "PDF 已落本地，但云端同步未完成，手机端会打不开"
    return result


def _first_pdf_attachment(parent_key):
    try:
        _, kids = ZOT.req("GET", "/users/%s/items/%s/children?limit=50"
                          % (ZOT.uid, parent_key))
    except Exception:
        return None
    for k in kids or []:
        d = k.get("data", {})
        if d.get("itemType") == "attachment" and \
                (d.get("contentType") or "").startswith("application/pdf"):
            return {"key": k["key"], "filename": d.get("filename"), "md5": d.get("md5")}
    return None


def cmd_verify(a):
    conf = S.load_conf()
    key, uid = conf.get("api_key"), conf.get("user_id")
    global ZOT
    ZOT = _Z(key, uid)
    out = {"items": []}
    for k in a.keys:
        att = k
        if not a.attachment:
            att = _first_pdf_attachment(k) or {}
            att = att.get("key")
        if not att:
            out["items"].append({"item": k, "ok": False, "error": "未找到 PDF 附件"})
            continue
        v = S.verify_remote(att)
        loc = S.find_local_attachment(att) or {}
        out["items"].append({"item": k, "attachment": att, "cloud_ok": v.get("ok"),
                             "cloud": v, "local": loc.get("path"),
                             "local_sync": loc.get("syncState")})
    out["ok"] = all(i.get("cloud_ok") for i in out["items"]) if out["items"] else False
    return out


def cmd_doctor(a):
    return S.doctor()


def main():
    p = argparse.ArgumentParser(description="论文 → PDF → Zotero 分类 → 坚果云 一条龙")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, with_write=True):
        sp.add_argument("query", help="arXiv 编号/链接、论文标题、GitHub 地址或 PDF 直链")
        sp.add_argument("--mode", default="auto",
                        choices=["auto", "arxiv", "title", "github", "pdf"])
        sp.add_argument("--hint", default="", help="补充检索词，帮助短名消歧（如 pi0 --hint robot）")
        sp.add_argument("--limit", type=int, default=5)

    sp = sub.add_parser("search", help="只解析候选，不写入")
    common(sp)

    ap = sub.add_parser("add", help="完整流水线")
    common(ap)
    ap.add_argument("--collection", required=True, help="目标分类名（支持子串匹配）")
    ap.add_argument("--pick", type=int, default=0, help="多候选时选第 N 篇")
    ap.add_argument("--yes", action="store_true", help="无歧义或多候选时直接取第一名")
    ap.add_argument("--type", default="preprint",
                    choices=["preprint", "journalArticle", "conferencePaper", "report"])
    ap.add_argument("--tags", default="")
    ap.add_argument("--pdf-repair", action="store_true", help="已存在条目时也补传 PDF")
    ap.add_argument("--align", action="store_true",
                    help="额外对齐本地库（会重启 Zotero，省一次云端下载；默认关闭）")
    ap.add_argument("--align-wait", type=int, default=90)

    vp = sub.add_parser("verify", help="校验云端可下载性")
    vp.add_argument("keys", nargs="+", help="item key 或 attachment key")
    vp.add_argument("--attachment", action="store_true", help="传入的已是附件 key")

    sub.add_parser("doctor", help="环境自检")

    a = p.parse_args()
    handler = {"search": cmd_search, "add": cmd_add,
               "verify": cmd_verify, "doctor": cmd_doctor}[a.cmd]
    try:
        out = handler(a)
    except Exception as e:
        out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if out.get("need_confirm"):
        return 3
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
