#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu_bot.py — 飞书群聊机器人（方向 B：飞书目录 → 公司词包）
基于飞书事件「长连接模式」(WebSocket)，本机常驻即可收群消息，无需公网地址。

流程：群里 @机器人 发指令 → 解析 → 调现有的飞书读取 + huiyouhua 自动登录写入流程 → 把进度/结果回发群里。

指令格式（自然语言解析）：
  @机器人 粘贴 <飞书目录链接> 到 <公司全名> <词包名>
  @机器人 粘贴 <父目录链接> 下面 6/29 6/30 7.1 7/2 到 <公司全名> <词包名>

前置（飞书开放平台）：
  - 应用 → 事件订阅 → 开启，订阅方式选「长连接模式」
  - 添加事件 im.message.receive_v1（接收消息）
  - 权限管理开通：im:message、im:message.group、im:message:send_as_bot
  - 机器人已加入目标群，且群已开知识库权限（读取 wiki 用）

运行：python feishu_bot.py   （保持终端/窗口开着即在线）
依赖：lark-oapi（已装入 venv）、playwright（同前）
凭证：FEISHU_APP_ID/SECRET 在 .env；HYH_USER/HYH_PWD 在 .env（huiyouhua 自动登录用）
"""
import os, sys, json, re, threading, time, psutil, requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from prepare_multi import load_env, feishu_collect, get_token, get_node_space, list_nodes

load_env()
os.environ.setdefault("HYH_USER", "daixiaoyu")
if not os.environ.get("HYH_PWD"):
    print("⚠️ 未检测到 HYH_PWD（huiyouhua 密码），机器人将无法自动登录；请在 .env 配置 HYH_USER/HYH_PWD")

# 必须在设置好 HYH_* 之后再 import auto_paste（它在模块顶层读环境变量）
from auto_paste import run_pipeline
# 方向 A：CMS → 飞书（huilaihua 最新 N 篇 → 飞书 Wiki 目录）
from direction_a_cms_to_feishu import run_pipeline_a

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, P2ImMessageReceiveV1

APP_ID = os.environ.get("FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
if not (APP_ID and APP_SECRET):
    print("❌ 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET（检查 .env）")
    sys.exit(1)

LOG_LEVEL = getattr(lark.LogLevel, "INFO", lark.LogLevel.DEBUG)

# 飞书 API 客户端（发消息用，内部缓存 tenant_access_token）
_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

# 同一时刻只跑一个粘贴任务（Chrome 调试端口独占）
_paste_lock = threading.Lock()

# 单实例锁文件（避免多实例抢同一个 Chrome 调试端口）
_BOT_LOCK = os.path.join(HERE, ".bot.lock")


_HEARTBEAT = os.path.join(HERE, ".bot.heartbeat")

# ── 客户映射表：公司名 → 飞书顶层目录 + 审核表格 ──
_CLIENT_MAP = {}
def _load_client_map():
    """加载 client_map.json（若文件不存在则为空）。"""
    fp = os.path.join(HERE, "client_map.json")
    if os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                _CLIENT_MAP.update(json.load(f))
        except Exception as e:
            print(f"⚠️ 加载 client_map.json 失败：{e}")
_load_client_map()


def _write_heartbeat():
    """活实例定期写入 {pid}\\n{timestamp}，供新实例判断「挡路实例是否卡死」。"""
    try:
        tmp = _HEARTBEAT + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{os.getpid()}\n{time.time()}")
        os.replace(tmp, _HEARTBEAT)
    except Exception:
        pass


def _read_heartbeat():
    try:
        with open(_HEARTBEAT) as f:
            lines = f.read().splitlines()
        if len(lines) >= 2:
            return int(lines[0]), float(lines[1])
    except Exception:
        pass
    return None


def _kill_pid(pid):
    try:
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
    except Exception:
        pass


def _other_is_stale(other_pid):
    """判断挡路实例是否已失效（卡死/连不上飞书），可安全接管。
    返回 True 表示可 kill 接管；False 表示对方健康，应拒绝重复启动。"""
    hb = _read_heartbeat()
    if not hb:
        return True            # 无心跳文件 → 视为已失效
    hb_pid, hb_ts = hb
    if hb_pid != other_pid:
        return True            # 心跳记录的不是它 → 不一致，接管
    if time.time() - hb_ts > 90:
        return True            # 心跳超 90s 未更新 → 卡死
    return False


def _acquire_bot_lock():
    """单实例保护：扫描是否已有其他 feishu_bot.py 主进程在运行。
    排除自身(pid)及自身 fork 的 worker(父=自身)。lark ws 库会 fork worker，
    但 worker 不重跑 main()，本函数只在 fork 前执行一次，故不会被自身 worker 干扰。
    相比锁文件方案，进程扫描在 fork 模型下更稳健（无锁文件被 worker/atexit 误删的问题）。
    返回 (ok, other_pid)：ok=True 可启动；ok=False 时 other_pid 为挡路实例。"""
    me = os.getpid()
    for p in psutil.process_iter(['pid', 'cmdline', 'ppid']):
        try:
            if p.info['pid'] == me:
                continue
            cl = ' '.join(p.info['cmdline'] or [])
            if 'feishu_bot.py' not in cl:
                continue
            # 排除本次会话里用于调试/验证的辅助脚本（它们也 import 了本模块）
            if '_verify_bot' in cl or '_debug_bot' in cl:
                continue
            if p.info['ppid'] == me:
                # 自身 fork/spawn 出来的 worker，跳过
                continue
            # 发现另一个 feishu_bot.py 主实例 → 已有实例在跑
            return (False, p.info['pid'])
        except Exception:
            continue
    return (True, None)


def _release_bot_lock():
    try:
        if os.path.exists(_BOT_LOCK):
            os.remove(_BOT_LOCK)
    except Exception:
        pass


def _setup_logging():
    """把 stdout/stderr 重定向到 bot_console.log（追加模式），便于 pythonw 无窗口常驻时也不丢日志。"""
    try:
        log_path = os.path.join(HERE, "bot_console.log")
        _f = open(log_path, "a", buffering=1, encoding="utf-8")
        sys.stdout = _f
        sys.stderr = _f
    except Exception:
        pass

HELP = (
    "📌 指令格式（三种方向都支持）：\n"
    "【方向 B｜飞书→CMS】\n"
    "@我 粘贴 <飞书目录链接> 到 <公司全名> <词包名>\n"
    "示例：@我 粘贴 https://vcnd134o0gra.feishu.cn/wiki/AbCd1234 到 云南约牛软件技术有限公司 天龙博弈\n"
    "多子目录：@我 粘贴 <父目录链接> 下面 6/29 6/30 7.1 7/2 到 <公司全名> <词包名>\n"
    "【方向 A｜CMS→飞书】\n"
    "@我 把 <公司全名>(id可选) 粘贴到 <飞书目录链接> 最新10条\n"
    "示例：@我 把 中泰期货股份有限公司 粘贴到 https://vcnd134o0gra.feishu.cn/wiki/Zi5Cxxx 最新30篇\n"
    "【方向 F｜目录链接→填表】\n"
    "@我 把 <源目录链接> 的文章链接填到 <表格链接> 第二列\n"
    "示例：@我 把 https://vcnd134o0gra.feishu.cn/wiki/D5nLxxx 的文章链接填到 https://vcnd134o0gra.feishu.cn/wiki/PI5xxx 第二列\n"
    "（可选）指定工作表：… 第二列 工作表 6月内容\n"
    "⚠️ 源必须是文件夹/docx 目录（含文章子页）；目标必须是电子表格(sheet)"
    "【方向 G｜智能生文→粘贴→填表（全自动）】\n"
    "@我 生文 <公司/产品> <关键词包> <数量> 粘贴到 <飞书目录链接> 填到 <表格链接>\n"
    "示例：@我 生文 长岛民谣 长岛民谣推荐 10 粘贴到 https://vcnd134o0gra.feishu.cn/wiki/AbCd 填到 https://vcnd134o0gra.feishu.cn/wiki/PI5\n"
    "（粘贴到/填到 可省略：只生文就写 @我 生文 长岛民谣 长岛民谣推荐 10）\n"
    "机器人会自动：点「智能生文」→ 选词包→填数量→等生成完→粘贴到飞书→填表，每步回你一句\n"
    "【更新机器人】\n"
    "@我 更新  → 拉取最新代码并自动重启（无需手动操作）"
)


def send_text(chat_id, text):
    """把一段文本发到指定群会话。"""
    content = json.dumps({"text": text})
    req = (CreateMessageRequest.builder()
           .receive_id_type("chat_id")
           .request_body(CreateMessageRequestBody.builder()
                         .receive_id(chat_id)
                         .msg_type("text")
                         .content(content)
                         .build())
           .build())
    try:
        resp = _client.im.v1.message.create(req)
        if not getattr(resp, "success", lambda: False)():
            print("⚠️ 发消息失败:", getattr(resp, "msg", ""), getattr(resp, "code", ""))
    except Exception as e:
        print("⚠️ 发消息异常:", e)


_CN_NUM = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
           '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def _cn_to_int(s):
    """把 '10' / '十' / '二十' / '三十' / '十二' 等转成 int（覆盖 1–99 常用文章数）。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if '十' in s:
        left, _, right = s.partition('十')
        tens = _CN_NUM.get(left, 1) if left else 1   # 十=10, 二十=20
        ones = _CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_NUM.get(s)


