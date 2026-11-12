# -*- coding: utf-8 -*-
r"""
BAN TIN CHUNG KHOAN - server Python chay tren may (khong qua cong trung gian cong khai)
--------------------------------------------------------------------------------------
Chay:   bam dup chay_ban_tin.bat   (hoac: python bantin_server.py)
        -> tu mo trinh duyet tai http://127.0.0.1:8765/
Can cai 1 lan:  pip install requests pymupdf   (pymupdf de cat anh do thi VCBS)

Trang ban tin hoi server nay thay vi hoi allorigins/corsproxy...:
  /get?url=...   Python tai thang trang nguon (khong bi CORS chan), tra lai cho trang
  /hanghoa       Python tai TOAN BO bieu do hang hoa song song (16 luong) va tra 1 lan
  /ping          kiem tra server dang chay
Server chi nghe 127.0.0.1 (may khac trong mang khong vao duoc).
Mo 4 cong 8765-8768 de trinh duyet tai duoc 24 yeu cau cung luc (moi cong toi da 6).
"""
import os, re, sys, json, time, threading, webbrowser, zipfile, io, socket
import datetime as dt
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    print("Thieu thu vien requests. Mo cmd va chay:  pip install requests")
    input("Bam Enter de thoat...")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
PORTS = [8765, 8766, 8767, 8768]
PAGE = "ban_tin_chung_khoan.html"
CACHE_DIR = os.path.join(HERE, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
SES = requests.Session()
SES.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "vi,en;q=0.8"})
_ad = HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0)
SES.mount("https://", _ad); SES.mount("http://", _ad)
TIMEOUT = (4, 9)           # (ket noi, doc) giay - nguon cham qua thi bo qua cho nhanh


def khongdau(t):
    import unicodedata
    t = str(t).replace("đ", "d").replace("Đ", "D")
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()


def log(msg):
    print(time.strftime("%H:%M:%S "), msg, flush=True)


def referer_for(url):
    h = urlparse(url).netloc
    if "sina.com.cn" in h:
        return "https://finance.sina.com.cn/"
    if "vndirect" in h:
        return "https://dstock.vndirect.com.vn/"
    return None


def http_get(url, timeout=TIMEOUT):
    hd = {}
    ref = referer_for(url)
    if ref:
        hd["Referer"] = ref
    return SES.get(url, headers=hd, timeout=timeout)


# --------------------------------------------------------------- /get: tai thang
_MEM = {}                 # url -> (luc, status, ctype, bytes)
_MEM_LOCK = threading.Lock()
MEM_TTL = 300             # giay: trong 5 phut khong tai lai cung 1 trang


def relay(url):
    now = time.time()
    with _MEM_LOCK:
        c = _MEM.get(url)
        if c and now - c[0] < MEM_TTL:
            return c[1:]
    r = http_get(url)
    out = (r.status_code, r.headers.get("Content-Type", "application/octet-stream"), r.content)
    if r.status_code == 200:
        with _MEM_LOCK:
            _MEM[url] = (now,) + out
            if len(_MEM) > 400:
                for k in sorted(_MEM, key=lambda k: _MEM[k][0])[:100]:
                    _MEM.pop(k, None)
    return out


# --------------------------------------------------------------- bieu do hang hoa
def since2y():
    return (dt.date.today() - dt.timedelta(days=735)).isoformat()


def cut(pts, mn=10):
    f = since2y()
    pts = [p for p in pts if p[0] >= f and p[1] is not None and p[1] == p[1] and p[1] > 0]
    if len(pts) < mn:
        raise RuntimeError("thieu du lieu (%d diem)" % len(pts))
    return pts


def jsonp(t):
    a, b = t.find("["), t.rfind("]")
    if a < 0 or b < a:
        raise RuntimeError("khong co du lieu")
    return json.loads(t[a:b + 1])


def f_yahoo(sym):
    p1 = int(time.mktime(dt.date.fromisoformat(since2y()).timetuple()))
    p2 = int(time.time()) + 86400
    err = None
    for host in ("query1", "query2"):
        try:
            r = http_get("https://%s.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d"
                         "&interval=1d&includePrePost=false&events=history" % (host, quote(sym, safe=""), p1, p2))
            if r.status_code != 200:
                err = "Yahoo HTTP %d" % r.status_code
                continue
            res = r.json()["chart"]["result"][0]
            off = (res.get("meta") or {}).get("gmtoffset") or 0
            ts = res.get("timestamp") or []
            cl = res["indicators"]["quote"][0].get("close") or []
            pts = [[dt.datetime.utcfromtimestamp(t + off).strftime("%Y-%m-%d"), c]
                   for t, c in zip(ts, cl) if c is not None]
            return cut(pts, 20)
        except Exception as e:
            err = str(e)[:80]
    raise RuntimeError(err or "Yahoo loi")


def f_stooq(sym):
    r = http_get("https://stooq.com/q/d/l/?s=%s&i=d&d1=%s&d2=%s" %
                 (sym, since2y().replace("-", ""), dt.date.today().strftime("%Y%m%d")))
    L = r.text.strip().splitlines()
    if len(L) < 4 or not L[0].lower().startswith("date,"):
        raise RuntimeError("Stooq: " + r.text[:50].replace("\n", " "))
    pts = []
    for l in L[1:]:
        c = l.split(",")
        try:
            pts.append([c[0], float(c[4])])
        except Exception:
            pass
    return cut(pts, 20)


def f_sinaI(sym):
    d = dt.date.today()
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%%20_%s=/"
           "InnerFuturesNewService.getDailyKLine?symbol=%s&type=%s" % (sym, sym, d.strftime("%Y_%m_%d")))
    rows = jsonp(http_get(url).text)
    return cut([[str(x.get("d") or x.get("date"))[:10], float(x.get("c") or x.get("close"))] for x in rows])


