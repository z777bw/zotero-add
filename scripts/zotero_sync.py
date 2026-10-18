#!/usr/bin/env python3
"""Zotero 本地存储 + WebDAV(坚果云) 文件同步层。

职责：把一份 PDF 按 Zotero 客户端真实使用的存储格式落到两处，使桌面端与手机端
都能直接打开：

  1) 本地： <dataDir>/storage/<attachment_key>/<filename>.pdf
  2) 云端： <webdav_url>/zotero/<attachment_key>.zip  +  <attachment_key>.prop

.prop 内容为 `<properties version="1"><mtime>毫秒</mtime><hash>内层PDF的md5</hash></properties>`，
zip 内是单条 deflate 压缩条目（条目名 = PDF 文件名）。

子命令（也可被 zotero_ingest.py 当模块导入）：
  doctor                         环境自检（Zotero 路径 / 数据目录 / WebDAV 配置 / 凭据）
  creds                          仅验证能否解出 WebDAV 凭据（不打印明文）
  upload --key K --pdf P         上传单个附件到 WebDAV
  verify --key K [--md5 M]       从 WebDAV 回读并校验
  align  --key K --pdf P         对齐本地库（path/syncState/storageHash），需 Zotero 关闭
  close / start                  Zotero 优雅关闭 / 启动

仅用标准库；Windows 下用 Zotero 自带的 nss3.dll 解密 logins.json。
"""
import argparse
import base64
import ctypes
import glob
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

IS_WIN = os.name == "nt"
CONF = os.path.join(os.path.expanduser("~"), ".zotero", "config.yaml")
ZAUTH_REALM = "Zotero Storage Server"

# Zotero 客户端常量（chrome/content/zotero/xpcom/storage/storageLocal.js）
SYNC_TO_UPLOAD, SYNC_TO_DOWNLOAD, SYNC_IN_SYNC = 0, 1, 2


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_conf():
    """读 ~/.zotero/config.yaml（极简 key: value 解析，不依赖 PyYAML）。"""
    conf = {}
    if os.path.exists(CONF):
        with open(CONF, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or ":" not in s:
                    continue
                k, v = s.split(":", 1)
                v = v.strip().strip('"').strip("'")
                if v:
                    conf[k.strip()] = v
    for env, key in (("ZOTERO_API_KEY", "api_key"), ("ZOTERO_USER_ID", "user_id"),
                     ("ZOTERO_HOME", "zotero_home"),
                     ("ZOTERO_WEBDAV_URL", "webdav_url"),
                     ("ZOTERO_WEBDAV_USER", "webdav_user"),
                     ("ZOTERO_WEBDAV_PASSWORD", "webdav_password")):
        if os.environ.get(env):
            conf[key] = os.environ[env]
    return conf


# --------------------------------------------------------------------------- #
# Zotero 安装目录 / 配置目录 / 数据目录
# --------------------------------------------------------------------------- #
def zotero_home(conf=None):
    conf = conf or load_conf()
    cands = [conf.get("zotero_home")]
    if IS_WIN:
        try:  # 运行中优先取真实 exe 路径（自定义安装盘符也能命中）
            out = subprocess.run(
                ["wmic", "process", "where", "name='zotero.exe'", "get", "ExecutablePath"],
                capture_output=True, text=True, timeout=8, errors="replace").stdout
            for line in out.splitlines():
                line = line.strip()
                if line.lower().endswith("zotero.exe"):
                    cands.append(os.path.dirname(line))
        except Exception:
            pass
        cands += [r"F:\Zotero", r"C:\Program Files\Zotero",
                  r"C:\Program Files (x86)\Zotero",
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "Zotero")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "zotero.exe")):
            return c
    return None


def profile_dir():
    base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Zotero", "Zotero", "Profiles")
    best, best_mtime = None, -1
    for p in glob.glob(os.path.join(base, "*")):
        pf = os.path.join(p, "prefs.js")
        if os.path.isfile(pf):
            m = os.path.getmtime(pf)
            if m > best_mtime:
                best, best_mtime = p, m
    return best