def parse_command(text):
    """解析指令，返回 dict：方向 A 或 方向 B。失败返回 None。

    方向 A：把 <公司(id)> 粘贴到 <飞书目录链接> [最新N条]
      → {"dir":"A","company":...,"node":...,"limit":10}
    方向 B：粘贴 <飞书目录链接> 到 <公司> <词包> [下面 子目录]
      → {"dir":"B","node":...,"subdirs":...,"company":...,"pkg":...}
    """
    nodes = re.findall(r"(?:feishu|larksuite)\S*wiki/([A-Za-z0-9]+)", text)
    node = nodes[0] if nodes else None

    # ── 方向 G（智能生文）优先：避免被下方 A/B/F 误判 ──
    g = parse_g(text)
    if g:
        return g

    # ── 方向 F：把 <源目录链接> 的文章链接填到 <表格链接> 第N列 ──
    if "填" in text and len(nodes) >= 2 and ("链接" in text or "表" in text):
        src, dst = nodes[0], nodes[1]
        cm = re.search(r"第\s*(\d+)\s*列", text)
        col = int(cm.group(1)) if cm else 2
        sh = re.search(r"工作表[\s：:]*([^\s,，。]+)", text)
        sheet = sh.group(1) if sh else None
        return {"dir": "F", "src": src, "dst": dst, "col": col, "sheet": sheet}

    # ── 方向 A（简写）：<公司名> [最新] <N>篇（无链接，从 client_map 自动补全）──
    if node is None:
        for cname in sorted(_CLIENT_MAP.keys(), key=lambda x: -len(x)):
            if cname in text:
                lm = re.search(r"最新\s*([0-9零一二两三四五六七八九十百]+)", text) or re.search(r"([0-9零一二两三四五六七八九十百]+)\s*篇", text)
                limit = _cn_to_int(lm.group(1)) if lm else 10
                mp = _CLIENT_MAP[cname]
                # 子目录：有显式「自建/新建」则提取，否则默认今天日期
                subdir = None
                if re.search(r"目录下|子目录|新建|自建", text):
                    dm = re.search(r"(?<!\d)(\d{1,2}[./]\d{1,2}|\d+\.\d+)(?!\d)", text)
                    if dm:
                        subdir = dm.group(1)
                    else:
                        bm = re.search(r"(?:自建个?|新建|建)\s*([^\s,，。目录]+?)\s*目录", text)
                        subdir = bm.group(1) if bm else None
                else:
                    # 默认：当天日期 MM.DD
                    subdir = time.strftime("%m.%d").lstrip("0")  # 7.22
                return {"dir": "A", "company": cname, "node": mp["dir_node"],
                        "limit": limit, "subdir": subdir, "sheet": mp["sheet_node"]}

    # ── 方向 A（完整）：把 <公司> 粘贴到 <飞书链接> ──
    if "把" in text and "粘贴到" in text:
        if not node:
            return None
        left = text.split("把", 1)[1].split("粘贴到", 1)[0]
        # 去掉「（id：83966）」「(编号123)」之类的标注
        company = re.sub(r"[（(]\s*(id|编号|corp|公司编号)[\s：:]*\d+[）)]",
                         "", left, flags=re.IGNORECASE)
        # 去掉尾部「的最新30篇 / 最新十篇 / 最新30篇文章 / 最新30条」等数量修饰（含中文数字）
        company = re.sub(r"的最新.*?(篇|条|篇文章|篇文)?\s*$", "", company)
        company = re.sub(r"最新.*?(篇|条|篇文章|篇文)?\s*$", "", company)
        company = company.strip(" （()）").strip()
        # 取数量：优先「最新N」（兼容 篇/条/篇文章，支持中文数字 十/二十…），其次「N条」
        lm = re.search(r"最新\s*([0-9零一二两三四五六七八九十百]+)", text) or re.search(r"([0-9零一二两三四五六七八九十百]+)\s*条", text)
        limit = _cn_to_int(lm.group(1)) if lm else 10
        if not company:
            return None
        # 子目录：指令含「目录下 / 子目录 / 新建 / 自建」时提取（如「自建个7.22目录」）
        subdir = None
        if re.search(r"目录下|子目录|新建|自建", text):
            dm = re.search(r"(?<!\d)(\d{1,2}[./]\d{1,2}|\d+\.\d+)(?!\d)", text)
            if dm:
                subdir = dm.group(1)
            else:
                bm = re.search(r"(?:自建个?|新建|建)\s*([^\s,，。目录]+?)\s*目录", text)
                subdir = bm.group(1) if bm else None
        return {"dir": "A", "company": company, "node": node,
                "limit": limit, "subdir": subdir}

    # ── 方向 B（原有）──
    if not node:
        return None
    subdirs = None
    if re.search(r"下面|目录下|子目录", text):
        ds = re.findall(r"(?<!\d)(\d{1,2}[/.]\d{1,2}|\d+\.\d+)(?!\d)", text)
        if ds:
            subdirs = ",".join(ds)

    right = text.split("到")[-1].strip()
    parts = right.split()
    if len(parts) < 2:
        return None
    pkg = parts[-1]
    company = " ".join(parts[:-1])
    return {"dir": "B", "node": node, "subdirs": subdirs,
            "company": company, "pkg": pkg}


