#!/usr/bin/env python3
"""
政策源巡检脚本 — 只发现变化，绝不修改数据

双模运行：
  · 云端（GitHub Actions 定时触发）：巡检后由 tools/propose.py 生成 PR
  · 本地（临时核查一条政策）：python3 tools/patrol.py --only <policy_id>

用法：
  python3 tools/patrol.py                # 全量巡检
  python3 tools/patrol.py --scope window # 仅巡检申报窗口（每周）
  python3 tools/patrol.py --scope terms  # 仅巡检条款与额度（每月）
  python3 tools/patrol.py --scope links  # 仅检查链接有效性（每季）
  python3 tools/patrol.py --only qh-employ-001   # 只查一条
  python3 tools/patrol.py --dry-run      # 不写快照，仅看结果
  python3 tools/patrol.py --max-requests 40      # 限制本次请求总数

产出：
  reports/patrol-YYYY-MM-DD-HHMM.md     人类可读的变更报告
  reports/patrol-latest.json            供 propose.py 消费（固定名，便于 CI）
  data/snapshots/<policy_id>__<hash>.txt 页面内容快照

铁律：本脚本对 data/policies.json 只读。任何变更都须经人工审批（本地 confirm.py 或云端 PR）。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICIES = ROOT / "data" / "policies.json"
SNAP_DIR = ROOT / "data" / "snapshots"
REPORT_DIR = ROOT / "reports"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 25
SLEEP_BETWEEN = 1.5   # 对政府站点保持克制，避免被限流

# 云端定时任务的安全阀：政府站点通常有防护，被限流甚至封 IP 都可能。
# 宁可这次少查几条，也不要把 IP 打进黑名单。
MAX_REQUESTS_DEFAULT = 80      # 单次巡检请求总量上限
MAX_CONSECUTIVE_FAILS = 6      # 连续失败达此数即中止，不硬重试

CST = timezone(timedelta(hours=8))

# 运行期计数器
_stats = {"requests": 0, "consecutive_fails": 0, "aborted": False, "max_requests": MAX_REQUESTS_DEFAULT}


# ---------- 工具函数 ----------

def now_cst():
    return datetime.now(CST)


def log(msg):
    print(f"[{now_cst().strftime('%H:%M:%S')}] {msg}", flush=True)


def _decode(raw):
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _fetch_curl(url):
    """
    主抓取通道：curl。

    为什么不用 Python urllib 作主通道：
    部分深圳政府站点（qh.sz.gov.cn / szfb.sz.gov.cn / hrss.sz.gov.cn）的 TLS
    椭圆曲线配置与 OpenSSL 3.x 协商失败，报 [SSL: BAD_ECPOINT]，而 curl
    （macOS 走 LibreSSL）可正常返回 200。若用 urllib 作主通道，会把这些
    活链接误报为「链接失效」——这种误报会让维护者误以为政策已下架，代价很大。
    """
    if not shutil.which("curl"):
        return None
    cmd = [
        "curl", "-sS", "-L", "--compressed",
        "--max-time", str(TIMEOUT),
        "-A", UA,
        "-H", "Accept-Language: zh-CN,zh;q=0.9",
        "-w", "\n__HTTP_STATUS__%{http_code}",
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 10)
    except subprocess.TimeoutExpired:
        return (False, None, "", "curl 超时")
    body = _decode(r.stdout)
    m = re.search(r"__HTTP_STATUS__(\d{3})\s*$", body)
    if not m:
        err = _decode(r.stderr).strip() or "curl 未返回状态码"
        return (False, None, "", f"curl 失败：{err[:160]}")
    status = int(m.group(1))
    body = body[:m.start()]
    if 200 <= status < 300:
        return (True, status, body, None)
    return (False, status, "", f"HTTP {status}")


def _fetch_urllib(url):
    """兜底通道：Python urllib，SSL 放宽以尽量兼容旧站点"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except ssl.SSLError:
        pass
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            return True, r.status, _decode(r.read()), None
    except urllib.error.HTTPError as e:
        return False, e.code, "", f"HTTP {e.code}"
    except Exception as e:
        return False, None, "", f"{type(e).__name__}: {e}"


