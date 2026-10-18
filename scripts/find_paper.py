#!/usr/bin/env python3
"""Find paper metadata + PDF URL from an arXiv ID, paper title, GitHub URL, or direct PDF URL.

Usage:
  find_paper.py arxiv 2601.01234
  find_paper.py title "HoloAgent-0 A unified embodied agent framework"
  find_paper.py github https://github.com/HorizonRobotics/HoloAgent
  find_paper.py pdf https://example.org/paper.pdf

Always prints a single JSON object to stdout:
  {"ok": true, "papers": [{source, arxiv_id, title, authors[], year, date, abstract, abs_url, pdf_url}]}
  {"ok": false, "error": "..."}
For title search, "papers" is a candidate list (top 5) — present to the user for confirmation.
Stdlib only. Safe to run with the managed python runtime.
"""
import base64
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "paper-to-zotero-skill/1.0 (mailto:local-user)"}
ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
ARXIV_TEXT_RE = re.compile(r"arxiv[:.\s]+(\d{4}\.\d{4,5})", re.I)


def http_get(url, timeout=30, retries=3):
    """GET with retry/backoff for arXiv API rate limiting (429) and transient 5xx."""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503) and i < retries - 1:
                time.sleep(3 * (i + 1))
                continue
            raise
    raise last


def arxiv_meta(arxiv_id):
    arxiv_id = arxiv_id.strip()
    url = "https://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(arxiv_id)
    root = ET.fromstring(http_get(url))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = root.find("a:entry", ns)
    if entry is None or entry.find("a:title", ns) is None:
        return None
    title_el = entry.find("a:title", ns)
    title = " ".join((title_el.text or "").split())
    if title.startswith("Error") or "not found" in title.lower():
        return None
    authors = [a.find("a:name", ns).text for a in entry.findall("a:author", ns)]
    published = (entry.find("a:published", ns).text or "")[:10]
    summary = " ".join((entry.find("a:summary", ns).text or "").split())
    abs_url = entry.find("a:id", ns).text or ""
    pdf = None
    for link in entry.findall("a:link", ns):
        if link.get("title") == "pdf":
            pdf = link.get("href")
    if not pdf and abs_url:
        pdf = abs_url.replace("/abs/", "/pdf/")
    vid = abs_url.rsplit("/abs/", 1)[-1] if "/abs/" in abs_url else arxiv_id
    return _mk("arxiv", vid, title, authors, published, summary, abs_url, pdf)


def _mk(source, arxiv_id, title, authors, date, abstract, abs_url, pdf_url):
    return {
        "source": source,
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "year": (date or "")[:4],
        "date": date or "",
        "abstract": abstract or "",
        "abs_url": abs_url or "",
        "pdf_url": pdf_url or ("https://arxiv.org/pdf/" + arxiv_id if arxiv_id else ""),
    }


def arxiv_abs_meta(arxiv_id):
    """Scrape the abs page HTML (citation_* meta tags) — no API rate limit."""
    abs_url = "https://arxiv.org/abs/" + arxiv_id.strip()
    page = http_get(abs_url).decode("utf-8", "replace")

    def meta(name, multi=False):
        vals = re.findall(
            r'<meta\s+name="%s"\s+content="([^"]*)"' % re.escape(name), page)
        return vals if multi else (vals[0] if vals else None)

    title = html.unescape(meta("citation_title") or "")
    if not title:
        return None
    authors = [html.unescape(a) for a in (meta("citation_author", multi=True) or [])]
    date = (meta("citation_date") or meta("citation_online_date") or "").replace("/", "-")
    abstract = html.unescape(meta("citation_abstract") or "")
    pdf = meta("citation_pdf_url") or ("https://arxiv.org/pdf/" + arxiv_id.strip())
    return _mk("arxiv_abs_page", arxiv_id.strip(), title, authors, date, abstract,
               abs_url, pdf)


def s2_single(arxiv_id):
    url = ("https://api.semanticscholar.org/graph/v1/paper/arXiv:"
           + urllib.parse.quote(arxiv_id)
           + "?fields=title,authors,year,abstract,externalIds,openAccessPdf,url")
    data = json.loads(http_get(url))
    ext = data.get("externalIds") or {}
    oa = data.get("openAccessPdf") or {}
    aid = ext.get("ArXiv") or arxiv_id
    pdf = oa.get("url") or ("https://arxiv.org/pdf/" + aid)
    return _mk("semantic_scholar", aid, data.get("title"),
               [a.get("name") for a in (data.get("authors") or []) if a.get("name")],
               str(data.get("year") or ""), data.get("abstract") or "",
               data.get("url") or "", pdf)


def arxiv_lookup(arxiv_id):
    """Try arXiv API -> Semantic Scholar -> abs-page scrape."""
    errs = []
    for fn in (arxiv_meta, s2_single, arxiv_abs_meta):
        try:
            meta = fn(arxiv_id)
            if meta:
                return meta, None
        except Exception as e:
            errs.append("%s: %s" % (fn.__name__, e))
    return None, "; ".join(errs)


def _arxiv_query(search_query, limit):
    url = ("https://export.arxiv.org/api/query?search_query="
           + urllib.parse.quote(search_query)
           + "&start=0&max_results=" + str(limit))
    root = ET.fromstring(http_get(url))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for entry in root.findall("a:entry", ns):
        abs_url = entry.find("a:id", ns).text or ""
        if "/abs/" not in abs_url:
            continue
        vid = abs_url.rsplit("/abs/", 1)[-1]
        title_el = entry.find("a:title", ns)
        t = " ".join((title_el.text or "").split()) if title_el is not None else ""
        authors = [a.find("a:name", ns).text for a in entry.findall("a:author", ns)]
        published = (entry.find("a:published", ns).text or "")[:10]
        summary = " ".join((entry.find("a:summary", ns).text or "").split())
        out.append(_mk("arxiv", vid, t, authors, published, summary, abs_url,
                       "https://arxiv.org/pdf/" + vid))
    return out


