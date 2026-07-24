# 飞书 Wiki 批量粘贴 (feishu-wiki-paste)

> WorkBuddy Skill — 在「飞书知识库 Wiki」与「huiyouhua CMS」之间批量迁移文章，并一键回填飞书审核表。

## 它能做什么

支持 4 个方向，在飞书里给机器人发指令即可触发（也支持对话内直接驱动脚本）：

| 方向 | 含义 | 触发示例 |
|---|---|---|
| **A** | CMS 已审核文章 → 飞书 Wiki 目录（自动建当日日期子目录）+ 回填审核表 | `中泰期货 30篇` |
| **G** | 智能生文：调 huiyouhua 后端接口生文 → 粘贴飞书 + 填表 | （机器人指令） |
| **B** | 飞书 Wiki 文章 → 回贴 CMS 词包 | `https://...wiki/XXX 粘贴回约牛 约牛软件词包下` |
| **F** | 把飞书目录下的文章链接批量填入审核表 | `把 https://.../AvgPws... 下的文章填到约牛审核表` |

## 快速开始

### 1. 凭证（本地，不入库）
复制 `.env.example` 为 `.env`，填入：
```
FEISHU_APP_ID=cli_xxxx
FEISHU_APP_SECRET=xxxx
HYH_USER=运营账号
HYH_PWD=运营密码
```
> ⚠️ `.env` 已被 `.gitignore` 排除，真实凭证**永不**进仓库。提交前 `sync.sh` 会扫描疑似真实凭证并中止提交。

### 2. 客户映射（client_map.json）
简写指令（如 `中泰期货 30篇`）依赖它。每客户配「飞书顶层目录节点 + 审核表节点」：
```json
{
  "中泰期货": { "dir_node": "ZChcwfF...", "sheet_node": "DUXVwK..." },
  "约牛":     { "dir_node": "QucCwt...", "sheet_node": "PI5owc..." }
}
```
加新客户：直接告诉我「公司名 + 文章目录链接 + 审核表链接」，我会写入并同步仓库。

### 3. 启动机器人（本机，非沙箱）
双击桌面「启动飞书机器人」（`start_bot.bat` 的镜像）即可常驻；连上飞书后等待指令。

## 常用命令（对话内 / 脚本）

- 方向 A 一键：`python run_client.py <公司名> [数量] [--subdir X] [--dry-run]`
- 方向 B 一键：`python run_direction_b.py --node <飞书node> --company <公司> --pkg <词包>`
- 查公司 / 词包 ID：`python cms_discover_ids.py --company '公司全名' --pkg '词包名'`
- 启动调试版：`start_bot_debug.bat`

## 目录结构

**核心流程**
- `feishu_bot.py` — 机器人主程序（解析指令、分发四向）
- `direction_a_cms_to_feishu.py` — 方向 A（CMS → 飞书）
- `auto_paste.py` / `direction_b_cms_write.py` — 方向 B（飞书 → CMS 词包）
- `shengwen_pipeline.py` — 方向 G（智能生文）
- `fill_sheet.py` — 回填飞书审核表
- `run_client.py` / `run_direction_b.py` — 通用一键驱动
- `client_map.json` — 客户映射

**辅助工具**
- `cms_discover_ids.py`（查 corp_id / pkg_id）、`collect_d5nl_links.py`（收目录链接）、`prepare_multi.py`、`paste_utils.py`（HTML→飞书块）、`launch_chrome_debug.py`

**配置 / 文档**
- `SKILL.md`（完整 SOP）、本文件、`start_bot.bat`、`sync.sh`、`.gitignore`、`.env.example`

> 仓库还保留少量早期 / 调试脚本（如 `direction_b_run.py`、`run_shengwen.bat`、`start_bot_debug.bat`、`paste_yn_missing.py`），已被新版通用驱动取代，日常使用可忽略。
>
> 运行产生的临时文件（`*_articles.json`、`*_state.json`、`bot_*.json`、`*.log`、`__pycache__` 等）已被 `.gitignore` 排除，不会入库。

## 依赖
```
requests  lark_oapi  psutil  playwright
```
机器人用基础 Python 运行，依赖需装到该 Python 下。

## License
MIT