def prefs(prof=None):
    prof = prof or profile_dir()
    out = {}
    if not prof:
        return out
    pf = os.path.join(prof, "prefs.js")
    if not os.path.isfile(pf):
        return out
    with open(pf, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r'user_pref\("([^"]+)",\s*(.*?)\);?\s*$', line.strip())
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip()
            if v in ("true", "false"):
                out[k] = (v == "true")
            elif v.startswith('"') and v.endswith('"'):
                out[k] = v[1:-1].replace("\\\\", "\\").replace('\\"', '"')
            else:
                try:
                    out[k] = int(v)
                except ValueError:
                    out[k] = v
    return out


def data_dir(conf=None):
    conf = conf or load_conf()
    p = prefs()
    if p.get("extensions.zotero.useDataDir") and p.get("extensions.zotero.dataDir"):
        return p["extensions.zotero.dataDir"]
    prof = profile_dir()
    return os.path.join(prof, "Zotero") if prof else None


def storage_dir(conf=None):
    d = data_dir(conf)
    return os.path.join(d, "storage") if d else None


def db_path(conf=None):
    d = data_dir(conf)
    return os.path.join(d, "zotero.sqlite") if d else None


# --------------------------------------------------------------------------- #
# WebDAV 配置与凭据
# --------------------------------------------------------------------------- #
def webdav_conf(conf=None):
    conf = conf or load_conf()
    p = prefs()
    proto = conf.get("webdav_protocol") or p.get("extensions.zotero.sync.storage.protocol")
    url = conf.get("webdav_url") or p.get("extensions.zotero.sync.storage.url") or ""
    user = conf.get("webdav_user") or p.get("extensions.zotero.sync.storage.username") or ""
    return {"protocol": proto or "zotero", "url": url, "user": user}


class _NSS(object):
    """用 Zotero 自带的 NSS 解密 logins.json（PK11SDR_Decrypt）。"""

    class SECItem(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint), ("data", ctypes.c_void_p),
                    ("len", ctypes.c_uint)]

    def __init__(self, home, prof):
        self.ok, self.err = False, None
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(home)
            for dll in ("mozglue.dll", "nss3.dll"):
                p = os.path.join(home, dll)
                if not os.path.isfile(p):
                    raise RuntimeError("缺少 %s（Zotero 安装目录：%s）" % (dll, home))
            try:
                ctypes.WinDLL(os.path.join(home, "mozglue.dll"))
            except Exception:
                pass
            nss = ctypes.WinDLL(os.path.join(home, "nss3.dll"))
            self.nss = nss
            nss.NSS_Init.restype = ctypes.c_int
            nss.NSS_Init.argtypes = [ctypes.c_char_p]
            nss.PK11_GetInternalKeySlot.restype = ctypes.c_void_p
            nss.PK11_Authenticate.restype = ctypes.c_int
            nss.PK11_Authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            nss.PK11SDR_Decrypt.restype = ctypes.c_int
            nss.PK11SDR_Decrypt.argtypes = [ctypes.POINTER(self.SECItem),
                                            ctypes.POINTER(self.SECItem), ctypes.c_void_p]
            rc = -1
            for arg in (prof, "sql:" + prof):
                rc = nss.NSS_Init(arg.replace("\\", "/").encode())
                if rc == 0:
                    break
            if rc != 0:
                raise RuntimeError("NSS_Init 失败(rc=%s)，profile=%s" % (rc, prof))
            self.slot = nss.PK11_GetInternalKeySlot()
            if self.slot:
                nss.PK11_Authenticate(self.slot, 1, None)
            self.ok = True
        except Exception as e:
            self.err = "%s: %s" % (type(e).__name__, e)

    def decrypt(self, b64):
        if not b64:
            return None
        raw = base64.b64decode(b64)
        buf = ctypes.create_string_buffer(raw, len(raw))
        i = self.SECItem(0, ctypes.cast(buf, ctypes.c_void_p), len(raw))
        o = self.SECItem(0, None, 0)
        if self.nss.PK11SDR_Decrypt(ctypes.byref(i), ctypes.byref(o), None) != 0:
            return None
        return ctypes.string_at(o.data, o.len).decode("utf-8", "replace")


