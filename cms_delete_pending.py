#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cms_delete_pending.py — 列出 / 删除指定公司「审核完成且待发布」文章
状态: audit_status=1 (审核完成) & publish_status=0 (待发布)

用法:
    # 仅列出（安全预览，不删任何东西）
    python cms_delete_pending.py --corp-id <客户corp_id>

    # 列出并实际删除全部命中
    python cms_delete_pending.py --corp-id <客户corp_id> --delete

    # 仅删除标题以某前缀开始的命中（精确控制范围）
    python cms_delete_pending.py --corp-id <客户corp_id> --startswith "示例文章标题前缀" --delete

    # 若当前登录账号默认公司不是目标公司，显式切换（GET changecorp 后重载 SPA）
    python cms_delete_pending.py --corp-id <客户corp_id> --changecorp <客户corp_id> --delete
"""
import os, sys, time, subprocess, shutil, json, argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PORT = 9222
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(os.path.expandvars("%TEMP%"), "hyh_debug_profile")
CHROME = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
USER = os.environ.get("HYH_USER", "daixiaoyu")
PWD = os.environ.get("HYH_PWD", "")
TARGET = "https://yunying.huiyouhua.com/cms-yunying.html?tab=articles"


def wait_cdp(timeout=45):
    import urllib.request
    url = f"http://127.0.0.1:{PORT}/json/version"
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def ensure_chrome():
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=3):
            print(">>> 9222 已就绪，复用现有调试 Chrome")
            return
    except Exception:
        pass
    print(">>> 启动独立调试 Chrome (临时 profile) ...")
    if os.path.isdir(PROFILE):
        shutil.rmtree(PROFILE, ignore_errors=True)
    os.makedirs(PROFILE, exist_ok=True)
    if not os.path.isfile(CHROME):
        print("❌ 找不到 chrome.exe:", CHROME)
        sys.exit(1)
    proc = subprocess.Popen(
        [CHROME, f"--user-data-dir={PROFILE}", f"--remote-debugging-port={PORT}",
         "--no-first-run", "--no-default-browser-check", "--new-window", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    print("    Chrome PID =", proc.pid)
    if not wait_cdp():
        print("❌ 9222 未就绪")
        sys.exit(1)
    print("✅ 9222 就绪")


def safe_eval(page, expr, retries=3):
    last = None
    for i in range(retries):
        try:
            return page.evaluate(expr)
        except Exception as e:
            s = str(e)
            if ("Execution context was destroyed" in s or "navigated" in s
                    or "detached" in s or "frame" in s.lower()):
                print(f"    [safe_eval] 页面导航，等待重试 ({i+1}/{retries}) ...", flush=True)
                try:
                    page.wait_for_load_state("load", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                last = e
                continue
            raise
    raise RuntimeError(f"safe_eval 多次失败: {last}")


def goto_stable(page, url, timeout=30000):
    page.goto(url, wait_until="load", timeout=timeout)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(2000)


def check_logged_in(page):
    try:
        res = safe_eval(page,
            "(async()=>{try{var r=await fetch('/yunying/v1/corp/active',{method:'GET'});"
            "var d=await r.json();return {status:r.status, code:(d&&d.code), hasData:!!(d&&d.data)};"
            "}catch(e){return {error:String(e)};}})()")
        return res.get("status") == 200 and res.get("hasData")
    except Exception:
        return False


def do_login(page):
    print(">>> 打开登录页 ...")
    goto_stable(page, "https://yunying.huiyouhua.com/", timeout=30000)
    pw = page.query_selector('input[type="password"]')
    if not pw:
        print("❌ 未找到密码输入框")
        return False
    user = None
    for inp in page.query_selector_all('input'):
        t = (inp.get_attribute('type') or 'text').lower()
        if t in ('text', 'email', 'tel', 'number', ''):
            user = inp
            break
    if not user:
        user = page.query_selector('input:not([type="password"])')
    if user:
        user.fill(USER)
    pw.fill(PWD)
    sub = None
    for btn in page.query_selector_all('button'):
        txt = (btn.inner_text() or '').strip()
        norm = "".join(txt.split())
        low = norm.lower()
        if '账号' in norm or 'geo' in low:
            continue
        if '登录' in norm or 'sign' in low or 'submit' in (btn.get_attribute('type') or ''):
            sub = btn
            break
    if sub:
        print("    点击登录按钮:", repr(sub.inner_text()))
        sub.click()
    else:
        print("    未找到登录按钮，尝试回车")
        pw.press('Enter')
    try:
        page.wait_for_url(lambda u: 'login' not in u.lower(), timeout=15000)
        print("    已离开登录页")
    except Exception:
        print("    ⚠️ 等待离开登录页超时")
    return True


def fetch_eval(page, method, path, body=None):
    expr = (
        "(async()=>{"
        f"  var method={json.dumps(method)};"
        f"  var path={json.dumps(path)};"
        f"  var body={json.dumps(body) if body is not None else 'null'};"
        "  try {"
        "    var ctrl=new AbortController();"
        "    var t=setTimeout(function(){ctrl.abort();},20000);"
        "    var hd=body?{'Content-Type':'application/json'}:{};"
        "    var r=await fetch(path,{method:method,headers:hd,"
        "      credentials:'include',"
        "      body: body?JSON.stringify(body):undefined, signal:ctrl.signal});"
        "    clearTimeout(t);"
        "    var txt=await r.text();"
        "    var d; try{d=JSON.parse(txt);}catch(e){d={__raw:txt};}"
        "    return JSON.stringify({ok:true, status:r.status, data:d});"
        "  } catch(e){"
        "    return JSON.stringify({ok:false, error:String(e)});"
        "  }"
        "})()"
    )
    return json.loads(page.evaluate(expr))


def do_changecorp(page, corp_id):
    """切换到指定公司（GET changecorp 后重载 SPA）。
    注意：changecorp 必须用 GET（POST 会返回 404）；调用后须重载文章页，
    否则列表视图仍停留在旧公司上下文、可能返回空。"""
    print(f">>> 切换公司 corp_id={corp_id} ...")
    res = fetch_eval(page, "GET", f"/yunying/v1/auth/changecorp?corp_id={corp_id}")
    print(f"    changecorp 响应 status={res.get('status')}")
    page.reload(wait_until="load")
    page.wait_for_timeout(3000)


def list_all(page):
    """翻页收集当前公司全部文章（不带状态过滤，避免过滤参数不稳定）。
    依赖 fetch_eval 的 credentials:'include' 携带登录态 Cookie。
    每页带重试：SPA 会话偶发未就绪会返回空，重试即可稳定拿到数据。"""
    import time as _t
    all_arts = []
    seen = set()
    page_num = 1
    while page_num <= 200:
        batch = []
        for attempt in range(6):
            res = fetch_eval(page, "GET",
                f"/yunying/v1/creation/articles?page={page_num}&page_size=200")
            batch = (res.get("data") or {}).get("articles") or []
            if batch:
                break
            _t.sleep(1.2)
        if not batch:
            print(f"    第{page_num}页: 空（已重试），停止")
            break
        for a in batch:
            aid = a.get("id")
            if aid in seen:
                continue
            seen.add(aid)
            all_arts.append(a)
        print(f"    第{page_num}页: +{len(batch)} 篇 (累计 {len(all_arts)})")
        if len(batch) < 200:
            break
        page_num += 1
    return all_arts


def delete_articles(page, ids):
    ok = 0
    for aid in ids:
        res = fetch_eval(page, "DELETE", f"/yunying/v1/articles/{aid}")
        good = res.get("ok") and ((res.get("data") or {}).get("code") == 0)
        print(f"    id={aid}: {'✅' if good else '❌ ' + str(res)[:200]}")
        if good:
            ok += 1
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corp-id", type=int, required=True, help="目标公司 corp_id")
    ap.add_argument("--changecorp", type=int, default=None,
                    help="若当前默认公司非目标公司，显式 GET 切换到的 corp_id（通常与 --corp-id 相同）")
    ap.add_argument("--delete", action="store_true", help="实际删除（默认仅列出）")
    ap.add_argument("--startswith", default=None, help="仅命中标题以此前缀开始的文章")
    ap.add_argument("--contains", default=None, help="仅命中标题包含该子串的文章")
    args = ap.parse_args()

    ensure_chrome()
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = b.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        if not check_logged_in(page):
            do_login(page)
            if not check_logged_in(page):
                print("❌ 登录失败，退出")
                sys.exit(1)

        # 进入文章列表页：SPA 加载并写入当前公司上下文。
        # 若目标公司不是当前默认公司，先 GET changecorp 切换并等待重载，
        # 否则列表视图停留在旧公司、可能返回空或错误数据。
        goto_stable(page, TARGET, timeout=30000)
        page.wait_for_timeout(3000)
        if args.changecorp:
            do_changecorp(page, args.changecorp)
            goto_stable(page, TARGET, timeout=30000)
            page.wait_for_timeout(3000)

        user = fetch_eval(page, "GET", "/yunying/v1/user/current?platform=win")
        corp_id = (user.get("data") or {}).get("corp_id")
        corp_name = (user.get("data") or {}).get("corp_name", "未知")
        print(f">>> 当前公司: {corp_id} {corp_name}")

        print(f">>> 拉取全部文章（翻页）...")
        all_arts = list_all(page)

        # 状态分布统计（便于核对）
        from collections import Counter
        dist = Counter((a.get("audit_status"), a.get("publish_status"))
                       for a in all_arts)
        print(">>> 状态分布 (audit_status, publish_status):")
        for k, v in sorted(dist.items()):
            print(f"    au={k[0]} pub={k[1]}: {v} 篇")

        # 搜索锚点标题（可选，便于从某文章开始界定范围）
        anchor = args.contains or "示例文章标题锚点"
        anchors = [a for a in all_arts if anchor in (a.get("title") or "")]
        if anchors:
            print(f">>> 含「{anchor}」的锚点文章:")
            for a in anchors:
                print(f"    id={a.get('id')} au={a.get('audit_status')} pub={a.get('publish_status')} | {a.get('title','')}")

        # 默认命中「审核完成且待发布」= audit_status=1 & publish_status=0
        arts = [a for a in all_arts
                if a.get("audit_status") == 1 and a.get("publish_status") == 0]

        # 范围过滤（可选，精确控制）
        if args.startswith:
            arts = [a for a in arts if (a.get("title") or "").startswith(args.startswith)]
        if args.contains:
            arts = [a for a in arts if args.contains in (a.get("title") or "")]

        print(f"\n=== 命中「审核完成且待发布」共 {len(arts)} 篇 ===")
        for i, a in enumerate(arts, 1):
            print(f"  [{i}] id={a.get('id')} | pkg={a.get('keyword_package_id')} | "
                  f"{a.get('title','')}")
        print(f"=== 命中「审核完成且待发布」共 {len(arts)} 篇 ===")

        if not arts:
            print("没有命中任何文章，退出")
            return

        if not args.delete:
            print("\n⚠️ 当前为「仅列出」模式，未删除任何文章。"
                  "确认无误后加 --delete 执行删除。")
            return

        print(f"\n>>> 开始删除 {len(arts)} 篇 ...")
        ok = delete_articles(page, [a["id"] for a in arts])
        print(f"✅ 删除成功 {ok}/{len(arts)} 篇")
    finally:
        try:
            b.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