def arxiv_search(title, limit=5):
    """Try progressively looser arXiv queries; exact-phrase search on a long title often misses."""
    words = [w for w in re.split(r"[\s:,\-]+", title) if w]
    queries = []
    if len(words) <= 25:
        queries.append('ti:"%s"' % title.replace('"', ""))
        queries.append('all:"%s"' % title.replace('"', ""))
    if words:
        queries.append("all:" + "+".join(words[:10]))
        queries.append("ti:" + "+".join(words[:6]))
    for q in queries:
        try:
            hits = _arxiv_query(q, limit)
        except Exception:
            continue
        if hits:
            return hits
    return []


def s2_search_ex(title, limit=5):
    """Returns (papers, error_note). Distinguishes 'no match' from rate limiting."""
    url = ("https://api.semanticscholar.org/graph/v1/paper/search?query="
           + urllib.parse.quote(title)
           + "&limit=" + str(limit)
           + "&fields=title,authors,year,abstract,externalIds,openAccessPdf,url")
    try:
        data = json.loads(http_get(url))
    except urllib.error.HTTPError as e:
        return [], "Semantic Scholar HTTP %s（可能限流）" % e.code
    except Exception as e:
        return [], "Semantic Scholar: %s" % e
    out = []
    for p in data.get("data") or []:
        ext = p.get("externalIds") or {}
        oa = p.get("openAccessPdf") or {}
        arxiv_id = ext.get("ArXiv")
        pdf_url = oa.get("url") or ("https://arxiv.org/pdf/" + arxiv_id if arxiv_id else "")
        out.append(_mk("semantic_scholar", arxiv_id, p.get("title"),
                       [a.get("name") for a in (p.get("authors") or []) if a.get("name")],
                       str(p.get("year") or ""), p.get("abstract") or "",
                       p.get("url") or "", pdf_url))
    return out, None


def s2_search(title, limit=5):
    return s2_search_ex(title, limit)[0]


def github_readme(repo_url):
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)", repo_url.strip())
    if not m:
        return None, None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    api = "https://api.github.com/repos/%s/%s/readme" % (owner, repo)
    try:
        req = urllib.request.Request(api, headers={**UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    except Exception:
        text = None
        for branch in ("main", "master"):
            for name in ("README.md", "readme.md"):
                try:
                    text = http_get(
                        "https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, repo, branch, name)
                    ).decode("utf-8", "replace")
                    break
                except Exception:
                    continue
            if text:
                break
    return (owner + "/" + repo), text


def from_github(repo_url):
    repo, readme = github_readme(repo_url)
    if not readme:
        return None, "无法获取该仓库的 README，请确认链接公开可访问"
    ids = []
    for mid in ARXIV_URL_RE.findall(readme) + ARXIV_TEXT_RE.findall(readme):
        if mid not in ids:
            ids.append(mid)
    if not ids:
        return None, ("README 中未发现 arXiv 链接/编号（repo: %s）。"
                      "可尝试用标题搜索：find_paper.py title \"<论文题目>\"" % repo)
    papers = []
    for mid in ids[:3]:
        meta, _ = arxiv_lookup(mid)
        if meta:
            papers.append(meta)
    if not papers:
        return None, "README 中的 arXiv 编号 %s 查询失败" % ids
    return papers, None


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    if len(sys.argv) < 3:
        emit({"ok": False, "error": "usage: find_paper.py <arxiv|title|github|pdf> <query>"})
        return 2
    mode, query = sys.argv[1], sys.argv[2]
    try:
        if mode == "arxiv":
            q = query.strip()
            m = ARXIV_URL_RE.search(q) or ARXIV_TEXT_RE.search(q)
            if m:
                q = m.group(1)
            if not ARXIV_ID_RE.match(q):
                emit({"ok": False, "error": "不是合法的 arXiv 编号: " + q})
                return 2
            meta, err = arxiv_lookup(q)
            emit({"ok": bool(meta), "papers": [meta] if meta else [],
                  "error": None if meta else "arXiv 未找到该编号（%s）" % err})
        elif mode == "title":
            papers, s2_err = s2_search_ex(query)
            papers = papers or []
            arxiv_hits = []
            try:
                arxiv_hits = arxiv_search(query) or []
            except Exception as e:
                s2_err = (s2_err + "; " if s2_err else "") + "arXiv API: %s" % e
            seen = {p["title"] for p in papers}
            for p in arxiv_hits:
                if p["title"] not in seen:
                    papers.append(p)
            if not papers:
                emit({"ok": False, "papers": [],
                      "error": "未匹配到论文" + ("（%s）" % s2_err if s2_err else "")
                               + "。可改用 arXiv 编号、GitHub 链接，或提供 PDF 直链。"})
                return 1
            emit({"ok": True, "papers": papers[:5], "error": s2_err})
        elif mode == "github":
            papers, err = from_github(query)
            emit({"ok": bool(papers), "papers": papers or [], "error": err})
        elif mode == "pdf":
            emit({"ok": True, "papers": [{
                "source": "direct_pdf", "arxiv_id": None, "title": None,
                "authors": [], "year": "", "date": "", "abstract": "",
                "abs_url": query, "pdf_url": query}]})
        else:
            emit({"ok": False, "error": "未知模式: " + mode})
            return 2
    except Exception as e:
        emit({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