_CRED_CACHE = {}


def webdav_password(conf=None):
    """返回 (password, source)。优先 config.yaml，其次解密 Zotero logins.json。"""
    conf = conf or load_conf()
    if conf.get("webdav_password"):
        return conf["webdav_password"], "config"
    if _CRED_CACHE.get("pw"):
        return _CRED_CACHE["pw"], _CRED_CACHE.get("src", "cache")
    prof = profile_dir()
    home = zotero_home(conf)
    if not (prof and home):
        return None, "no-profile"
    lf = os.path.join(prof, "logins.json")
    if not os.path.isfile(lf):
        return None, "no-logins"
    nss = _NSS(home, prof)
    if not nss.ok:
        return None, "nss:" + str(nss.err)
    try:
        logins = json.load(open(lf, encoding="utf-8")).get("logins", [])
    except Exception as e:
        return None, "logins.json: %s" % e
    want = (conf.get("webdav_url") or "").split("/")[0].lower()
    w_user = (webdav_conf(conf).get("user") or "")
    wanted_user = storage_realm = None
    for l in logins:
        realm = (l.get("httpRealm") or "").strip()
        # 只认文件存储凭据；库里的 "Zotero Web API"(API Key) 与 "(encrypted)" 变体必须排除
        if "web api" in realm.lower() or "encrypted" in realm.lower():
            continue
        user = nss.decrypt(l.get("encryptedUsername"))
        pw = nss.decrypt(l.get("encryptedPassword"))
        if not pw:
            continue
        if realm == ZAUTH_REALM:
            storage_realm = storage_realm or pw
            if user and w_user and user.lower() == w_user.lower():
                wanted_user = pw
        elif "storage" in realm.lower():
            storage_realm = storage_realm or pw
        elif want and want in (l.get("hostname") or "").lower():
            wanted_user = wanted_user or pw
    pw = wanted_user or storage_realm
    if pw:
        _CRED_CACHE.update(pw=pw, src="zotero-logins")
        return pw, "zotero-logins"
    return None, "no-match"


def dav_base(conf=None):
    """规范化出 https://host/path/ 形式。"""
    conf = conf or load_conf()
    w = webdav_conf(conf)
    u = (w["url"] or "").strip().rstrip("/")
    if not u:
        return None
    if not u.lower().startswith("http"):
        u = "https://" + u
    return u + "/"


def dav_auth(conf=None):
    conf = conf or load_conf()
    w = webdav_conf(conf)
    pw, src = webdav_password(conf)
    if not (w["user"] and pw):
        raise RuntimeError("无法取得 WebDAV 凭据（user=%r, source=%s）" % (w["user"], src))
    return base64.b64encode(("%s:%s" % (w["user"], pw)).encode()).decode()


def dav_request(method, path, data=None, ctype=None, conf=None, timeout=300, depth=None):
    base = dav_base(conf)
    url = base + (path or "")
    h = {"Authorization": "Basic " + dav_auth(conf)}
    if ctype:
        h["Content-Type"] = ctype
    if depth:
        h["Depth"] = depth
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        raise RuntimeError("HTTP %s on %s %s: %s" % (e.code, method, url, body))


# --------------------------------------------------------------------------- #
# 存储对象：zip + prop
# --------------------------------------------------------------------------- #
def md5_bytes(data):
    return hashlib.md5(data).hexdigest()


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def build_zip(data, filename, mtime_ms):
    ts = time.localtime(mtime_ms / 1000.0)[:6]
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        zi = zipfile.ZipInfo(filename, date_time=ts)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = 0o600 << 16
        z.writestr(zi, data)
    return bio.getvalue()


def build_prop(md5, mtime_ms):
    return ('<properties version="1"><mtime>%d</mtime><hash>%s</hash></properties>'
            % (mtime_ms, md5)).encode("utf-8")