def parse_g(text):
    """方向 G 解析：生文 <公司> <词包> <数量> [粘贴到<目录>] [填到<表格>]。

    返回 {"dir":"G","company","pkg","count","node","dst"} 或 None。
    链接用「粘贴到 / 填到」关键字就近匹配，不再要求紧贴（允许中间有空格）。
    """
    if not ("生文" in text or ("生成" in text and "文章" in text)):
        return None
    node = dst = None
    if "粘贴到" in text:
        m = re.search(r"粘贴到[^\n]*?wiki/([A-Za-z0-9]+)", text)
        if m:
            node = m.group(1)
    if "填到" in text:
        m = re.search(r"填到[^\n]*?wiki/([A-Za-z0-9]+)", text)
        if m:
            dst = m.group(1)
    # 去掉链接与 @提及，只留自然语言参数
    tail = text
    tail = re.sub(r"https?://\S+", " ", tail)
    tail = re.sub(r"\S*wiki/[A-Za-z0-9]+", " ", tail)
    tail = re.sub(r"@_\w+", " ", tail)
    tail = re.sub(r"（[^）]*）|\([^)]*\)", " ", tail)  # 去括号标注
    tail = tail.replace("生文", " ").replace("生成文章", " ").replace("生成", " ")
    tail = tail.replace("粘贴到", " ").replace("填到", " ")
    tail = re.sub(r"\s+", " ", tail).strip(" 的到和、，。：: ")
    # 取数量（第一个数字）
    cm = re.search(r"(\d+)", tail)
    count = int(cm.group(1)) if cm else 10
    if cm:
        tail = (tail[:cm.start()] + " " + tail[cm.end():]).strip()
    tail = re.sub(r"(篇|条|篇文章|篇文)\s*$", "", tail).strip()
    parts = tail.split()
    if len(parts) >= 2:
        pkg = parts[-1]
        company = " ".join(parts[:-1])
    elif len(parts) == 1:
        company, pkg = parts[0], None
    else:
        company = pkg = None
    if not company or not pkg:
        return None
    return {"dir": "G", "company": company, "pkg": pkg,
            "count": count, "node": node, "dst": dst}


