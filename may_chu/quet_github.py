# -*- coding: utf-8 -*-
"""Chay tren may chu GitHub Actions (1 gio/lan): bat server Python, mo ban tin bang Chrome an,
cho quet xong, xuat ban tinh -> site/index.html de GitHub Pages dang len.
Khong can may tinh o nha. Du lieu tu luu lich su (gia trong nuoc...) giu trong thu muc state/ (cache)."""
import json, os, socket, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE = os.path.join(ROOT, "state")
SITE = os.path.join(ROOT, "site")
os.makedirs(STATE, exist_ok=True)
os.makedirs(SITE, exist_ok=True)


def cho_cong(port, giay=40):
    t = time.time()
    while time.time() - t < giay:
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def main():
    env = dict(os.environ, BT_CLOUD="1", PYTHONIOENCODING="utf-8")
    sv = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "bantin_server.py"), "--no-browser"], cwd=HERE, env=env)
    try:
        if not cho_cong(8765):
            print("!! Server khong chay duoc"); return 2
        from playwright.sync_api import sync_playwright
        ls_file = os.path.join(STATE, "localStorage.json")
        ls = {}
        if os.path.exists(ls_file):
            try:
                ls = json.load(open(ls_file, encoding="utf-8"))
                print("Nap lai du lieu lan truoc: %d muc" % len(ls))
            except Exception:
                ls = {}
        with sync_playwright() as p:
            try:
                br = p.chromium.launch(channel="chrome")      # Chrome co san tren may chu GitHub
            except Exception:
                br = p.chromium.launch()
            ctx = br.new_context(locale="vi-VN", timezone_id="Asia/Ho_Chi_Minh", viewport={"width": 1280, "height": 900})
            ctx.add_init_script("window.__BT_CLOUD__ = true; try { var d = %s; for (var k in d) localStorage.setItem(k, d[k]); } catch (e) {}"
                                % json.dumps(ls, ensure_ascii=False))
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: print("  [loi trang]", e))
            t0 = time.time()
            pg.goto("http://127.0.0.1:8765/", wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_function("window.__BT_DONE__", timeout=14 * 60 * 1000, polling=2000)
            done = pg.evaluate("window.__BT_DONE__")
            print("Quet xong sau %d giay. %s" % (time.time() - t0, "OK" if done.get("ok") else "KHONG lay duoc so moi"))
            m = done.get("msg") or ["", [], []]
            for x in (m[1] or []):
                print("  +", x)
            for x in (m[2] or []):
                if "GitHub" not in x:
                    print("  -", x)
            # luu localStorage cho lan sau (bo khoa trang thai GitHub)
            ls = pg.evaluate("(function(){var o={};for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i);if(k!=='bt-gh-last')o[k]=localStorage.getItem(k);}return o;})()")
            json.dump(ls, open(ls_file, "w", encoding="utf-8"), ensure_ascii=False)
            html = pg.evaluate("window.__BT_OFFLINE__()")
            br.close()
        if not done.get("ok"):
            print("!! Lan nay khong lay duoc so moi -> giu nguyen trang dang co, lan sau quet lai"); return 0
        if not html or len(html) < 20000:
            print("!! Trang xuat ra qua ngan -> khong dang"); return 4
        with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        open(os.path.join(SITE, ".nojekyll"), "w").close()
        open(os.path.join(STATE, "da_dang.txt"), "w").write(time.strftime("%H:%M %d/%m/%Y"))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("dang=1\n")
        print("Da xuat site/index.html (%d KB)" % (len(html.encode("utf-8")) // 1024))
        return 0
    finally:
        sv.terminate()


if __name__ == "__main__":
    sys.exit(main())