def ensure_dav_folder(conf=None):
    """确保 WebDAV 上有 zotero/ 目录（已存在时 PROPFIND 成功即可）。"""
    try:
        dav_request("PROPFIND", "zotero/", b"", ctype="application/xml",
                    conf=conf, depth="0")
        return True
    except Exception:
        pass
    try:
        dav_request("MKCOL", "zotero/", b"", conf=conf)
        return True
    except Exception:
        return False


def upload_attachment(att_key, pdf_path, filename=None, conf=None):
    """把 PDF 以 Zotero WebDAV 格式上传。返回 dict。"""
    if not os.path.isfile(pdf_path):
        return {"ok": False, "error": "PDF 不存在: " + pdf_path}
    filename = filename or os.path.basename(pdf_path)
    with open(pdf_path, "rb") as f:
        data = f.read()
    if not data.startswith(b"%PDF"):
        return {"ok": False, "error": "文件不是 PDF（缺少 %PDF 头）: " + pdf_path}
    md5 = md5_bytes(data)
    mtime = int(os.path.getmtime(pdf_path) * 1000)
    zb = build_zip(data, filename, mtime)
    prop = build_prop(md5, mtime)
    last = None
    for attempt in range(3):
        try:
            s1, _ = dav_request("PUT", "zotero/%s.zip" % att_key, zb,
                                ctype="application/zip", conf=conf)
            s2, _ = dav_request("PUT", "zotero/%s.prop" % att_key, prop,
                                ctype="application/xml", conf=conf)
            return {"ok": True, "key": att_key, "filename": filename,
                    "pdf_bytes": len(data), "zip_bytes": len(zb),
                    "md5": md5, "mtime": mtime,
                    "zip_status": s1, "prop_status": s2}
        except Exception as e:
            last = e
            msg = str(e)
            if "HTTP 403" in msg or "HTTP 507" in msg:  # 配额/权限，重试无意义
                break
            time.sleep(2 * (attempt + 1))
    return {"ok": False, "key": att_key, "error": str(last)}


def verify_remote(att_key, expect_md5=None, conf=None):
    """回读 WebDAV 上的 zip + prop，校验内层 PDF 的 md5。"""
    try:
        _, prop_raw = dav_request("GET", "zotero/%s.prop" % att_key, conf=conf)
        prop = prop_raw.decode("utf-8", "replace")
        m = re.search(r"<hash>([0-9a-f]{32})</hash>", prop)
        remote_hash = m.group(1) if m else None
        mt = re.search(r"<mtime>(\d+)</mtime>", prop)
        _, zb = dav_request("GET", "zotero/%s.zip" % att_key, conf=conf, timeout=600)
        with zipfile.ZipFile(io.BytesIO(zb)) as z:
            names = z.namelist()
            inner = z.read(names[0]) if names else b""
        inner_md5 = md5_bytes(inner)
        ok = bool(inner.startswith(b"%PDF")) and (remote_hash == inner_md5) and \
            (not expect_md5 or expect_md5 == inner_md5)
        return {"ok": ok, "key": att_key, "filename": names[0] if names else None,
                "inner_md5": inner_md5, "prop_hash": remote_hash,
                "prop_mtime": int(mt.group(1)) if mt else None,
                "pdf_bytes": len(inner), "zip_bytes": len(zb)}
    except Exception as e:
        return {"ok": False, "key": att_key, "error": str(e)}


# --------------------------------------------------------------------------- #
# 本地 storage
# --------------------------------------------------------------------------- #
def local_put(att_key, data, filename, conf=None):
    sd = storage_dir(conf)
    if not sd:
        return {"ok": False, "error": "找不到 Zotero 数据目录"}
    d = os.path.join(sd, att_key)
    os.makedirs(d, exist_ok=True)
    for old in os.listdir(d):  # 一个附件目录内只留一个文件
        if old != filename:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass
    p = os.path.join(d, filename)
    with open(p, "wb") as f:
        f.write(data)
    return {"ok": True, "path": p, "bytes": len(data)}