def do_message(data: P2ImMessageReceiveV1) -> None:
    """事件回调：飞书在群里收到消息时触发，必须在 3 秒内返回。"""
    try:
        msg = data.event.message
        mtype = getattr(msg, "message_type", "")
        mentions = getattr(msg, "mentions", None) or []
        content = getattr(msg, "content", "") or ""
        chat_id = getattr(msg, "chat_id", "")
        print(f"[收到消息] type={mtype} chat_id={chat_id} "
              f"mentions={len(mentions)} content={content[:200]}", flush=True)
        _write_heartbeat()
        if mtype != "text":
            print("  → 跳过：非文本消息", flush=True)
            return
        if not mentions:
            print("  → 跳过：未 @机器人（群里需 @ 才响应）", flush=True)
            return
        try:
            text = json.loads(content).get("text", "")
        except Exception:
            text = content
        clean = re.sub(r"@_\w+", "", text).strip()
        print(f"  → 清洗后指令: {clean!r}", flush=True)

        # ── 更新/重启机器人（独立处理，不进 parse_command）──
        if any(k in clean for k in ("更新机器人", "更新", "升级", "重启机器人", "重启")):
            print("  → 触发：更新并重启机器人", flush=True)
            threading.Thread(target=process_update, args=(chat_id,),
                             daemon=True).start()
            return

        # 简写格式（client_map 公司名+数量）也视为有效动作关键字
        has_cm = any(cname in clean for cname in _CLIENT_MAP)
        if "粘贴" not in clean and not ("填" in clean and ("链接" in clean or "表" in clean)) \
                and not ("生文" in clean or "生成" in clean) \
                and not has_cm:
            print("  → 跳过：不含可识别动作关键字", flush=True)
            if chat_id:
                send_text(chat_id, "👋 我在，但没识别出动作关键字。\n"
                                   "可用指令：粘贴 / 填链接 / 生文 / 更新。\n"
                                   "发「帮助」查看完整说明。")
            return
        if not chat_id:
            print("  → 跳过：无 chat_id", flush=True)
            return

        parsed = parse_command(clean)
        print(f"  → 解析结果: {parsed}", flush=True)
        if not parsed:
            send_text(chat_id, "⚠️ 没看懂指令。\n" + HELP)
            return
        # 解析成功 → 丢到后台线程执行，立即返回（满足 3 秒限制）
        if parsed["dir"] == "A":
            threading.Thread(target=process_a, args=(chat_id, parsed),
                             daemon=True).start()
        elif parsed["dir"] == "F":
            threading.Thread(target=process_fill, args=(chat_id, parsed),
                             daemon=True).start()
        elif parsed["dir"] == "G":
            threading.Thread(target=process_g, args=(chat_id, parsed),
                             daemon=True).start()
        else:
            threading.Thread(target=process, args=(chat_id, parsed),
                             daemon=True).start()
    except Exception as e:
        import traceback
        print("handler 异常:", e, flush=True)
        traceback.print_exc()
    return