def f_sinaG(sym):
    d = dt.date.today()
    tag = "%d_%d_%d" % (d.year, d.month, d.day)
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%%20_%s%s=/"
           "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=%s&_=%s&source=web" % (sym, tag, sym, tag))
    rows = jsonp(http_get(url).text)
    return cut([[str(x.get("date") or x.get("d"))[:10], float(x.get("close") or x.get("c"))] for x in rows])


def f_fred(sid):
    r = http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (sid, since2y()))
    L = r.text.strip().splitlines()
    if len(L) < 4 or "," not in L[0]:
        raise RuntimeError("FRED CSV loi")
    pts = []
    for l in L[1:]:
        c = l.split(",")
        try:
            pts.append([c[0], float(c[1])])
        except Exception:
            pass
    if len(pts) < 4:
        raise RuntimeError("FRED rong")
    return pts


# ---- World Bank Pink Sheet: tai 1 lan / 24 gio, luu file, doc bang zipfile (khong can pandas)
WB_PAGE = "https://www.worldbank.org/en/research/commodity-markets"
WB_FALLBACK = ("https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/"
               "related/CMO-Historical-Data-Monthly.xlsx")
WB_FILE = os.path.join(CACHE_DIR, "CMO-Historical-Data-Monthly.xlsx")
_WB = {}
_WB_LOCK = threading.Lock()
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _wb_download():
    urls = [WB_FALLBACK]
    try:
        m = re.search(r'https?://[^"\'\s]+CMO-Historical-Data-Monthly\.xlsx', http_get(WB_PAGE).text)
        if m:
            urls.insert(0, m.group(0))
    except Exception:
        pass
    for u in urls:
        try:
            r = http_get(u, timeout=(5, 60))
            if r.status_code == 200 and len(r.content) > 2000:
                with open(WB_FILE, "wb") as f:
                    f.write(r.content)
                return
        except Exception:
            pass
    raise RuntimeError("khong tai duoc file World Bank")


def wb_table():
    with _WB_LOCK:
        if _WB.get("fail") and time.time() - _WB["fail"] < 600 and not os.path.exists(WB_FILE):
            raise RuntimeError("World Bank vua loi, 10 phut sau thu lai")
        fresh = os.path.exists(WB_FILE) and time.time() - os.path.getmtime(WB_FILE) < 24 * 3600
        if "t" in _WB and fresh:
            return _WB["t"]
        if not fresh:
            try:
                _wb_download()
            except Exception:
                _WB["fail"] = time.time()
                if not os.path.exists(WB_FILE):
                    raise
        z = zipfile.ZipFile(WB_FILE)
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        sh = next(s for s in wb.iter("{%s}sheet" % NS["m"]) if "monthly prices" in s.get("name", "").lower())
        tgt = next(r.get("Target") for r in rels if r.get("Id") == sh.get(RID))
        path = "xl/" + re.sub(r"^/?xl/", "", tgt)
        strs = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
                strs.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
        rows = []
        for row in ET.fromstring(z.read(path)).iter("{%s}row" % NS["m"]):
            arr = {}
            for c in row.findall("m:c", NS):
                ref = re.match(r"[A-Z]+", c.get("r")).group(0)
                ci = 0
                for ch in ref:
                    ci = ci * 26 + ord(ch) - 64
                v = c.find("m:v", NS)
                val = v.text if v is not None else "".join(t.text or "" for t in c.iter("{%s}t" % NS["m"]))
                if c.get("t") == "s" and val is not None:
                    val = strs[int(val)]
                arr[ci - 1] = val
            rows.append(arr)
        hdr = next(i for i, r in enumerate(rows[:20])
                   if any(str(v or "").strip().startswith("Crude oil") for v in r.values()))
        names = {k: re.sub(r"\*", "", str(v or "")).strip().lower() for k, v in rows[hdr].items()}
        body = [r for r in rows[hdr + 1:] if re.match(r"^\d{4}M\d{2}$", str(r.get(0, "")))]
        _WB["t"] = (names, body)
        return _WB["t"]


def f_wb(col):
    names, body = wb_table()
    key = col.lower()
    ci = next((k for k, n in names.items() if n.startswith(key)), None)
    if ci is None:
        raise RuntimeError("World Bank khong co cot '%s'" % col)
    f = since2y()[:7]
    pts = []
    for r in body:
        m = str(r[0])
        try:
            v = float(r.get(ci))
        except Exception:
            continue
        d = m[:4] + "-" + m[5:7] + "-01"
        if d[:7] >= f:
            pts.append([d, v])
    if len(pts) < 3:
        raise RuntimeError("World Bank thieu du lieu")
    return pts


FETCH = {"yahoo": f_yahoo, "stooq": f_stooq, "sinaI": f_sinaI, "sinaG": f_sinaG, "wb": f_wb, "fred": f_fred}
NGUON_VN = {"sinaG": "Sàn quốc tế (Sina)", "sinaI": "Sàn TQ (Sina)", "stooq": "Stooq", "yahoo": "Yahoo",
            "wb": "World Bank", "fred": "FRED"}

HH_FILE = os.path.join(CACHE_DIR, "hanghoa_cache.json")
_HH_LOCK = threading.Lock()
try:
    HH = json.load(open(HH_FILE, encoding="utf-8"))
except Exception:
    HH = {"series": {}, "pref": {}}


def save_hh():
    with _HH_LOCK:
        tmp = HH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(HH, f, ensure_ascii=False)
        os.replace(tmp, HH_FILE)


