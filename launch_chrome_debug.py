#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launch_chrome_debug.py — 为 CMS 浏览器自动化准备好带远程调试端口的 Chrome
================================================================================
⚠️ 为什么需要这个脚本：
   Chrome 有个硬性规矩——**远程调试端口不能和默认用户配置一起用**。直接
   `chrome.exe --remote-debugging-port=9222`（用默认 User Data）会启动失败。

🔒 安全设计（2026-07 修正）：
   - **不复制**你的 Chrome 用户档案（避免把 Cookie/书签/登录态带进仓库目录，
     之前这曾在仓库内生成含隐私的 chrome_debug_profile/ 副本）。
   - **不杀**你正在用的 Chrome（不再 taskkill，改为用独立临时 profile 并行启动）。
   - 临时 profile 默认放在系统 %TEMP%，不在本仓库目录内。
   启动后调试 Chrome 是「未登录」全新态：请手动登录 huiyouhua，或用
   `auto_paste.py`（已内置自动登录）直接跑全流程。

用法：
    python launch_chrome_debug.py
    python launch_chrome_debug.py --port 9222
"""
import os
import sys
import time
import subprocess
import urllib.request

# Windows 终端默认 GBK，重配置为 utf-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def find_chrome():
    """在常见位置找 chrome.exe"""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    in_path = shutil_which("chrome") or shutil_which("chrome.exe")
    return in_path


def shutil_which(name):
    import shutil as _s
    return _s.which(name)


def wait_cdp(port, timeout=30):
    url = f"http://127.0.0.1:{port}/json/version"
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


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--profile-dir",
                    default=os.path.join(os.path.expandvars("%TEMP%"),
                                         "hyh_chrome_debug"))
    ap.add_argument("--chrome", default=None, help="chrome.exe 路径(可选)")
    args = ap.parse_args()

    chrome = args.chrome or find_chrome()
    if not chrome or not os.path.isfile(chrome):
        print("❌ 找不到 chrome.exe，请用 --chrome 指定路径。")
        sys.exit(1)
    print(">>> 使用 Chrome:", chrome)

    # 全新临时 profile（不复制用户档案，避免隐私泄露）
    profile = args.profile_dir
    os.makedirs(profile, exist_ok=True)
    print(f">>> 使用全新临时 profile（不复制用户档案）: {profile}")

    print(f">>> 启动 Chrome(调试端口 {args.port})，与你的正常 Chrome 并行运行 ...")
    proc = subprocess.Popen(
        [chrome,
         f"--user-data-dir={profile}",
         f"--remote-debugging-port={args.port}",
         "--no-first-run",
         "--no-default-browser-check"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    print(f"    Chrome 进程 PID={proc.pid}")

    if wait_cdp(args.port):
        print(f"\n✅ Chrome 调试端口已就绪: http://127.0.0.1:{args.port}")
        print("    调试 Chrome 为全新未登录态：请手动登录 huiyouhua，")
        print("    或改用 auto_paste.py 自动登录并写完整个流程。")
        print("    （保持此 Chrome 窗口打开；你的正常 Chrome 不受影响）")
    else:
        print(f"\n⚠️ 端口 {args.port} 未在 {30}s 内就绪，请检查 Chrome 是否启动。")


if __name__ == "__main__":
    main()