def process(chat_id, parsed):
    """后台执行方向 B（飞书→CMS）粘贴任务，并通过飞书群回报进度。"""
    if not _paste_lock.acquire(False):
        send_text(chat_id, "⏳ 上一条任务还在处理中，请稍候…")
        return
    try:
        node, subdirs, company, pkg = parsed["node"], parsed.get("subdirs"), \
            parsed["company"], parsed["pkg"]
        send_text(chat_id, f"✅ 收到指令（方向B：飞书→CMS），开始处理：\n飞书节点：{node}"
                           + (f"\n子目录：{subdirs}" if subdirs else "")
                           + f"\n公司：{company}\n词包：{pkg}")
        send_text(chat_id, "🔍 正在读取飞书文档…")
        out_articles = os.path.join(HERE, "bot_articles.json")
        articles = feishu_collect(node, subdirs, out_articles)
        if not articles:
            send_text(chat_id, "⚠️ 没读到任何文章，请检查目录链接 / 子目录名是否正确")
            return
        send_text(chat_id, f"📄 已读取 {len(articles)} 篇，准备写入并逐篇校验…")

        def log_fn(s):
            if any(k in s for k in ("✅ 全部正确落库", "❌", "⚠️ 探路失败", "⚠️ 有异常")):
                send_text(chat_id, s)

        res = run_pipeline(company, pkg, out_articles,
                           os.path.join(HERE, "bot_cms_results.json"), log_fn=log_fn)
        if res and res.get("bad", 0) == 0:
            send_text(chat_id, f"🎉 完成！{res['ok']} 篇已粘贴到「{company} / {pkg}」")
        elif res:
            send_text(chat_id, f"⚠️ 完成但有异常：成功 {res['ok']} / 异常 {res['bad']}，"
                               f"请检查 bot_cms_results.json")
        else:
            send_text(chat_id, "❌ 任务未成功完成，请查看运行日志")
    except Exception as e:
        send_text(chat_id, f"❌ 执行出错：{e}")
    finally:
        _paste_lock.release()


