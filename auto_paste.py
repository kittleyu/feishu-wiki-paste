#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_paste.py — 全自动：启动调试 Chrome → 登录 huiyouhua → 反查 ID → 写词包 → 校验
仅用于本地自动化，密码通过环境变量 HYH_USER / HYH_PWD 传入，不落盘、不提交。

用法：
    HYH_USER=daixiaoyu HYH_PWD=xxxx \\
    python auto_paste.py --company "贵阳脉通血管医院有限公司" \\
        --pkg "贵阳静脉曲张医院推荐" --in gui_articles.json

说明：
    --in 为 prepare_multi.py / run_direction_b.py 已转换好的文章 JSON（[{title,content}]）
    --user-corp-id 可选，仅作交叉核对（强烈建议留空，按公司名反查真实 corp_id）
"""
import os, sys, time, subprocess, shutil, json, re, urllib.request, argparse

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
    """若 9222 没起，则用全新临时 profile 起一个独立调试 Chrome（不杀用户正常 Chrome）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=3):
            print(">>> 9222 已就绪，复用现有调试 Chrome")
            return
    except Exception:
        pass
    print(">>> 启动独立调试 Chrome (全新临时 profile) ...")
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
    """page.evaluate 的健壮版：遇页面导航/上下文销毁时等待并重试，避免任务崩溃。

    huiyouhua 是 SPA，登录态过期时前端会自动整页跳登录页，正在执行的 evaluate
    会报 'Execution context was destroyed'。捕获后等待导航完成再重试即可自愈。
    """
    last = None
    for i in range(retries):
        try:
            return page.evaluate(expr)
        except Exception as e:
            s = str(e)
            if ("Execution context was destroyed" in s or "navigated" in s
                    or "detached" in s or "frame" in s.lower()):
                print(f"    [safe_eval] 页面导航，等待重试 ({i+1}/{retries}) ...",
                      flush=True)
                try:
                    page.wait_for_load_state("load", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                last = e
                continue
            raise
    raise RuntimeError(f"safe_eval 多次因页面导航失败: {last}")


def goto_stable(page, url, timeout=30000):
    """跳转并等待页面真正稳定（load + 短暂 networkidle 探测），避免 evaluate 撞导航。"""
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
    print("    登录前 URL:", page.url)
    try:
        info = safe_eval(page, """(function(){
            var inputs=[].map.call(document.querySelectorAll('input'),function(i){
                return {type:(i.type||'text'), name:(i.name||''), id:(i.id||''),
                        placeholder:(i.placeholder||'')};
            });
            var btns=[].map.call(document.querySelectorAll('button'),function(b){
                return (b.textContent||'').trim().slice(0,20);
            });
            return {url:location.href, inputs:inputs, buttons:btns, title:document.title};
        })()""")
        print("    表单诊断:", json.dumps(info, ensure_ascii=False)[:800])
    except Exception as e:
        print("    诊断失败:", e)

    # 注意：不再自动点击「GEO账号登录」选项卡。
    # 2026-07-22 实测发现该按钮会把表单从默认的「运营账号」模式切换到
    # 「GEO 账号」模式，导致存储的运营账号凭据 (HYH_USER/HYH_PWD) 静默失败。
    # 默认表单就是运营账号，直接填即可。

    pw = page.query_selector('input[type="password"]')
    if not pw:
        print("❌ 未找到密码输入框，可能登录页结构变化")
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
        norm = re.sub(r'\s+', '', txt)
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
        print("    仍未离开登录页，继续自检")
    page.wait_for_timeout(3000)
    print("    登录后 URL:", page.url)
    ok = check_logged_in(page)
    if ok:
        print("✅ 登录成功 (API 验证通过)")
        return True
    diag = safe_eval(page, """(function(){
        var t=document.body?document.body.innerText:'';
        var errs=[].map.call(document.querySelectorAll(
            '.el-message,.el-message--error,[class*=error],[class*=message],[class*=tip]'),
            function(e){return (e.innerText||'').trim();}).filter(Boolean).slice(0,6);
        return {snippet:t.slice(0,300), errs:errs};
    })()""")
    print("    登录失败诊断:", json.dumps(diag, ensure_ascii=False)[:700])
    return False


def discover_ids(page, company, pkg, user_corp_id=None, log_fn=print):
    print(f">>> 反查公司/词包 ID (公司={company!r} 词包={pkg!r}) ...")
    if not check_logged_in(page):
        log_fn(">>> 登录态失效，重新登录 ...")
        if not do_login(page):
            log_fn("❌ 登录失败")
            return None, None
        goto_stable(page, TARGET)
    corps = safe_eval(page,
        "(async()=>{var r=await fetch('/yunying/v1/corp/active',{method:'GET'});"
        "var d=await r.json();return d.data.corps;})()")
    corp = None
    for c in corps:
        if (c.get("name") or "") == company:
            corp = c
            break
    if not corp:
        for c in corps:
            if company in (c.get("name") or ""):
                corp = c
                break
    if not corp:
        print(f"❌ 未找到公司: {company}（可访问 {len(corps)} 个）")
        print("   公司列表:", [c.get("name") for c in corps][:20])
        return None, None
    corp_id = corp["id"]
    print(f"✅ 公司: {corp['name']}  corp_id={corp_id}  (用户给的={user_corp_id})")
    if user_corp_id and str(corp_id) != str(user_corp_id):
        print(f"⚠️ API 返回的 corp_id({corp_id}) 与用户给的({user_corp_id})不一致，以 API 为准")
    elif not user_corp_id:
        print("ℹ️ 未提供 user_corp_id，以 API 反查为准")
    safe_eval(page,
        f"(async()=>{{await fetch('/yunying/v1/auth/changecorp?corp_id={corp_id}',"
        f"{{method:'GET'}});}})()")
    page.wait_for_timeout(800)
    pkgs = safe_eval(page,
        "(async()=>{var r=await fetch('/yunying/v1/keyword/package?page=1&page_size=10000',"
        "{method:'GET'});var d=await r.json();return d.data.keyword_packages;})()")
    found = None
    for k in pkgs:
        desc = k.get("description") or ""
        name = k.get("name") or ""
        if pkg == desc or pkg == name:
            found = k
            break
    if not found:
        for k in pkgs:
            desc = k.get("description") or ""
            name = k.get("name") or ""
            if pkg in desc or pkg in name:
                found = k
                break
    if not found:
        print(f"❌ 公司(corp_id={corp_id})下未找到词包: {pkg}（共 {len(pkgs)} 个）")
        print("   词包列表:", [(k.get('id'), k.get('description')) for k in pkgs][:20])
        return corp_id, None
    print(f"✅ 词包: {found.get('description')}  pkg_id={found['id']}")
    return corp_id, found["id"]


def write_articles(page, articles, corp_id, pkg_id, limit=None):
    """单篇 evaluate 写入，一篇失败不影响其他，且能规避整批 evaluate 撞导航崩溃。"""
    items = articles[:limit] if limit else articles
    results = []
    for a in items:
        if not a.get("content", "").strip():
            continue
        call = {"title": a["title"], "content": a["content"],
                "corp_id": corp_id, "keyword_package_id": pkg_id}
        expr = (
            "(async()=>{"
            "  var call=" + json.dumps(call, ensure_ascii=False) + ";"
            "  var ctrl=new AbortController();"
            "  var t=setTimeout(function(){ctrl.abort();},15000);"
            "  try {"
            "    var r=await fetch('/yunying/v1/articles',{"
            "      method:'POST', headers:{'Content-Type':'application/json'},"
            "      body:JSON.stringify(call), signal:ctrl.signal});"
            "    clearTimeout(t);"
            "    var d=await r.json();"
            "    return JSON.stringify({index:0,code:d.code,msg:(d.msg||d.message||''),"
            "      id:(d.data&&d.data.id),title:call.title.substring(0,40),"
            "      corp_id:call.corp_id,pkg:call.keyword_package_id});"
            "  } catch(e){"
            "    clearTimeout(t);"
            "    return JSON.stringify({index:0,code:-1,error:String(e),"
            "      title:call.title.substring(0,40)});"
            "  }"
            "})()"
        )
        try:
            res = safe_eval(page, expr)
            results.append(json.loads(res))
        except Exception as e:
            results.append({"index": 0, "code": -1, "error": str(e),
                            "title": a["title"][:40]})
    return results


def verify_written(page, results, corp_id, pkg_id):
    good, bad = [], []
    for x in results:
        aid = x.get("id")
        if not aid or x.get("code") != 0:
            bad.append(x)
            continue
        try:
            res = safe_eval(page,
                "(async()=>{var r=await fetch('/yunying/v1/articles/%s',{method:'GET'});"
                "var d=await r.json();return JSON.stringify({ok:true,data:d});})()" % aid)
            j = json.loads(res)
        except Exception:
            bad.append({**x, "verify": "解析失败"})
            continue
        art = (j.get("data") or {}).get("data") or {}
        real_corp = art.get("corp_id")
        real_pkg = art.get("keyword_package_id")
        ok_corp = (real_corp == corp_id) if corp_id is not None else True
        ok_pkg = (real_pkg == pkg_id) if pkg_id is not None else True
        if ok_corp and ok_pkg:
            good.append({**x, "verify_corp": real_corp, "verify_pkg": real_pkg})
        else:
            bad.append({**x, "verify_corp": real_corp, "verify_pkg": real_pkg,
                        "mismatch": True})
    return good, bad


def run_pipeline(company, pkg, in_file, out_file=None, user_corp_id=None, log_fn=print):
    """可被 bot 调用：登录 huiyouhua → 反查 ID → 探路 → 全量写并校验。
    in_file 为文章 JSON 绝对路径；log_fn 用于把进度转发到飞书群（默认 print）。"""
    out_file = out_file or os.path.join(HERE, "cms_write_results.json")
    ensure_chrome()
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = b.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        # 先让页面稳定在目标页，再判断登录态（避免对未稳定页面 evaluate 撞导航）
        goto_stable(page, TARGET)
        if not check_logged_in(page):
            log_fn(">>> 未登录，执行登录 ...")
            if not do_login(page):
                log_fn("❌ 登录失败，需人工介入（验证码/凭证错误）")
                return False
            goto_stable(page, TARGET)
            if not check_logged_in(page):
                log_fn("❌ 登录后仍无法访问接口，登录可能失败")
                return False
        else:
            log_fn("✅ 已是登录态")

        corp_id, pkg_id = discover_ids(page, company, pkg, user_corp_id, log_fn=log_fn)
        if corp_id is None or pkg_id is None:
            log_fn("❌ ID 反查失败，终止")
            return False

        articles = json.load(open(in_file, encoding="utf-8"))
        log_fn(f">>> 待写入文章 {len(articles)} 篇")

        log_fn(">>> [探路] 写 1 篇 ...")
        probe = write_articles(page, articles, corp_id, pkg_id, limit=1)
        pgood, pbad = verify_written(page, probe, corp_id, pkg_id)
        log_fn(f"    探路结果: 成功 {len(pgood)} / 异常 {len(pbad)}")
        if pbad:
            log_fn("⚠️ 探路失败，先不铺开")
            return False

        log_fn(f">>> 正式写入 {len(articles)} 篇 ...")
        results = write_articles(page, articles, corp_id, pkg_id)
        ok_w = [x for x in results if x.get("code") == 0]
        bad_w = [x for x in results if x.get("code") != 0]
        log_fn(f"    接口返回: 成功 {len(ok_w)} / 失败 {len(bad_w)}")
        good, bad = verify_written(page, results, corp_id, pkg_id)
        log_fn(f"    逐篇校验: 正确落库 {len(good)} / 异常 {len(bad)}")
        for x in bad:
            log_fn("    异常:" + str(x))
        if bad:
            log_fn("⚠️ 有异常篇目，建议检查后用 --delete 清理错发的再补")
        else:
            log_fn("✅ 全部正确落库，任务完成")
        json.dump(results, open(out_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return {"total": len(articles), "ok": len(good), "bad": len(bad),
                "corp_id": corp_id, "pkg_id": pkg_id}
    finally:
        b.close()
        p.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True, help="公司全名，如 贵阳脉通血管医院有限公司")
    ap.add_argument("--pkg", required=True, help="词包名，如 贵阳静脉曲张医院推荐")
    ap.add_argument("--in", dest="in_file", default="gui_articles.json",
                    help="已转换好的文章 JSON")
    ap.add_argument("--user-corp-id", type=int, default=None,
                    help="可选，仅交叉核对，不填则按公司名反查")
    ap.add_argument("--out", default="cms_write_results.json")
    args = ap.parse_args()
    run_pipeline(args.company, args.pkg, os.path.join(HERE, args.in_file),
                 os.path.join(HERE, args.out), args.user_corp_id)


if __name__ == "__main__":
    main()
