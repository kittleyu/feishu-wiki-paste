#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launch_chrome_debug.py — 为 CMS 浏览器自动化准备好带远程调试端口的 Chrome
================================================================================
⚠️ 为什么需要这个脚本：
   Chrome 有个硬性规矩——**远程调试端口不能和默认用户配置一起用**。直接
   `chrome.exe --remote-debugging-port=9222`（用默认 User Data）会启动失败。
   本脚本的做法：
     1) 关闭正在运行的 Chrome（保留原配置不动）
     2) 把你的 Chrome 配置（含 huiyouhua 登录态）复制一份到临时目录
        （排除缓存，复制很快，约几十 MB）
     3) 用这份副本带 --user-data-dir + --remote-debugging-port=9222 启动
     4) 轮询 127.0.0.1:9222 直到就绪
   这样既能用 CDP 接管，又不破坏你原 Chrome 的登录态/标签页。

用法：
    python launch_chrome_debug.py
    python launch_chrome_debug.py --port 9222 --profile-dir D:/tmp/chrome_debug

启动后保持该窗口打开，再跑 direction_b_cms_write.py（需要 Playwright 已装）。
"""
import os
import sys
import time
import shutil
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
    # 退回 PATH
    import shutil as _s
    in_path = _s.which("chrome") or _s.which("chrome.exe")
    return in_path


def kill_chrome():
    print(">>> 关闭正在运行的 Chrome ...")
    # Windows: taskkill；其他平台: pkill
    if sys.platform.startswith("win"):
        try:
            subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except Exception as e:
            print("    taskkill 异常(可忽略):", e)
    else:
        try:
            subprocess.run(["pkill", "-f", "chrome"], stderr=subprocess.DEVNULL, timeout=30)
        except Exception:
            pass
    time.sleep(2)
    print("    已尝试关闭。")


CACHE_DIRS = {"Cache", "Code Cache", "GPUCache", "Service Worker",
              "Storage", "Session Storage", "ShaderCache", "GoogleServices"}


def copy_profile(src_default, dst_default):
    """复制 Default 配置到临时目录，排除缓存目录。"""
    if os.path.isdir(dst_default):
        print(f">>> 复用已存在的调试配置: {dst_default}")
        return
    print(f">>> 复制 Chrome 配置(含登录态) {src_default} -> {dst_default} ...")
    os.makedirs(dst_default, exist_ok=True)

    def ignore(path, names):
        return {n for n in names if n in CACHE_DIRS}

    # 复制 Default 下所有内容（排除缓存）
    for entry in os.listdir(src_default):
        s = os.path.join(src_default, entry)
        d = os.path.join(dst_default, entry)
        if entry in CACHE_DIRS:
            continue
        try:
            if os.path.isdir(s):
                shutil.copytree(s, d, ignore=ignore)
            else:
                shutil.copy2(s, d)
        except Exception as e:
            print(f"    跳过 {entry}: {e}")
    # 复制 Local State（记录登录/profile 信息）
    src_ls = os.path.join(os.path.dirname(src_default), "Local State")
    if os.path.isfile(src_ls):
        try:
            shutil.copy2(src_ls, os.path.join(os.path.dirname(dst_default), "Local State"))
        except Exception as e:
            print(f"    跳过 Local State: {e}")
    print("    复制完成。")


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
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "chrome_debug_profile"))
    ap.add_argument("--chrome", default=None, help="chrome.exe 路径(可选)")
    args = ap.parse_args()

    chrome = args.chrome or find_chrome()
    if not chrome or not os.path.isfile(chrome):
        print("❌ 找不到 chrome.exe，请用 --chrome 指定路径。")
        sys.exit(1)
    print(">>> 使用 Chrome:", chrome)

    default_profile = os.path.join(
        os.path.dirname(args.profile_dir), "..", "User Data", "Default"
    )
    # 标准默认位置
    src_default = os.path.expandvars(
        r"%LOCALAPPDATA%\Google\Chrome\User Data\Default"
    )
    if not os.path.isdir(src_default):
        src_default = os.path.expandvars(
            r"%USERPROFILE%\AppData\Local\Google\Chrome\User Data\Default"
        )
    if not os.path.isdir(src_default):
        print("❌ 找不到 Chrome 默认配置目录(User Data/Default)。")
        sys.exit(1)

    kill_chrome()
    copy_profile(src_default, args.profile_dir)

    print(f">>> 启动 Chrome(调试端口 {args.port}) ...")
    proc = subprocess.Popen(
        [chrome,
         f"--user-data-dir={args.profile_dir}",
         f"--remote-debugging-port={args.port}",
         "--no-first-run",
         "--no-default-browser-check"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    print(f"    Chrome 进程 PID={proc.pid}")

    if wait_cdp(args.port):
        print(f"\n✅ Chrome 调试端口已就绪: http://127.0.0.1:{args.port}")
        print("   现在请确认 huiyouhua 后台已登录，然后跑 direction_b_cms_write.py。")
        print("   （保持此 Chrome 窗口打开，不要关；原 Chrome 配置未被改动）")
    else:
        print(f"\n⚠️ 端口 {args.port} 未在 {30}s 内就绪，请检查 Chrome 是否启动。")
        sys.exit(1)


if __name__ == "__main__":
    main()