def process_fill(chat_id, parsed):
    """后台执行方向 F（目录文章链接 → 飞书表格某列），并通过群回报进度。"""
    if not _paste_lock.acquire(False):
        send_text(chat_id, "⏳ 上一条任务还在处理中，请稍候…")
        return
    try:
        from fill_sheet import run_fill
        src, dst, col, sheet = parsed["src"], parsed["dst"], parsed["col"], parsed.get("sheet")
        token = get_token()
        space = get_node_space(token, src) or "7630734017544981692"
        items = list_nodes(token, space, src)
        urls = [f"https://vcnd134o0gra.feishu.cn/wiki/{it['node_token']}"
                for it in items if (it.get("obj_type") or "") == "docx"]
        if not urls:
            send_text(chat_id, "⚠️ 源目录没有文章(docx)子节点，无法填表")
            return
        send_text(chat_id, f"✅ 收到指令（填表）：从目录 {src} 取到 {len(urls)} 条文章链接，"
                           f"准备写入表格 {dst} 第{col}列…")
        r = run_fill(dst, urls, col=col, sheet_hint=sheet, token=token)
        if r.get("ok"):
            send_text(chat_id, f"🎉 已填入 {r['count']} 条链接\n"
                               f"表格：{r['title']}｜工作表：{r['sheet']}｜范围：{r['range']}")
        else:
            send_text(chat_id, f"⚠️ 填表失败：{r.get('error')}")
    except Exception as e:
        send_text(chat_id, f"❌ 执行出错：{e}")
    finally:
        _paste_lock.release()