def fetch(url):
    """
    返回 (ok, status, text, error)

    双通道：先 curl，失败再试 urllib。只有两者都失败才判定链接失效，
    避免把 TLS 兼容问题误报成政策下架。

    同时承担限流闸门职责：请求总量超限或连续失败过多即中止后续抓取。
    云端定时任务无人看守，这道闸门防止把政府站点打到限流或封 IP。
    """
    if _stats["aborted"]:
        return False, None, "", "已触发安全中止，跳过本次请求"

    if _stats["requests"] >= _stats["max_requests"]:
        _stats["aborted"] = True
        log(f"!! 已达请求上限 {_stats['max_requests']}，中止后续抓取")
        return False, None, "", f"达到请求上限 {_stats['max_requests']}"

    _stats["requests"] += 1

    res = _fetch_curl(url)
    if res and res[0]:
        _stats["consecutive_fails"] = 0
        return res

    fallback = _fetch_urllib(url)
    if fallback[0]:
        _stats["consecutive_fails"] = 0
        return fallback

    # 两个通道都失败
    _stats["consecutive_fails"] += 1
    if _stats["consecutive_fails"] >= MAX_CONSECUTIVE_FAILS:
        _stats["aborted"] = True
        log(f"!! 连续 {_stats['consecutive_fails']} 次抓取失败，中止巡检"
            f"（可能被限流或网络异常，不做硬重试）")

    curl_err = res[3] if res else "curl 不可用"
    return False, fallback[1], "", f"curl: {curl_err} / urllib: {fallback[3]}"