def load_item(item, force):
    ten, donvi, ma, srcs = item[0], item[1], item[2], item[3]
    old = HH["series"].get(ten)
    if old and not force and time.time() * 1000 - old["at"] < (24 if old.get("thang") else 6) * 3600e3:
        return old, None
    order = list(range(len(srcs)))
    pref = HH["pref"].get(ten)
    if isinstance(pref, int) and pref < len(srcs):
        order.remove(pref)
        order.insert(0, pref)
    errs = []
    for i in order:
        s = srcs[i]
        typ, sym = s[0], s[1]
        mult = s[2] if len(s) > 2 and isinstance(s[2], (int, float)) else 1
        if typ not in FETCH:
            continue
        try:
            pts = FETCH[typ](sym)
            if len(pts) < 3:
                raise RuntimeError("qua it du lieu")
            out = {"at": int(time.time() * 1000), "ten": ten, "donvi": donvi, "ma": ma, "nguon": NGUON_VN[typ],
                   "thang": len(pts) < 60, "pts": [[p[0], round(p[1] * mult, 4)] for p in pts]}
            with _HH_LOCK:
                HH["series"][ten] = out
                HH["pref"][ten] = i
            log("  OK  %-32s %-6s %-14s %d diem" % (khongdau(ten), typ, sym, len(pts)))
            return out, None
        except Exception as e:
            errs.append("%s %s: %s" % (typ, sym, str(e)[:60]))
    log("  LOI %-32s %s" % (khongdau(ten), " | ".join(errs)))
    return (old, "; ".join(errs)) if old else (None, "; ".join(errs))


POOL = ThreadPoolExecutor(max_workers=16)


def hanghoa(items, force):
    t0 = time.time()
    log("Quet %d bieu do hang hoa%s..." % (len(items), " (bat buoc tai moi)" if force else ""))
    res = list(POOL.map(lambda it: load_item(it, force), items))
    save_hh()
    series, miss = [], []
    for it, (s, err) in zip(items, res):
        if s:
            series.append(s)
            if err:
                miss.append(it[0] + " (giu ban cu)")
        else:
            miss.append(it[0])
    log("Xong hang hoa: %d/%d trong %.1f giay" % (len(series), len(items), time.time() - t0))
    return {"series": series, "miss": miss}


# --------------------------------------------------------------- anh do thi VCBS
# Lay file PDF "Ban tin thi truong" cua VCBS roi cat ra anh do thi lon nhat (anh TradingView).
# Can 1 trong 2 thu vien:  pip install pymupdf   (tot nhat)   hoac   pip install pypdf pillow
# VCBS bat nhap email khi tai PDF. Muon lay anh goc VCBS thi dien email CUA BAN vao day (de trong = khong tai, dung do thi tu ve).
VCBS_EMAIL = ""


def vcbs_email():
    """Email de tai PDF VCBS. Thu tu: bien moi truong VCBS_EMAIL (may chu GitHub: Settings > Secrets > Actions > VCBS_EMAIL,
    khong lo ra ngoai du kho Public) -> dong "VCBS_EMAIL = ..." trong dien_thoai_online.txt -> hang so VCBS_EMAIL o tren."""
    e = (os.environ.get("VCBS_EMAIL") or "").strip()
    if not e:
        try:
            e = (_doc_cfg(ONLINE_CFG).get("VCBS_EMAIL") or "").strip()
        except Exception:
            e = ""
    return e or VCBS_EMAIL.strip()
VCBS_API = "https://www.vcbs.com.vn/api/v1/"
VCBS_HDR = {"Referer": "https://www.vcbs.com.vn/trung-tam-phan-tich/bao-cao-phan-tich", "Origin": "https://www.vcbs.com.vn",
            "Accept": "application/json, text/plain, */*"}


def _find_url(o):
    """tim link PDF trong JSON tra ve"""
    if isinstance(o, str):
        return o if re.match(r"https?://", o) and (".pdf" in o.lower() or "download" in o.lower() or "storage" in o.lower()) else None
    if isinstance(o, dict):
        for k in ("url", "link", "path", "downloadUrl", "fileUrl", "data"):
            if k in o:
                u = _find_url(o[k])
                if u:
                    return u
        for v in o.values():
            u = _find_url(v)
            if u:
                return u
    if isinstance(o, list):
        for v in o:
            u = _find_url(v)
            if u:
                return u
    return None