def process_a(chat_id, parsed):
    """后台执行方向 A（CMS→飞书）任务，并通过飞书群回报进度。"""
    if not _paste_lock.acquire(False):
        send_text(chat_id, "⏳ 上一条任务还在处理中，请稍候…")
        return
    try:
        company, node, limit = parsed["company"], parsed["node"], parsed["limit"]
        subdir = parsed.get("subdir")
        # 前置检查：目标节点必须能贴文章页（docx/folder），不能是表格/多维表
        try:
            tkn = get_token()
            nd = requests.get("https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
                headers={"Authorization": f"Bearer {tkn}"},
                params={"token": node, "token_type": "wiki"}, timeout=10).json()
            ot = (nd.get("data") or {}).get("node", {}).get("obj_type")
            if ot in ("sheet", "bitable", "mindnote"):
                send_text(chat_id, f"⚠️ 链接 {node} 是「{ot}」类型（电子表格/多维表），"
                                   f"不能贴文章页。请把文章贴到文件夹(docx/folder)链接；"
                                   f"表格链接仅用于「填链接」。")
                return
        except Exception as e:
            send_text(chat_id, f"⚠️ 无法识别目标节点类型：{e}")
            return
        send_text(chat_id, f"✅ 收到指令（方向A：CMS→飞书），开始处理：\n"
                           f"公司：{company}\n飞书目录：{node}\n最新：{limit} 条"
                           + (f"\n子目录：{subdir}" if subdir else ""))
        send_text(chat_id, "🔍 正在登录 huiyouhua 并定位公司（同名多公司时自动选有文章的）…")

        def log_fn(s):
            # 把关键进度/结果回发群
            if any(k in s for k in ("✅ 选用", "📄 取出", "✅ 方向 A 完成",
                                    "❌", "⚠️", "成功", "失败")):
                send_text(chat_id, s)

        res = run_pipeline_a(company, node, limit, log_fn=log_fn,
                             out_file=os.path.join(HERE, "bot_direction_a_results.json"),
                             subdir=subdir)
        if res and res.get("bad", 0) == 0:
            urls = [u for u in res.get("urls", []) if u]
            send_text(chat_id, f"🎉 完成！{res['ok']} 篇已从「{company}」写入飞书目录：\n"
                               + "\n".join(urls))
            # ── 自动填表（client_map 里有审核表时）──
            sheet_node = parsed.get("sheet")
            if sheet_node and urls:
                try:
                    from fill_sheet import run_fill
                    date_label = parsed.get("subdir") or time.strftime("%m.%d").lstrip("0")
                    # 转成「M月D日」格式
                    parts = date_label.replace("/", ".").split(".")
                    if len(parts) == 2:
                        date_label = f"{int(parts[0])}月{int(parts[1])}日"
                    fre = run_fill(sheet_node, urls, col=2, date_label=date_label)
                    if fre.get("ok"):
                        send_text(chat_id, f"📋 审核表已更新：{fre.get('count',0)} 条 ({fre.get('range','')})，日期={date_label}")
                    else:
                        send_text(chat_id, f"⚠️ 审核表写入失败：{fre.get('error','未知')}")
                except Exception as fe:
                    send_text(chat_id, f"⚠️ 审核表写入出错：{fe}")
        elif res:
            send_text(chat_id, f"⚠️ 完成但有异常：成功 {res['ok']} / 异常 {res['bad']}，"
                               f"请检查 bot_direction_a_results.json")
        else:
            send_text(chat_id, "❌ 任务未成功完成，请查看运行日志")
    except Exception as e:
        send_text(chat_id, f"❌ 执行出错：{e}")
    finally:
        _paste_lock.release()


def process_g(chat_id, parsed):
    """后台执行方向 G（智能生文 → 粘贴飞书 → 填表），并通过飞书群回报进度。"""
    if not _paste_lock.acquire(False):
        send_text(chat_id, "⏳ 上一条任务还在处理中，请稍候…")
        return
    try:
        from shengwen_pipeline import run_shengwen_pipeline
        company, pkg, count = parsed["company"], parsed["pkg"], parsed["count"]
        node = parsed.get("node")
        dst = parsed.get("dst")
        send_text(chat_id, f"✅ 收到指令（智能生文→粘贴→填表）：\n"
                           f"公司：{company}\n词包：{pkg}\n数量：{count}"
                           + (f"\n粘贴到飞书目录：{node}" if node else "")
                           + (f"\n填到表格：{dst}" if dst else ""))
        # 进度回发：只挑关键节点，避免刷屏
        def log_fn(s):
            if any(k in s for k in ("✅", "⚠️", "❌", "🎉", "📡",
                                    "[1/5]", "[2/5]", "[3/5]", "[4/5]", "[5/5]",
                                    "完成", "拒绝")):
                send_text(chat_id, s)

        res = run_shengwen_pipeline(
            company=company, pkg_name=pkg, count=count,
            feishu_node_token=node, spreadsheet_url=dst,
            dry_run=False, log_fn=log_fn,
        )
        gen = res.get("generated_count", 0)
        if gen > 0:
            msg = f"🎉 生文完成！共生成 {gen} 篇"
            if node and res.get("step4_paste"):
                ok = res["step4_paste"].get("ok", 0)
                bad = res["step4_paste"].get("bad", 0)
                extra = f"，{bad} 篇失败" if bad else ""
                msg += (f"\n✅ 已粘贴到飞书目录 {node}（{ok} 篇成功{extra}；"
                        f"当天日期子目录已自动创建）")
            if dst and res.get("step5_spreadsheet"):
                msg += f"\n✅ 已填表 {dst}"
            send_text(chat_id, msg)
        elif res.get("task_created"):
            send_text(chat_id, "⚠️ 生文任务已提交但暂未取到新文章，"
                               "可稍后查飞书目录 / bot_console.log")
        else:
            send_text(chat_id, "⚠️ 生文流程未成功（可能解析公司/词包失败），"
                               "请查看 bot_console.log")
    except Exception as e:
        send_text(chat_id, f"❌ 执行出错：{e}")
    finally:
        _paste_lock.release()