def html_to_text(html):
    """粗提取正文，用于内容比对"""
    h = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?s)<[^>]+>", "\n", h)
    h = re.sub(r"&nbsp;?", " ", h)
    h = re.sub(r"&amp;", "&", h)
    h = re.sub(r"&lt;", "<", h)
    h = re.sub(r"&gt;", ">", h)
    h = re.sub(r"[ \t\u3000]+", " ", h)
    lines = [ln.strip() for ln in h.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def snap_path(policy_id, url):
    key = hashlib.sha1(url.encode()).hexdigest()[:10]
    return SNAP_DIR / f"{policy_id}__{key}.txt"


def extract_amounts(text):
    """
    抽取金额型数字，用于比对额度是否变动。

    需同时覆盖三种写法，否则会误报：
      「600元」→ 600
      「500万元」→ 5000000
      「10万元」→ 100000
    还要把带小数的万元写法（如「1.5万元」）算进来。
    """
    found = set()

    # 万元（含小数）：500万元 / 1.5万元
    for m in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*万\s*元", text):
        try:
            v = int(float(m.group(1)) * 10000)
        except ValueError:
            continue
        if 100 <= v <= 100_000_000:
            found.add(v)

    # 元（含千分位）：600元 / 10,000元
    for m in re.finditer(r"([0-9][0-9,，]{0,12})\s*元", text):
        num = m.group(1).replace(",", "").replace("，", "")
        if not num.isdigit():
            continue
        v = int(num)
        if 100 <= v <= 100_000_000:
            found.add(v)

    # 裸数字兜底：分档表格里常见「博士8000 硕士4000」这类无单位写法
    for m in re.finditer(r"(?<![0-9./-])([0-9]{3,9})(?![0-9./-])", text):
        v = int(m.group(1))
        if 100 <= v <= 100_000_000:
            found.add(v)

    return found


def extract_dates(text):
    """抽取日期，用于比对申报窗口"""
    out = set()
    for m in re.finditer(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text):
        out.add(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")
    for m in re.finditer(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text):
        out.add(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")
    return out


def diff_ratio(a, b):
    """粗略差异比例：基于行集合"""
    sa, sb = set(a.split("\n")), set(b.split("\n"))
    if not sa and not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return 1.0 - (inter / union if union else 1.0)


# ---------- 巡检核心 ----------

def patrol_policy(p, scope, dry_run):
    """
    对单条政策做检查，返回 findings 列表。

    设计要点：额度与窗口比对采用「跨来源合并」策略。
    一条政策常有多个来源（如省厅细则 + 市局专区），各来源承载的信息不同：
    省级细则写省标准 300 元，市级页面写深圳标准 600 元。若逐个来源比对全部字段，
    必然互相误报。因此先汇总所有来源的金额与日期，再与数据表比对——
    只要任一官方来源印证了该数值，就不报警。
    """
    findings = []
    pid = p["id"]
    merged_amounts = set()
    merged_dates = set()
    fetched_any = False
    context_pool = []

    for src in p.get("sources", []):
        url = src.get("url")
        if not url:
            continue

        log(f"  检查 {pid} ← {url[:70]}")
        ok, status, html, err = fetch(url)
        time.sleep(SLEEP_BETWEEN)

        # --- 链接有效性 ---
        if not ok:
            findings.append({
                "policy_id": pid,
                "policy_name": p["name"],
                "type": "link_dead",
                "severity": "high" if src.get("is_primary") else "medium",
                "field": "sources.url",
                "old_value": url,
                "new_value": None,
                "detail": f"链接无法访问：{err}"
                          + ("（该来源为一手来源）" if src.get("is_primary") else "（该来源为参考来源）"),
                "quote": "",
                "url": url,
                "confidence": "high",
                "suggested_action": "官网可能改版或文件下架，需人工查找新地址；若政策已废止，将 verification.status 改为 superseded",
            })
            continue

        fetched_any = True
        text = html_to_text(html)
        merged_amounts |= extract_amounts(text)
        merged_dates |= extract_dates(text)
        context_pool.append(text)

        sp = snap_path(pid, url)
        old_text = sp.read_text(encoding="utf-8") if sp.exists() else None

        # --- 首次建立快照 ---
        if old_text is None:
            if not dry_run:
                sp.write_text(text, encoding="utf-8")
            findings.append({
                "policy_id": pid,
                "policy_name": p["name"],
                "type": "snapshot_created",
                "severity": "info",
                "field": "",
                "old_value": None,
                "new_value": None,
                "detail": "首次建立页面快照，本次不做差异比对",
                "quote": "",
                "url": url,
                "confidence": "high",
                "suggested_action": "无需操作",
            })
            continue

        # --- 内容差异（逐来源，因为快照是按来源存的）---
        if scope in ("all", "terms", "window"):
            ratio = diff_ratio(old_text, text)
            if ratio > 0.08:
                findings.append({
                    "policy_id": pid,
                    "policy_name": p["name"],
                    "type": "page_changed",
                    "severity": "high" if ratio > 0.25 else "medium",
                    "field": "page_content",
                    "old_value": f"快照 {sp.name}",
                    "new_value": f"差异约 {ratio:.0%}",
                    "detail": f"来源页面内容变动约 {ratio:.0%}，须人工打开原文核对政策条款是否调整",
                    "quote": "",
                    "url": url,
                    "confidence": "medium",
                    "suggested_action": "打开 URL 逐项核对：适用身份、申领条件、补贴额度、申报窗口",
                })

        if not dry_run:
            sp.write_text(text, encoding="utf-8")

    primary_url = next((s["url"] for s in p.get("sources", []) if s.get("is_primary")),
                       (p["sources"][0]["url"] if p.get("sources") else ""))
    all_text = "\n".join(context_pool)

    # --- 额度比对（跨来源合并后）---
    if scope in ("all", "terms") and fetched_any and merged_amounts:
        for label, val in iter_policy_amounts(p):
            if val and val not in merged_amounts:
                findings.append({
                    "policy_id": pid,
                    "policy_name": p["name"],
                    "type": "amount_mismatch",
                    "severity": "medium",
                    "field": f"benefit.{label}",
                    "old_value": val,
                    "new_value": "所有来源页面均未检出该金额",
                    "detail": (f"数据表记录 {label}={val}，但该政策的全部来源页面都未检出此金额。"
                               f"页面检出的金额有：{sorted(merged_amounts)[:14]}"),
                    "quote": find_context(all_text, [val]),
                    "url": primary_url,
                    "confidence": "low",
                    "suggested_action": "低置信度提示。页面可能用分档表格、「万元」或文字描述表述金额。请人工打开原文确认额度是否真的变动",
                })

    # --- 申报窗口比对（跨来源合并后）---
    if scope in ("all", "window") and fetched_any:
        w = p.get("application_window", {})
        if w.get("window_type") == "annual_batch" and merged_dates:
            missing = [k for k in ("start", "end", "actual_end_extended")
                       if w.get(k) and w[k] not in merged_dates]
            # 只有全部窗口日期都对不上，才认为可能换了年度指南
            declared = [k for k in ("start", "end", "actual_end_extended") if w.get(k)]
            if declared and len(missing) == len(declared):
                findings.append({
                    "policy_id": pid,
                    "policy_name": p["name"],
                    "type": "window_mismatch",
                    "severity": "high",
                    "field": "application_window",
                    "old_value": " / ".join(f"{k}={w[k]}" for k in declared),
                    "new_value": f"页面日期：{sorted(merged_dates)[:10]}",
                    "detail": ("数据表记录的申报窗口日期在所有来源页面中均未出现，"
                               "可能已发布新年度申报指南。错过申报窗口代价大，请优先核对。"),
                    "quote": find_context(all_text, ["申报时间", "申请时间", "受理"]),
                    "url": primary_url,
                    "confidence": "medium",
                    "suggested_action": "打开一手来源确认本年度申报时间。若已是新年度指南，更新 application_window 并在 benefit.year_history 追加上一年度记录",
                })

    # --- 核实时效 ---
    lv = p.get("verification", {}).get("last_verified")
    if lv:
        days = (now_cst().date() - datetime.strptime(lv, "%Y-%m-%d").date()).days
        if days > 180:
            findings.append({
                "policy_id": pid,
                "policy_name": p["name"],
                "type": "stale",
                "severity": "medium",
                "field": "verification.last_verified",
                "old_value": lv,
                "new_value": f"{days} 天前",
                "detail": f"该条政策已 {days} 天未核实，超过 180 天阈值，前台将显示降级提示",
                "quote": "",
                "url": primary_url,
                "confidence": "high",
                "suggested_action": "重新核对官方原文后更新 last_verified",
            })

    return findings


def iter_policy_amounts(p):
    """产出 (label, value) 供额度比对"""
    b = p.get("benefit", {})
    for k, v in (b.get("amounts") or {}).items():
        if isinstance(v, (int, float)) and v:
            yield f"amounts.{k}", int(v)
    if b.get("cap_total"):
        yield "cap_total", int(b["cap_total"])


def find_context(text, needles, span=90):
    """在文本中找关键词上下文，作为原文引句"""
    for n in needles:
        s = str(n)
        i = text.find(s)
        if i >= 0:
            a = max(0, i - span // 2)
            return text[a:a + span].replace("\n", " ").strip()
    return ""


# ---------- 报告输出 ----------

SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
SEV_LABEL = {"high": "🔴 高", "medium": "🟠 中", "low": "🟡 低", "info": "⚪ 提示"}
TYPE_LABEL = {
    "link_dead": "链接失效",
    "page_changed": "页面内容变动",
    "amount_mismatch": "额度疑似不一致",
    "window_mismatch": "申报窗口疑似变动",
    "stale": "核实已超期",
    "snapshot_created": "首次建立快照",
}


def detect_network_failure(findings, total_sources):
    """
    区分「政策真下架」和「本机/本节点访问不了」。

    背景（2026-08-20 实测）：在 GitHub Actions 境外节点跑巡检，
    22 条来源全部报 link_dead，但本地 curl 全返回 200。
    如果照单报成「链接失效」，会把一批已核实政策错误降级为待核实——
    噪音淹没真实信号，比不巡检更糟。

    判据：失效数 ≥ 3 且失效率 > 60%。单条挂掉是政策下架，
    集体挂掉是网络问题——政府网站不会同时下架所有文件。

    返回 (是否网络异常, 失效数, 失效率)
    """
    dead = [f for f in findings if f["type"] == "link_dead"]
    if not dead or not total_sources:
        return False, len(dead), 0.0
    ratio = len(dead) / total_sources
    return (len(dead) >= 3 and ratio > 0.6), len(dead), ratio


def write_report(findings, meta, scope, stamp, total_sources=0):
    REPORT_DIR.mkdir(exist_ok=True)
    md = REPORT_DIR / f"patrol-{stamp}.md"
    js = REPORT_DIR / f"patrol-{stamp}.json"

    # 先判定是否为网络环境问题。是的话把 link_dead 全部改判，
    # 避免下游 propose.py 把这些条目标成「待核实」污染正式数据。
    net_fail, dead_n, dead_ratio = detect_network_failure(findings, total_sources)
    if net_fail:
        for f in findings:
            if f["type"] == "link_dead":
                f["type"] = "network_unreachable"
                f["severity"] = "info"
                f["confidence"] = "high"
                f["detail"] = (
                    f"本次巡检有 {dead_n}/{total_sources}（{dead_ratio:.0%}）来源无法访问，"
                    "判定为网络环境问题，非政策下架。"
                    "政府网站不会同时下架全部文件。"
                )
                f["suggested_action"] = (
                    "不要据此修改政策数据。请在能正常访问国内政府站点的网络环境下重跑巡检"
                    "（本地 macOS 直接跑 tools/patrol.py 即可）。"
                )

    findings.sort(key=lambda f: (SEV_ORDER.get(f["severity"], 9), f["policy_id"]))
    actionable = [f for f in findings
                  if f["type"] not in ("snapshot_created", "network_unreachable")]

    lines = [
        f"# 政策巡检报告 {stamp}",
        "",
        f"**巡检时间**：{now_cst().strftime('%Y-%m-%d %H:%M')} (CST)　|　"
        f"**范围**：{scope}　|　**数据版本**：{meta.get('data_version')}",
        "",
        "> ⚠️ **以上均为待确认项，未写入正式数据。**",
        "> 请逐条核对官方原文后，运行 `python3 tools/confirm.py` 决定采纳或驳回。",
        "",
        "---",
        "",
    ]

    if net_fail:
        lines += [
            "## 🌐 本次巡检判定为网络环境异常",
            "",
            f"有 **{dead_n}/{total_sources}（{dead_ratio:.0%}）** 来源无法访问。",
            "政府网站不会同时下架全部文件，因此判定为当前网络访问不到国内政府站点，"
            "**不是政策下架**。",
            "",
            "这些条目已改判为 `network_unreachable`，**不计入待处理项，不会影响政策数据**。",
            "",
            "**处理方式**：在能正常访问 `*.sz.gov.cn` 的网络环境下重跑（本机直接跑即可）：",
            "",
            "```bash",
            "python3 tools/patrol.py",
            "```",
            "",
            "---",
            "",
        ]

    lines += [
        "## 汇总",
        "",
        f"- 发现待处理项：**{len(actionable)}** 条",
        f"- 🔴 高：{sum(1 for f in actionable if f['severity']=='high')}　"
        f"🟠 中：{sum(1 for f in actionable if f['severity']=='medium')}　"
        f"🟡 低：{sum(1 for f in actionable if f['severity']=='low')}",
        f"- 新建快照：{sum(1 for f in findings if f['type']=='snapshot_created')} 条",
        "",
    ]

    dead = [f for f in actionable if f["type"] == "link_dead"]
    if dead:
        lines += ["## 🔴 链接失效清单（优先处理）", ""]
        for f in dead:
            lines += [f"- **{f['policy_name']}**（`{f['policy_id']}`）",
                      f"  - {f['url']}",
                      f"  - {f['detail']}",
                      f"  - 建议：{f['suggested_action']}", ""]

    others = [f for f in actionable if f["type"] != "link_dead"]
    if others:
        lines += ["## 疑似变更明细", ""]
        for i, f in enumerate(others, 1):
            lines += [
                f"### {i}. {SEV_LABEL.get(f['severity'],'')} {TYPE_LABEL.get(f['type'], f['type'])} — {f['policy_name']}",
                "",
                f"| 项 | 内容 |",
                f"|---|---|",
                f"| 政策 ID | `{f['policy_id']}` |",
                f"| 字段 | `{f['field']}` |",
                f"| 数据表现值 | {f['old_value']} |",
                f"| 巡检发现 | {f['new_value']} |",
                f"| 置信度 | {f['confidence']} |",
                f"| 来源 | {f['url']} |",
                "",
                f"**说明**：{f['detail']}",
                "",
            ]
            if f["quote"]:
                lines += [f"**页面片段**：`{f['quote']}`", ""]
            lines += [f"**建议动作**：{f['suggested_action']}", "", "---", ""]

    if not actionable:
        lines += ["## 结果", "", "本次巡检未发现需处理的变更。", ""]

    lines += [
        "## 下一步",
        "",
        "**云端**：本报告若在 GitHub Actions 中生成，会自动开一个 PR，"
        "在手机上点开原文核对后合并即生效。",
        "",
        "**本地**：",
        "```bash",
        "python3 tools/confirm.py --list     # 仅查看待确认项",
        "python3 tools/confirm.py            # 逐条确认变更",
        "```",
        "",
        "确认完成后会生成新版本数据文件，并自动写入 `data/CHANGELOG.md`。",
    ]

    md.write_text("\n".join(lines), encoding="utf-8")
    payload = {
        "stamp": stamp,
        "scope": scope,
        "generated_at": now_cst().isoformat(),
        "data_version": meta.get("data_version"),
        "requests_made": _stats["requests"],
        "aborted": _stats["aborted"],
        "findings": actionable,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    js.write_text(body, encoding="utf-8")
    # 固定名副本，供 CI 与 propose.py 消费，避免在工作流里拼时间戳
    (REPORT_DIR / "patrol-latest.json").write_text(body, encoding="utf-8")
    (REPORT_DIR / "patrol-latest.md").write_text("\n".join(lines), encoding="utf-8")

    return md, js, len(actionable)


# ---------- 入口 ----------

def gha_output(key, value):
    """写入 GitHub Actions step output，供后续 step 判断是否需要开 PR"""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def main():
    ap = argparse.ArgumentParser(description="政策源巡检（只报告，不改数据）")
    ap.add_argument("--scope", default="all",
                    choices=["all", "window", "terms", "links"],
                    help="all=全量 window=申报窗口 terms=条款额度 links=仅链接")
    ap.add_argument("--dry-run", action="store_true", help="不写快照")
    ap.add_argument("--only", help="仅巡检指定 policy_id，逗号分隔")
    ap.add_argument("--max-requests", type=int, default=MAX_REQUESTS_DEFAULT,
                    help=f"本次巡检请求总量上限（默认 {MAX_REQUESTS_DEFAULT}）")
    args = ap.parse_args()

    _stats["max_requests"] = args.max_requests

    if not POLICIES.exists():
        log(f"错误：找不到 {POLICIES}")
        sys.exit(1)

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        data = json.loads(POLICIES.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log(f"错误：policies.json 格式有误 → {e}")
        log("巡检中止。请先修复数据文件格式。")
        gha_output("status", "data_invalid")
        sys.exit(2)

    policies = data["policies"]
    if args.only:
        keep = {x.strip() for x in args.only.split(",")}
        policies = [p for p in policies if p["id"] in keep]
        if not policies:
            log(f"错误：--only 指定的 id 都不存在：{args.only}")
            sys.exit(1)

    log(f"开始巡检：{len(policies)} 条政策，范围={args.scope}，"
        f"请求上限={args.max_requests}")
    log("提醒：本脚本对 policies.json 只读，不会修改任何政策数据")

    all_findings = []
    failed = 0
    for p in policies:
        if _stats["aborted"]:
            log(f"[跳过] {p['id']}（已触发安全中止）")
            continue
        log(f"[{p['id']}] {p['name']}")
        try:
            all_findings += patrol_policy(p, args.scope, args.dry_run)
        except Exception as e:
            failed += 1
            log(f"  !! 巡检异常：{type(e).__name__}: {e}")
            all_findings.append({
                "policy_id": p["id"], "policy_name": p["name"],
                "type": "patrol_error", "severity": "high",
                "field": "", "old_value": None, "new_value": None,
                "detail": f"巡检该条时发生异常：{type(e).__name__}: {e}",
                "quote": "", "url": "", "confidence": "high",
                "suggested_action": "检查网络或脚本，重跑 --only " + p["id"],
            })

    stamp = now_cst().strftime("%Y-%m-%d-%H%M")
    # 分母必须是「实际尝试访问的次数」，不是「本该访问的来源总数」。
    # 安全阀中止时后者会虚高，导致失效率被低估、判不出网络异常。
    attempted = max(_stats["requests"], 1)
    md, js, n = write_report(all_findings, data["meta"], args.scope, stamp,
                             total_sources=attempted)

    log("")
    log(f"巡检完成：待处理 {n} 条，异常 {failed} 条，"
        f"实际请求 {_stats['requests']} 次")
    log(f"报告：{md.relative_to(ROOT)}")
    log(f"数据：{js.relative_to(ROOT)}")
    if _stats["aborted"]:
        log("!! 本次巡检因安全阀提前中止，结果不完整")
    if n:
        log("下一步：python3 tools/confirm.py（本地）或等 CI 自动开 PR（云端）")

    gha_output("findings_count", n)
    gha_output("scope", args.scope)
    gha_output("stamp", stamp)
    gha_output("aborted", "true" if _stats["aborted"] else "false")
    gha_output("has_changes", "true" if n else "false")
    gha_output("status", "aborted" if _stats["aborted"]
               else ("error" if failed else "ok"))

    # 巡检失败要显式非零退出，便于定时任务捕获并通知
    if failed:
        log("!! 存在巡检异常，退出码 3（定时任务应捕获此状态并通知维护者）")
        sys.exit(3)
    if _stats["aborted"]:
        log("!! 安全阀中止，退出码 4")
        sys.exit(4)


if __name__ == "__main__":
    main()