# --------------------------------------------------------------------------- #
# Zotero 进程
# --------------------------------------------------------------------------- #
def zotero_running():
    if not IS_WIN:
        return bool(subprocess.run(["pgrep", "-x", "zotero"], capture_output=True).stdout)
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq zotero.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    return "zotero.exe" in out.lower()


def close_zotero(timeout=40):
    """优雅关闭（CloseMainWindow），失败再 taskkill。返回 True 表示已无进程。"""
    if not zotero_running():
        return True
    ps = ("$p = Get-Process zotero -ErrorAction SilentlyContinue;"
          "foreach ($x in $p) { if ($x.MainWindowHandle -ne 0) { $null = $x.CloseMainWindow() } }")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, timeout=60)
    for _ in range(timeout):
        if not zotero_running():
            return True
        time.sleep(1)
    subprocess.run(["taskkill", "/IM", "zotero.exe", "/F"], capture_output=True)
    time.sleep(3)
    return not zotero_running()


def start_zotero(conf=None, wait=40):
    """启动 Zotero。Windows 下必须用 WMI 拉起，否则进程会随调用方退出而被回收。"""
    home = zotero_home(conf)
    if not home:
        return False
    exe = os.path.join(home, "zotero.exe")
    if IS_WIN:
        ps = ('([wmiclass]"Win32_Process").Create(\'%s\',\'%s\')'
              % (exe.replace("'", "''"), home.replace("'", "''")))
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps],
                           capture_output=True, text=True, timeout=60,
                           errors="replace")
        except Exception:
            pass
    else:
        try:
            subprocess.Popen([exe], cwd=home)
        except Exception:
            return False
    for _ in range(wait):
        if zotero_running():
            return True
        time.sleep(1)
    return zotero_running()


# --------------------------------------------------------------------------- #
# 本地库对齐（可选）
# --------------------------------------------------------------------------- #
def _db_copy():
    src = db_path()
    if not src or not os.path.isfile(src):
        return None, None
    tmp = os.path.join(tempfile.gettempdir(), "zotero_probe_%d.sqlite" % os.getpid())
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(src + suf):
            shutil.copy2(src + suf, tmp + suf)
    return tmp, src


def find_local_attachment(att_key):
    """在本地库里查附件行（返回 dict 或 None）。用副本读，避免锁库。"""
    tmp, _ = _db_copy()
    if not tmp:
        return None
    try:
        con = sqlite3.connect("file:%s?immutable=1" % tmp, uri=True)
        cur = con.cursor()
        cur.execute("SELECT i.itemID, a.path, a.syncState, a.storageModTime, a.storageHash "
                    "FROM items i LEFT JOIN itemAttachments a ON a.itemID=i.itemID "
                    "WHERE i.key=?", (att_key,))
        row = cur.fetchone()
        con.close()
        if not row:
            return None
        return {"itemID": row[0], "path": row[1], "syncState": row[2],
                "storageModTime": row[3], "storageHash": row[4]}
    except Exception:
        return None
    finally:
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(tmp + suf)
            except OSError:
                pass