def process_update(chat_id, parsed=None):
    """后台执行：git pull 最新代码 → 拉起新实例 → 自身退出（实现「飞书里更新机器人」）。"""
    try:
        import subprocess
        send_text(chat_id, "🔄 正在拉取最新代码（git pull）…")
        r = subprocess.run(["git", "pull", "origin", "main"], cwd=HERE,
                           capture_output=True, text=True, timeout=90)
        out = (r.stdout + r.stderr)[-1500:]
        send_text(chat_id, f"git pull 结果：\n{out}")
        # 用新进程替换自己（等当前进程退出后再抢单实例锁）
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.isfile(pyw):
            pyw = sys.executable
        send_text(chat_id, "♻️ 正在用新代码重启机器人…（旧实例即将退出）")
        subprocess.Popen([pyw, "-u", "feishu_bot.py", "--replace", "--wait-exit",
                          str(os.getpid())], cwd=HERE,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        time.sleep(1.5)
        os._exit(0)
    except Exception as e:
        send_text(chat_id, f"❌ 更新失败：{e}")


def main():
    _setup_logging()
    # 自更新场景：新实例先等旧实例退出，再抢单实例锁，避免被自己误判为重复
    args = sys.argv[1:]
    if "--wait-exit" in args:
        try:
            _wp = int(args[args.index("--wait-exit") + 1])
            _cnt = 0
            while psutil.pid_exists(_wp) and _cnt < 60:
                time.sleep(0.3)
                _cnt += 1
        except Exception:
            pass
    # 单实例保护（含僵尸自愈）：发现挡路实例时，若是 --replace 模式或对方已卡死，
    # 则杀掉它再接管，避免「僵尸占坑 → 新实例全被拒 → 死锁」。
    ok, other = _acquire_bot_lock()
    if not ok:
        if "--replace" in args or _other_is_stale(other):
            try:
                print(f"⚠️ 发现挡路实例(pid={other})，准备接管（kill）…", flush=True)
                _kill_pid(other)
                time.sleep(1.5)
                ok, other = _acquire_bot_lock()
            except Exception as e:
                print(f"接管失败：{e}", flush=True)
        if not ok:
            print("⚠️ 已有一个 feishu_bot.py 健康实例在运行，拒绝重复启动"
                  "（多实例会抢同一个 Chrome 调试端口导致任务错乱）。退出。")
            sys.exit(1)
    _write_heartbeat()
    event_handler = (lark.EventDispatcherHandler.builder("", "")
                     .register_p2_im_message_receive_v1(do_message)
                     .build())
    cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=event_handler, log_level=LOG_LEVEL)
    print(">>> 飞书长连接 bot 启动中…（pythonw 无窗口常驻；日志见 bot_console.log；Ctrl+C 或结束进程退出）")
    # 自愈：cli.start() 会因网络抖动/服务端踢线而提前返回或抛异常，
    # 这里循环重连，避免进程退出导致 bot 掉线（常驻/开机自启场景必需）。
    while True:
        _write_heartbeat()
        try:
            cli.start()
        except Exception as e:
            print(f"[WARN] cli.start 异常退出：{e!r}，5s 后重连…", flush=True)
        else:
            print("[INFO] cli.start 返回（连接已断开），5s 后重连…", flush=True)
        _write_heartbeat()
        time.sleep(5)


if __name__ == "__main__":
    main()