def vcbs_pdf(rid, fname, ngay=""):
    """PDF ban tin VCBS. VCBS de file o dia chi co dinh, tai tu do (khong can email):
    https://www.vcbs.com.vn/storage/ttpt_reports/<YYYYMMDD ngay dang>/<ten file>"""
    errs = []
    fname = os.path.basename(str(fname or ""))
    if fname.lower().endswith(".pdf"):
        days = []
        for d in re.findall(r"\d{8}", str(ngay or "")):
            try:
                d0 = dt.datetime.strptime(d, "%Y%m%d")
            except ValueError:
                continue
            for k in (0, -1, 1):
                x = (d0 + dt.timedelta(days=k)).strftime("%Y%m%d")
                if x not in days:
                    days.append(x)
        if not days:   # khong biet ngay: doan tu ten file "vcbs-12-11-2026-..." roi toi hom nay
            m = re.match(r"vcbs-(\d{1,2})-(\d{1,2})-(\d{4})", fname)
            if m:
                days.append("%s%02d%02d" % (m.group(3), int(m.group(2)), int(m.group(1))))
            days.append(dt.datetime.now().strftime("%Y%m%d"))
        for d in days:
            for host in ("https://www.vcbs.com.vn", "https://api.vcbs.com.vn"):
                u = "%s/storage/ttpt_reports/%s/%s" % (host, d, fname)
                try:
                    f = SES.get(u, headers=VCBS_HDR, timeout=(5, 40))
                    if f.status_code == 200 and f.content[:4] == b"%PDF":
                        return f.content
                    errs.append("%s: HTTP %d" % (d, f.status_code))
                except Exception as e:
                    errs.append("%s: %s" % (d, str(e)[:50]))
    if not vcbs_email():
        raise RuntimeError("khong tai duoc PDF VCBS (" + "; ".join(errs[:3]) + ")")
    # 1) API tao link tai (trang VCBS goi khi bam "Tai xuong")
    em = vcbs_email()
    for body in ({"id": rid, "email": em}, {"reportId": rid, "email": em}, {"ttptReportId": rid, "email": em}, {"ids": [rid], "email": em}):
        try:
            r = SES.post(VCBS_API + "ttpt-reports/create-download", json=body, headers=VCBS_HDR, timeout=(4, 8))
            if r.status_code == 422:   # VCBS bao thieu truong nao -> ghi lai de sua
                log("VCBS create-download %s -> 422: %s" % (list(body)[0], r.text[:300].replace("\n", " ")))
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                return r.content
            u = _find_url(r.json()) if r.status_code == 200 else None
            if u:
                u = u if u.startswith("http") else "https://api.vcbs.com.vn" + u
                f = SES.get(u, headers=VCBS_HDR, timeout=(5, 60))
                if f.content[:4] == b"%PDF":
                    return f.content
            errs.append("create-download %s: HTTP %d" % (list(body)[0], r.status_code))
        except Exception as e:
            errs.append("create-download: %s" % str(e)[:60])
    # 2) doan duong dan file tinh
    for u in ("https://api.vcbs.com.vn/storage/upload/ttpt-reports/%s" % fname, "https://api.vcbs.com.vn/storage/ttpt-reports/%s" % fname,
              "https://api.vcbs.com.vn/storage/upload/media/%s" % fname, "https://www.vcbs.com.vn/api/v1/ttpt-reports/%s/download" % rid):
        try:
            f = SES.get(u, headers=VCBS_HDR, timeout=(4, 10))
            if f.status_code == 200 and f.content[:4] == b"%PDF":
                return f.content
        except Exception:
            pass
    raise RuntimeError("khong tai duoc PDF VCBS (" + "; ".join(errs[:2]) + ")")


def biggest_image(pdf):
    """anh raster lon nhat, ngang (khong lay logo) -> PNG bytes"""
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        doc = fitz.open(stream=pdf, filetype="pdf")
        best = None
        for pno in range(min(4, len(doc))):
            for im in doc[pno].get_images(full=True):
                x = doc.extract_image(im[0])
                w, h = x.get("width", 0), x.get("height", 0)
                if w > h * 1.2 and w >= 400 and (not best or w * h > best[0]):
                    best = (w * h, im[0], pno)
        if best:
            pix = fitz.Pixmap(doc, best[1])
            if pix.n - pix.alpha >= 4:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            return pix.tobytes("png")
        # do thi la hinh ve (khong phai anh): chup nua tren trang co chu "VNINDEX"
        for pno in range(min(4, len(doc))):
            pg = doc[pno]
            hit = pg.search_for("VNINDEX")
            if hit:
                r = pg.rect
                clip = fitz.Rect(r.x0, max(r.y0, hit[0].y0 - 10), r.x1, min(r.y1, hit[0].y0 + r.height * 0.42))
                return pg.get_pixmap(dpi=170, clip=clip).tobytes("png")
        raise RuntimeError("PDF khong co anh do thi")
    except ImportError:
        pass
    try:
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(pdf))
        best = None
        for pg in rd.pages[:4]:
            for im in pg.images:
                img = im.image
                w, h = img.size
                if w > h * 1.2 and w >= 400 and (not best or w * h > best[0]):
                    best = (w * h, img)
        if not best:
            raise RuntimeError("PDF khong co anh do thi")
        out = io.BytesIO()
        best[1].convert("RGB").save(out, "PNG")
        return out.getvalue()
    except ImportError:
        raise RuntimeError("chua cai thu vien doc PDF: pip install pymupdf")


VCBS_FAIL = os.path.join(CACHE_DIR, "vcbs_loi.json")   # ban tin nao tai loi thi 12 gio sau moi thu lai


_VCBS_LOCK = threading.Lock()


def vcbs_pdf_nho(rid, fname, ngay=""):
    """Tai PDF 1 lan / ban tin (luu cache/vcbs_<so>.pdf) - anh do thi va phan chu dung chung.
    Loi thi 15 phut sau moi thu lai."""
    fp = os.path.join(CACHE_DIR, "vcbs_%s.pdf" % rid)
    with _VCBS_LOCK:
        if os.path.exists(fp):
            return open(fp, "rb").read()
        try:
            fail = json.load(open(VCBS_FAIL, encoding="utf-8"))
        except Exception:
            fail = {}
        f0 = fail.get(rid, {})
        if time.time() - f0.get("at", 0) < 15 * 60:
            raise RuntimeError("vua loi, 15 phut sau moi thu lai: " + f0.get("loi", ""))
        try:
            pdf = vcbs_pdf(rid, fname, ngay)
        except Exception as e:
            fail[rid] = {"at": time.time(), "loi": str(e)[:200]}
            try:
                json.dump(fail, open(VCBS_FAIL, "w", encoding="utf-8"))
            except Exception:
                pass
            raise
        with open(fp, "wb") as f:
            f.write(pdf)
        return pdf