def wait_local_attachment(att_key, timeout=90):
    """轮询等待客户端把条目同步到本地库（Zotero 运行中会自动拉取）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        row = find_local_attachment(att_key)
        if row:
            return row
        time.sleep(5)
    return None


def align_local(att_key, filename, md5, mtime_ms, conf=None):
    """把本地库的 path/syncState/storageHash 改成与已上传文件一致。

    需要 Zotero 已退出（由调用方负责关闭/重启）。返回 dict。
    """
    if zotero_running():
        return {"ok": False, "error": "Zotero 仍在运行，未修改数据库"}
    src = db_path(conf)
    if not src or not os.path.isfile(src):
        return {"ok": False, "error": "找不到 zotero.sqlite"}
    bak = os.path.join(os.path.dirname(src),
                       "backup-%s" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.makedirs(bak, exist_ok=True)
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(src + suf):
                shutil.copy2(src + suf, os.path.join(bak, "zotero.sqlite" + suf))
    except Exception as e:
        return {"ok": False, "error": "备份失败，已中止: %s" % e}
    try:
        con = sqlite3.connect(src, timeout=30)
        cur = con.cursor()
        cur.execute("SELECT itemID FROM items WHERE key=?", (att_key,))
        row = cur.fetchone()
        if not row:
            con.close()
            return {"ok": False, "error": "本地库尚无该附件条目（客户端还没同步下来）",
                    "backup": bak}
        item_id = row[0]
        cur.execute("UPDATE itemAttachments SET path=?, syncState=?, storageModTime=?, "
                    "storageHash=? WHERE itemID=?",
                    ("storage:" + filename, SYNC_IN_SYNC, mtime_ms, md5, item_id))
        n = cur.rowcount
        con.commit()
        try:
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        con.close()
        return {"ok": n > 0, "rows": n, "backup": bak, "itemID": item_id}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "backup": bak}


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def doctor(conf=None):
    conf = conf or load_conf()
    home = zotero_home(conf)
    prof = profile_dir()
    dd = data_dir(conf)
    sd = storage_dir(conf)
    w = webdav_conf(conf)
    out = {
        "zotero_home": home,
        "profile": prof,
        "data_dir": dd,
        "storage_dir": sd,
        "storage_entries": len(os.listdir(sd)) if sd and os.path.isdir(sd) else 0,
        "db": db_path(conf),
        "running": zotero_running(),
        "webdav": {"protocol": w["protocol"], "url": w["url"], "user": w["user"]},
    }
    pw, src = webdav_password(conf)
    out["webdav"]["credential_ok"] = bool(pw)
    out["webdav"]["credential_source"] = src
    if pw:
        try:
            out["webdav"]["base"] = dav_base(conf)
            ensure_dav_folder(conf)
            s, _ = dav_request("PROPFIND", "zotero/", b"", ctype="application/xml",
                               conf=conf, depth="0")
            out["webdav"]["reachable"] = s in (207, 200)
        except Exception as e:
            out["webdav"]["reachable"] = False
            out["webdav"]["error"] = str(e)[:200]
    problems = []
    if not home:
        problems.append("未找到 Zotero 安装目录（可在 ~/.zotero/config.yaml 设 zotero_home）")
    if not dd:
        problems.append("未解析到 Zotero 数据目录")
    if w["protocol"] != "webdav":
        problems.append("文件同步不是 WebDAV（protocol=%s），本 skill 的云端同步仅支持 WebDAV"
                        % w["protocol"])
    if not pw:
        problems.append("无法取得 WebDAV 密码（%s）；可在 config.yaml 写 webdav_password" % src)
    out["ok"] = not problems
    out["problems"] = problems
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("creds")
    for name in ("upload", "verify", "align"):
        sp = sub.add_parser(name)
        sp.add_argument("--key", required=True)
        if name != "verify":
            sp.add_argument("--pdf", required=True)
        if name == "verify":
            sp.add_argument("--md5", default=None)
        if name == "align":
            sp.add_argument("--filename", default=None)
    sub.add_parser("close")
    sub.add_parser("start")
    a = p.parse_args()

    try:
        if a.cmd == "doctor":
            out = doctor()
        elif a.cmd == "creds":
            pw, src = webdav_password()
            out = {"ok": bool(pw), "source": src,
                   "user": webdav_conf()["user"], "base": dav_base()}
        elif a.cmd == "upload":
            ensure_dav_folder()
            out = upload_attachment(a.key, a.pdf)
            if out.get("ok"):
                out["verify"] = verify_remote(a.key, out["md5"])
                out["ok"] = out["verify"].get("ok", False)
        elif a.cmd == "verify":
            out = verify_remote(a.key, a.md5)
        elif a.cmd == "align":
            fn = a.filename or os.path.basename(a.pdf)
            data_md5 = md5_file(a.pdf)
            mtime = int(os.path.getmtime(a.pdf) * 1000)
            out = align_local(a.key, fn, data_md5, mtime)
        elif a.cmd == "close":
            out = {"ok": close_zotero()}
        else:
            out = {"ok": start_zotero()}
    except Exception as e:
        out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
