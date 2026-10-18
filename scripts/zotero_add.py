#!/usr/bin/env python3
"""Deprecated: 只用于纯元数据场景（check / collections）。

PDF 附件请改用 zotero_ingest.py add，它会落本地 storage 并上传到 WebDAV。
本文件的 attach_pdf() 走的是 Zotero 官方存储上传四步，对使用 WebDAV 的账号无效：
文件不会进坚果云，且写回 md5 后客户端会误判已同步，导致手机端永远打不开。

Add a paper item (with optional PDF attachment) to a Zotero collection via the Web API.

Config (first run creates it interactively per SKILL.md):
  ~/.zotero/config.yaml   ->   api_key: <key>  \n  user_id: <numeric id>
Env overrides: ZOTERO_API_KEY, ZOTERO_USER_ID.

Usage:
  zotero_add.py check                          # verify credentials, print user + collections
  zotero_add.py collections                    # list collection names and keys
  zotero_add.py add --title "..." --collection "Robot Learning" \
      [--authors "A; B; C"] [--date 2026-01-01] [--arxiv 2601.01234] \
      [--url https://arxiv.org/abs/...] [--abstract "..."] [--pdf path/to.pdf] \
      [--type journalArticle|preprint|conferencePaper] [--tags "vla,embodied"]

Always prints a single JSON object to stdout: {"ok": bool, ...}
Stdlib only (no requests dependency).
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.zotero.org"
CONF = os.path.join(os.path.expanduser("~"), ".zotero", "config.yaml")


def load_conf():
    key = os.environ.get("ZOTERO_API_KEY")
    uid = os.environ.get("ZOTERO_USER_ID")
    if not (key and uid) and os.path.exists(CONF):
        with open(CONF, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("api_key:"):
                    key = key or line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.strip().startswith("user_id:"):
                    uid = uid or line.split(":", 1)[1].strip().strip('"').strip("'")
    return key, uid


class Z:
    def __init__(self, key, uid):
        self.key, self.uid = key, uid

    def req(self, method, path, body=None, headers=None, raw=False, url=None):
        h = {"Zotero-API-Key": self.key, "Zotero-API-Version": "3"}
        if headers:
            h.update(headers)
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
            if not isinstance(body, (bytes, bytearray)):
                h.setdefault("Content-Type", "application/json")
        full = url or (API + path)
        req = urllib.request.Request(full, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = r.read()
                return r.status, (payload if raw else json.loads(payload or b"{}"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError("HTTP %s on %s %s: %s" % (e.code, method, full, detail))

    def collections(self):
        out, start = [], 0
        while True:
            _, data = self.req("GET",
                               "/users/%s/collections?limit=100&start=%d" % (self.uid, start))
            out.extend({"key": c["key"], "name": c["data"]["name"],
                        "parent": c["data"].get("parentCollection") or None} for c in data)
            if len(data) < 100:
                break
            start += 100
        return out

    def find_collection(self, name):
        cols = self.collections()
        low = name.strip().lower()
        for c in cols:
            if c["name"].strip().lower() == low:
                return c, cols
        for c in cols:  # fallback: substring
            if low in c["name"].strip().lower():
                return c, cols
        return None, cols


def parse_authors(s):
    out = []
    for name in [x.strip() for x in s.split(";") if x.strip()]:
        if "," in name:  # "Last, First" (e.g. arXiv abs-page meta tags)
            last, first = [p.strip() for p in name.split(",", 1)]
            out.append({"creatorType": "author", "firstName": first, "lastName": last})
        else:
            parts = name.split()
            if len(parts) >= 2:
                out.append({"creatorType": "author",
                            "firstName": " ".join(parts[:-1]), "lastName": parts[-1]})
            else:
                out.append({"creatorType": "author", "lastName": name, "firstName": ""})
    return out


def cmd_check(z):
    cols = z.collections()
    return {"ok": True, "user_id": z.uid, "collection_count": len(cols),
            "collections": [c["name"] for c in cols]}


def cmd_collections(z):
    return {"ok": True, "collections": z.collections()}


def cmd_add(z, a):
    col, all_cols = z.find_collection(a.collection)
    if not col:
        return {"ok": False, "error": "未找到分类: %s" % a.collection,
                "available_collections": [c["name"] for c in all_cols]}
    # dedupe: same title already in this collection?
    if not a.force:
        try:
            _, existing = z.req(
                "GET", "/users/%s/collections/%s/items?q=%s&limit=25&format=json"
                % (z.uid, col["key"], urllib.parse.quote(a.title)))
            norm = lambda s: "".join((s or "").lower().split())
            for it in existing:
                d = it.get("data", {})
                if d.get("itemType") == "attachment":
                    continue
                if norm(d.get("title")) == norm(a.title):
                    return {"ok": True, "skipped": True, "reason": "该分类下已存在同名条目",
                            "item_key": it.get("key"), "collection": col["name"]}
        except Exception:
            pass
    item = {
        "itemType": a.type,
        "title": a.title,
        "creators": parse_authors(a.authors) if a.authors else [],
        "abstractNote": a.abstract or "",
        "date": a.date or "",
        "url": a.url or "",
        "collections": [col["key"]],
        "tags": [{"tag": t.strip()} for t in a.tags.split(",") if t.strip()] if a.tags else [],
    }
    if a.arxiv:
        item["extra"] = "arXiv: " + a.arxiv
    status, resp = z.req("POST", "/users/%s/items" % z.uid, body=[item])
    ok = resp.get("successful", {})
    if "0" not in ok:
        return {"ok": False, "error": "创建条目失败", "zotero_response": resp}
    item_key = ok["0"]["key"]
    result = {"ok": True, "item_key": item_key, "collection": col["name"],
              "pdf_attached": False}
    if a.pdf:
        result.update(attach_pdf(z, item_key, a.pdf))
    return result


def attach_pdf(z, parent_key, pdf_path):
    if not os.path.isfile(pdf_path):
        return {"ok": False, "error": "PDF 不存在: " + pdf_path}
    fname = os.path.basename(pdf_path)
    data = open(pdf_path, "rb").read()
    md5 = hashlib.md5(data).hexdigest()
    size = len(data)
    mtime = str(int(os.path.getmtime(pdf_path) * 1000))
    # step 1: attachment item
    _, resp = z.req("POST", "/users/%s/items" % z.uid, body=[{
        "itemType": "attachment", "linkMode": "imported_file", "title": fname,
        "contentType": "application/pdf", "parentItem": parent_key}])
    ok = resp.get("successful", {})
    if "0" not in ok:
        return {"ok": False, "error": "创建附件条目失败", "zotero_response": resp}
    att_key = ok["0"]["key"]
    # step 2: upload authorization
    form = urllib.parse.urlencode({"md5": md5, "filename": fname,
                                   "filesize": size, "mtime": mtime}).encode()
    try:
        _, auth = z.req("POST", "/users/%s/items/%s/file" % (z.uid, att_key),
                        body=form,
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "If-None-Match": "*"})
    except Exception as e:
        # 服务端已有同名/同 md5 文件时会返回 412 "file exists"，视为已上传
        if "HTTP 412" in str(e):
            return {"ok": True, "pdf_attached": True, "attachment_key": att_key,
                    "pdf_bytes": size, "pdf_md5": md5, "note": "server: file exists (412)"}
        raise
    # 服务端已存在该文件（md5 命中），无需再上传
    if isinstance(auth, dict) and auth.get("exists"):
        return {"ok": True, "pdf_attached": True, "attachment_key": att_key,
                "pdf_bytes": size, "pdf_md5": md5, "note": "server: exists"}
    # step 3: upload file (prefix + file + suffix)
    body = (auth["prefix"].encode("utf-8") + data + auth["suffix"].encode("utf-8"))
    z.req("POST", None, body=body, url=auth["url"],
          headers={"Content-Type": auth["contentType"]}, raw=True)
    # step 4: register upload
    z.req("POST", "/users/%s/items/%s/file?upload=%s" % (z.uid, att_key, auth["uploadKey"]),
          body=b"", headers={"If-None-Match": "*"}, raw=True)
    return {"ok": True, "pdf_attached": True, "attachment_key": att_key,
            "pdf_bytes": size, "pdf_md5": md5}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("collections")
    pa = sub.add_parser("add")
    pa.add_argument("--title", required=True)
    pa.add_argument("--collection", required=True)
    pa.add_argument("--authors", default="")
    pa.add_argument("--date", default="")
    pa.add_argument("--arxiv", default="")
    pa.add_argument("--url", default="")
    pa.add_argument("--abstract", default="")
    pa.add_argument("--pdf", default="")
    pa.add_argument("--type", default="preprint",
                    choices=["preprint", "journalArticle", "conferencePaper", "report"])
    pa.add_argument("--tags", default="")
    pa.add_argument("--force", action="store_true",
                    help="跳过同分类同名条目检查，强制新增")
    a = p.parse_args()

    key, uid = load_conf()
    if not (key and uid):
        print(json.dumps({"ok": False,
                          "error": "缺少 Zotero 凭据。请按 SKILL.md「首次配置」写入 "
                                   + CONF + " 或设置 ZOTERO_API_KEY / ZOTERO_USER_ID"},
                         ensure_ascii=False))
        return 2
    z = Z(key, uid)
    try:
        if a.cmd == "check":
            out = cmd_check(z)
        elif a.cmd == "collections":
            out = cmd_collections(z)
        else:
            out = cmd_add(z, a)
    except Exception as e:
        out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