def vcbs_chart(rid, fname):
    rid = re.sub(r"\D", "", str(rid))
    fp = os.path.join(CACHE_DIR, "vcbs_%s.png" % rid)
    if os.path.exists(fp):
        return open(fp, "rb").read()
    png = biggest_image(vcbs_pdf_nho(rid, fname))
    with open(fp, "wb") as f:
        f.write(png)
    log("Lay anh do thi VCBS ban tin %s: %d KB" % (rid, len(png) // 1024))
    return png


# ---- Phan chu nhan dinh trong PDF (VCBS tu 9/11/2026 chi dang PDF, khong con phan chu tren web)
# Bo cuc ban tin: trang 1 cot trai = bang thong ke, phai = tieu de, "Tin tuc noi bat", "Dien bien giao dich trong phien";
# trang 2 = "Phan tich ki thuat", "Chien luoc giao dich"; trang cuoi = "DIEU KHOAN SU DUNG".
ND_MUC = re.compile(r"^\s*(Diễn biến giao dịch(?: trong phiên)?|Diễn biến thị trường|Phân tích k[ĩỹ] thuật|Chiến lược giao dịch|Chiến lược đầu tư|"
                    r"Khuyến nghị(?: đầu tư)?|Nhận định thị trường|Triển vọng(?: thị trường)?|Tin tức nổi bật|Tin tức)\s*:?\s*(.*)$", re.I)
ND_BO_MUC = re.compile(r"^Tin tức", re.I)                     # muc tin tuc: khong lay (giong ban tren web)
ND_HET = re.compile(r"^\s*(ĐIỀU KHOẢN SỬ DỤNG|KHUYẾN CÁO|TUYÊN BỐ MIỄN TRÁCH|Báo cáo này và/hoặc)", re.I)
ND_RAC = re.compile(r"^(BẢN TIN THỊ TRƯỜNG.*|vcbs\.com\.vn\s*\|\s*\d+|[\d\s.,%+\-=()/]+|(?:[A-Z][A-Z0-9]{1,4}\s*){1,8}|Top cổ phiếu.*|"
                    r"Chuyên viên Phân tích|Bloomberg:.*|https?://\S+|\S+@\S+.*)$")


def _pdf_lines(pdf):
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            fitz = None
    if fitz:
        doc = fitz.open(stream=pdf, filetype="pdf")
        out = []
        for pg in doc:
            out += pg.get_text("text").splitlines()
        return out
    try:
        from pypdf import PdfReader
        import io as _io
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(_io.BytesIO(pdf)).pages).splitlines()
    except ImportError:
        raise RuntimeError("chua cai thu vien doc PDF: pip install pymupdf")


def pdf_blocks(pdf):
    """PDF ban tin -> [{t, kieu: 'h'|'li'|'p'}] tu 'Dien bien giao dich' toi truoc 'Dieu khoan su dung'."""
    out, cur, muc, bo = [], None, False, False
    def xong():
        if cur and len(cur["t"]) > 15:
            out.append(cur)
    for raw in _pdf_lines(pdf):
        t = re.sub(r"\s+", " ", raw.replace(" ", " ")).strip()
        if not t:
            continue
        if ND_HET.match(t):
            break
        m = ND_MUC.match(t)
        if m and len(t) < 60 + len(m.group(2)) and (t.rstrip().endswith(":") or not m.group(2) or len(m.group(1)) > 12):
            xong(); cur = None
            bo = bool(ND_BO_MUC.match(m.group(1)))
            muc = muc or not bo
            if not bo:
                out.append({"t": m.group(1).strip() + ":", "kieu": "h"})
                if len(m.group(2).strip()) > 3:
                    cur = {"t": m.group(2).strip(), "kieu": "p"}
            continue
        if cur and muc and not bo and re.match(r"^[+\-]?\d[\d.]*,\d+\s*%?[.;]?$", t) and not re.search(r"[.!?:]$", cur["t"]):
            cur["t"] += " " + t           # duoi cau bi xuong dong, vd "... tuong duong" + "0,70%"
            continue
        if not muc or bo or ND_RAC.match(t):
            continue
        b = re.match(r"^[▪•●■◦\-–·]\s*(.*)$", t)
        if b:
            xong(); cur = {"t": b.group(1), "kieu": "li"}
        elif cur is None:
            cur = {"t": t, "kieu": "p"}
        else:
            cur["t"] += ("" if cur["t"].endswith("-") else " ") + t
    xong()
    if sum(len(x["t"]) for x in out if x["kieu"] != "h") < 200:
        raise RuntimeError("khong doc duoc phan chu trong PDF")
    return out[:40]


def vcbs_text(rid, fname, ngay=""):
    rid = re.sub(r"\D", "", str(rid))
    fp = os.path.join(CACHE_DIR, "vcbs_%s.json" % rid)
    if os.path.exists(fp):
        return json.load(open(fp, encoding="utf-8"))
    doan = pdf_blocks(vcbs_pdf_nho(rid, fname, ngay))
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(doan, f, ensure_ascii=False)
    log("Doc phan chu nhan dinh tu PDF VCBS ban tin %s: %d doan" % (rid, len(doan)))
    return doan



# --------------------------------------------------------------- Day ban tin len GitHub Pages
ONLINE_CFG = os.path.join(HERE, "dien_thoai_online.txt")   # giong cong cu Bao cao CTCK: REPO = ..., TOKEN = ...
ONLINE_TEMPLATE = (
    "# TU DONG DUA BAN TIN LEN MANG (GitHub Pages) - may tinh tat van xem duoc tren dien thoai\n"
    "# Moi lan ban tin quet xong se tu day len. Dien 2 dong duoi (xem README.txt muc GITHUB):\n"
    "#   REPO  = <tai khoan GitHub>/<ten kho>\n"
    "#   TOKEN = ma token GitHub (github_pat_...), can quyen Contents: Read and write cho kho nay\n"
    "#   (de trong TOKEN: tu dung chung TOKEN cua cong cu Bao cao CTCK neu tim thay)\n"
    "REPO = tungnt36-maker/bantin\n"
    "TOKEN = \n"
    "# (khong bat buoc) Email de tai PDF ban tin VCBS khi VCBS chua dang phan chu tren web - chi dung tren may tinh nay:\n"
    "VCBS_EMAIL = \n")

def _doc_cfg(path):
    cfg = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    cfg[k.strip().upper()] = v.strip()
    except Exception:
        pass
    return cfg

def _token_bao_cao():
    """Tim TOKEN trong dien_thoai_online.txt cua cong cu Bao cao CTCK (thu muc anh em / Desktop / Downloads)."""
    import glob
    home = os.path.expanduser("~")
    goc = [os.path.dirname(HERE), os.path.dirname(os.path.dirname(HERE)), home]
    for d in ("Desktop", "Downloads", "Documents", "OneDrive", os.path.join("OneDrive", "Desktop"),
              os.path.join("OneDrive", "Documents"), os.path.join("OneDrive", "Downloads")):
        goc.append(os.path.join(home, d))
    goc += [os.path.join(home, d) for d in os.listdir(home) if d.startswith("OneDrive")] if os.path.isdir(home) else []
    da = set()
    for g in goc:
        if not os.path.isdir(g) or g in da:
            continue
        da.add(g)
        for f in (glob.glob(os.path.join(g, "*", "dien_thoai_online*.txt*")) + glob.glob(os.path.join(g, "*", "*", "dien_thoai_online*.txt*"))
                  + (glob.glob(os.path.join(g, "*", "*", "*", "dien_thoai_online*.txt*")) if g != home else [])):
            if os.path.abspath(os.path.dirname(f)) == os.path.abspath(HERE):
                continue
            t = _doc_cfg(f).get("TOKEN", "")
            if t.startswith(("github_pat_", "ghp_")) and "xxxx" not in t:
                return t, f
    return "", ""

def gh_cfg():
    import glob
    path = ONLINE_CFG
    if not os.path.exists(path):
        c = sorted(glob.glob(os.path.join(HERE, "dien_thoai_online*.txt*")), key=os.path.getmtime, reverse=True)
        if c:
            path = c[0]
        else:
            try:
                with open(ONLINE_CFG, "w", encoding="utf-8") as f:
                    f.write(ONLINE_TEMPLATE)
            except Exception:
                pass
    cfg = _doc_cfg(path)
    out = {"repo": cfg.get("REPO", ""), "token": cfg.get("TOKEN", ""), "nguon_token": "dien_thoai_online.txt"}
    if not out["token"] or "xxxx" in out["token"]:
        out["token"], f = _token_bao_cao()
        out["nguon_token"] = f
    if "/" not in out["repo"]:
        out["repo"] = ""
    return out

def gh_push(html, cfg):
    """Day index.html len GitHub va XOA LICH SU CU: moi lan tao 1 commit moi khong co cha
    roi ghi de nhanh main (force) -> kho luon chi co 1 ban, khong phinh theo thoi gian."""
    import base64, requests
    repo = cfg["repo"].strip().strip("/")
    if repo.startswith("http"):
        repo = "/".join(urlparse(repo).path.strip("/").split("/")[:2])
    base = "https://api.github.com/repos/%s" % repo
    hd = {"Authorization": "Bearer " + cfg["token"].strip(), "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ban-tin-chung-khoan"}
    owner, name = repo.split("/")[:2]
    site = "https://%s.github.io/%s/" % (owner.lower(), name)
    branch = cfg.get("branch") or "main"
    content = base64.b64encode(html.encode("utf-8")).decode("ascii")
    msg = "Ban tin " + dt.datetime.now().strftime("%H:%M %d/%m/%Y")

    def chk(r, buoc):
        if r.status_code in (401, 403):
            raise PermissionError("GitHub tu choi TOKEN (%s) - ma het han hoac chua cap quyen Contents: Read and write cho kho %s" % (r.status_code, repo))
        if r.status_code == 404:
            raise PermissionError("Khong thay kho %s, hoac TOKEN chua duoc cap quyen kho nay (sua token tren GitHub: them kho %s)" % (repo, name))
        if not r.ok:
            raise RuntimeError("%s: GitHub tra loi %s %s" % (buoc, r.status_code, r.text[:120]))
        return r.json()

    def day_xoa_lich_su():
        blob = chk(requests.post(base + "/git/blobs", headers=hd, json={"content": content, "encoding": "base64"}, timeout=90), "tao blob")["sha"]
        tree = chk(requests.post(base + "/git/trees", headers=hd, json={"tree": [
            {"path": "index.html", "mode": "100644", "type": "blob", "sha": blob},
            {"path": ".nojekyll", "mode": "100644", "type": "blob", "content": ""}]}, timeout=30), "tao tree")["sha"]
        commit = chk(requests.post(base + "/git/commits", headers=hd, json={"message": msg, "tree": tree, "parents": []}, timeout=30), "tao commit")["sha"]
        r = requests.patch(base + "/git/refs/heads/" + branch, headers=hd, json={"sha": commit, "force": True}, timeout=30)
        if r.status_code in (404, 422):   # chua co nhanh main
            r = requests.post(base + "/git/refs", headers=hd, json={"ref": "refs/heads/" + branch, "sha": commit}, timeout=30)
        chk(r, "ghi de nhanh " + branch)

    def day_thuong():   # kho con trong (chua co commit nao): dung cach thuong de tao commit dau tien
        api = base + "/contents/index.html"
        r = requests.get(api, headers=dict(hd, Accept="application/vnd.github.object+json"), timeout=30)
        body = {"message": msg, "content": content}
        if r.status_code == 200:
            body["sha"] = r.json().get("sha")
        chk(requests.put(api, headers=hd, json=body, timeout=90), "tai index.html")

    # Kho da cai "tu quet tren GitHub" (co file workflow) -> KHONG ghi de kho (se xoa mat code may chu),
    # chi nho GitHub quet lai ngay (can token co quyen Actions: Read and write; khong co thi doi lan quet theo gio)
    try:
        wf = requests.get(base + "/contents/" + WF_PATH, headers=hd, timeout=20)
    except Exception:
        wf = None
    if wf is not None and wf.status_code == 200:
        d = requests.post(base + "/actions/workflows/quet_ban_tin.yml/dispatches", headers=hd, json={"ref": branch}, timeout=20)
        return {"ok": 1, "repo": repo, "site": site, "cloud": 1, "dispatched": d.status_code == 204}

    last = ""
    for lan in range(1, 5):            # thu lai toi da 4 lan nhu cong cu Bao cao
        try:
            try:
                day_xoa_lich_su()
            except RuntimeError as e:
                if "409" not in str(e):          # 409 = kho trong
                    raise
                day_thuong()
            return {"ok": 1, "repo": repo, "site": site}
        except PermissionError as e:
            last = str(e); break
        except Exception as e:
            last = str(e)[:150]
        log("  ! Lan %d chua day len GitHub duoc (%s) - thu lai..." % (lan, last[:90]))
        time.sleep(4 * lan)
    raise RuntimeError(last)

def _ghi_trang_thai(msg):
    try:
        with open(os.path.join(HERE, "trang_thai_dua_len_mang.txt"), "w", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

CLOUD = bool(os.environ.get("BT_CLOUD"))   # dang chay tren may chu GitHub Actions (quet_github.py)
WF_PATH = ".github/workflows/quet_ban_tin.yml"
GH_LOGGED = set()

def gh_trang_thai():
    """In ra cua so den: trang GitHub dang co ban tin luc nao."""
    cfg = gh_cfg()
    if not cfg.get("repo"):
        print(" GITHUB: chua cai dat - mo file dien_thoai_online.txt (canh chay_ban_tin.bat) dien REPO / TOKEN.")
        return
    if not cfg.get("token"):
        print(" GITHUB: kho %s - chua co TOKEN: dien vao dien_thoai_online.txt (xem README muc GITHUB)." % cfg["repo"])
        return
    c = gh_check("")
    if c.get("ver_web"):
        t = dt.datetime.fromtimestamp(int(c["ver_web"]) / 1000)
        cu = (dt.datetime.now() - t).total_seconds() / 3600
        print(" GITHUB: %s dang co ban tin luc %s%s" % (c["site"], t.strftime("%H:%M %d/%m/%Y"), "  (cu hon %d gio)" % cu if cu >= 12 else ""))
    else:
        print(" GITHUB: %s chua co ban tin (%s)%s" % (c["site"], c.get("code") or ("loi mang: " + c.get("err", ""))[:70], " - kiem tra Settings > Pages" if c.get("code") == 404 else ""))
    print(" Moi lan ban tin quet xong se TU DONG day len trang nay.%s" % ("" if cfg["nguon_token"] == "dien_thoai_online.txt" else " (dung chung TOKEN cua Bao cao CTCK)"))

def gh_check(ver):
    """Kiem tra trang github.io da hien ban moi (co dau bt-ver) chua."""
    import requests
    cfg = gh_cfg()
    if not cfg.get("repo"):
        return {"live": False, "err": "chua_cau_hinh"}
    repo = cfg["repo"].strip().strip("/")
    if repo.startswith("http"):
        repo = "/".join(urlparse(repo).path.strip("/").split("/")[:2])
    owner, name = repo.split("/")[:2]
    site = "https://%s.github.io/%s/" % (owner.lower(), name)
    out = {"site": site, "live": False, "code": 0, "ver_web": ""}
    try:
        r = requests.get(site, params={"t": int(time.time())}, headers={"Cache-Control": "no-cache", "User-Agent": "ban-tin-chung-khoan"}, timeout=15)
        out["code"] = r.status_code
        m = re.search(r'name="bt-ver" content="(\d+)"', r.text)
        out["ver_web"] = m.group(1) if m else ""
        out["live"] = r.ok and bool(m) and (not ver or int(m.group(1)) >= int(ver))
        if out["live"] and ver and ver not in GH_LOGGED:
            GH_LOGGED.add(ver)
            log("DA LEN GITHUB OK - ban tin luc %s da hien tren %s" % (dt.datetime.fromtimestamp(int(ver) / 1000).strftime("%H:%M %d/%m"), site))
    except Exception as e:
        out["err"] = str(e)
    try:   # trang thai dung trang (can token co quyen Pages: read; khong co thi bo qua)
        hd = {"Authorization": "Bearer " + cfg.get("token", ""), "Accept": "application/vnd.github+json", "User-Agent": "ban-tin-chung-khoan"}
        b = requests.get("https://api.github.com/repos/%s/pages/builds/latest" % repo, headers=hd, timeout=10)
        if b.ok:
            out["build"] = b.json().get("status", "")
    except Exception:
        pass
    return out

# --------------------------------------------------------------- HTTP server
class H(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)

    def log_message(self, *a):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Expose-Headers", "X-Upstream-Status")

    def end_headers(self):
        self.cors()
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_body(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_body(204, "text/plain", b"")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/vcbs_chart":
            q = parse_qs(u.query)
            try:
                png = vcbs_chart((q.get("id") or [""])[0], (q.get("file") or [""])[0])
                return self.send_body(200, "image/png", png)
            except Exception as e:
                log("Anh do thi VCBS loi: %s" % e)
                return self.send_body(502, "text/plain; charset=utf-8", str(e).encode("utf-8"))
        if u.path == "/vcbs_text":
            q = parse_qs(u.query)
            try:
                doan = vcbs_text((q.get("id") or [""])[0], (q.get("file") or [""])[0], (q.get("ngay") or [""])[0])
                return self.send_body(200, "application/json; charset=utf-8", json.dumps({"doan": doan}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                log("Phan chu PDF VCBS loi: %s" % e)
                return self.send_body(200, "application/json; charset=utf-8", json.dumps({"err": str(e)[:200]}, ensure_ascii=False).encode("utf-8"))
        if u.path == "/github_check":
            ver = (parse_qs(u.query).get("ver") or [""])[0]
            return self.send_body(200, "application/json; charset=utf-8", json.dumps(gh_check(ver)).encode("utf-8"))
        if u.path == "/ping":
            return self.send_body(200, "application/json", b'{"ok":1,"ports":%s}' % json.dumps(PORTS).encode())
        if u.path == "/get":
            url = (parse_qs(u.query).get("url") or [""])[0]
            p = urlparse(url)
            if p.scheme not in ("http", "https") or p.hostname in ("127.0.0.1", "localhost", None):
                return self.send_body(400, "text/plain", b"url khong hop le")
            try:
                code, ctype, body = relay(url)
            except Exception as e:
                return self.send_body(502, "text/plain; charset=utf-8", ("loi tai: %s" % e).encode("utf-8"),
                                      {"X-Upstream-Status": "0"})
            return self.send_body(200 if code == 200 else 502, ctype, body, {"X-Upstream-Status": str(code)})
        if u.path in ("/", ""):
            self.path = "/" + PAGE
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/github_push":
            if CLOUD:
                return self.send_body(409, "application/json", b'{"err":"cloud"}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                cfg = gh_cfg()
                if not cfg.get("token") or not cfg.get("repo"):
                    log("Chua day len GitHub: mo file dien_thoai_online.txt dien REPO / TOKEN")
                    return self.send_body(409, "application/json", b'{"err":"chua_cau_hinh"}')
                log("Dang day ban tin len GitHub (%s)..." % cfg["repo"])
                out = gh_push(req.get("html") or "", cfg)
                if out.get("cloud"):
                    log(("Kho dang TU QUET TREN GITHUB: da nho GitHub quet lai ngay (~3-5 phut sau len trang %s)" if out["dispatched"] else
                         "Kho dang TU QUET TREN GITHUB moi gio - may tinh khong can day (muon nho quet ngay: cap token quyen Actions: Read and write). Trang: %s") % out["site"])
                    return self.send_body(200, "application/json; charset=utf-8", json.dumps(out).encode("utf-8"))
                log("Da gui len GitHub - dang cho trang %s cap nhat (~1 phut)..." % out["site"])
                _ghi_trang_thai("Da gui len mang luc %s: %s" % (dt.datetime.now().strftime("%H:%M %d/%m/%Y"), out["site"]))
                return self.send_body(200, "application/json; charset=utf-8", json.dumps(out).encode("utf-8"))
            except Exception as e:
                log("  ! KHONG day len GitHub duoc: %s  -> lan quet sau se tu day lai." % e)
                _ghi_trang_thai("LOI luc %s: %s" % (dt.datetime.now().strftime("%H:%M %d/%m/%Y"), e))
                return self.send_body(502, "application/json; charset=utf-8", json.dumps({"err": str(e)}, ensure_ascii=False).encode("utf-8"))
        if u.path != "/hanghoa":
            return self.send_body(404, "text/plain", b"")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            out = hanghoa(req.get("items") or [], bool(req.get("force")))
            return self.send_body(200, "application/json; charset=utf-8", json.dumps(out, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            return self.send_body(500, "text/plain; charset=utf-8", str(e).encode("utf-8"))


def open_browser(url):
    """Mo bang Google Chrome neu co, khong thi dung trinh duyet mac dinh."""
    import subprocess, shutil
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        try:
            import winreg
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    k = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
                    cands.insert(0, winreg.QueryValue(k, None))
                except OSError:
                    pass
        except ImportError:
            pass
    elif sys.platform == "darwin":
        cands.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        cands += [shutil.which("google-chrome") or "", shutil.which("chromium") or ""]
    for c in cands:
        if c and os.path.exists(c):
            try:
                subprocess.Popen([c, url])
                return
            except Exception:
                pass
    webbrowser.open(url)


def port_busy(p):
    s = socket.socket()
    try:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", p)) == 0
    finally:
        s.close()


LAN = "--lan" in sys.argv   # che do dien thoai: cho may khac trong cung Wi-Fi mo ban tin


def lan_ips():
    ips = []
    try:
        s0 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s0.connect(("8.8.8.8", 80))
        ips.append(s0.getsockname()[0])
        s0.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return [ip for ip in ips if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", ip)]


def main():
    url = "http://127.0.0.1:%d/" % PORTS[0]
    if port_busy(PORTS[0]):
        print("Server da chay san, mo trinh duyet:", url)
        if "--no-browser" not in sys.argv:
            open_browser(url)
        return
    servers = []
    for p in PORTS:
        try:
            sv = ThreadingHTTPServer(("0.0.0.0" if LAN else "127.0.0.1", p), H)
            sv.daemon_threads = True
            servers.append(sv)
            threading.Thread(target=sv.serve_forever, daemon=True).start()
        except OSError as e:
            log("Khong mo duoc cong %d: %s" % (p, e))
    if not servers:
        input("Khong mo duoc cong nao. Bam Enter de thoat...")
        return
    print("=" * 64)
    print(" BAN TIN CHUNG KHOAN dang chay tai  %s" % url)
    if LAN:
        ips = lan_ips()
        print("-" * 64)
        print(" MO TREN DIEN THOAI (cung mang Wi-Fi voi may tinh nay):")
        for ip in ips or ["<khong tim thay IP - xem bang lenh: ipconfig>"]:
            print("     http://%s:%d/" % (ip, PORTS[0]))
        print(" Neu Windows hoi tuong lua: chon cho phep mang Private (rieng tu).")
        print(" Chi may trong cung mang Wi-Fi mo duoc, Internet ben ngoai khong vao duoc.")
    print(" Giu cua so nay mo trong luc xem. Dong cua so = tat server.")
    print("=" * 64, flush=True)
    def _gh():
        try:
            print("-" * 64); gh_trang_thai(); print("-" * 64, flush=True)
        except Exception as e:
            print(" GITHUB: khong kiem tra duoc (%s)" % e, flush=True)
    if not CLOUD:
        threading.Thread(target=_gh, daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: open_browser(url)).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
