#!/usr/bin/env python3
"""
PR 提案生成工具 — 把巡检发现转成可审批的 PR 内容

用法：
  python3 tools/propose.py                 # 读 reports/patrol-latest.json
  python3 tools/propose.py --report X.json
  python3 tools/propose.py --print-only    # 只打印 PR 正文，不改任何文件

产出：
  data/policies.json      被标注为待核实的条目（不猜新值）
  .github/pr-body.md      PR 正文，含逐条核对清单与官方原文引句

设计取舍 —— 为什么不自动填新值：
  巡检能可靠判断「这个页面变了」「这个金额对不上」，但「变成了多少」需要读懂
  政策文件的上下文（分档表格、附件、口径说明）。让脚本猜新值，一旦猜错就是
  把错误金额直接推上线，用户拿着去申请会白跑。
  所以本工具只做两件事：
    1. 把受影响条目标为 unverified，前台立即显示「待核实」（保护用户）
    2. 在 PR 正文列出核对清单，由人打开原文填正确值（保证准确）
  合并 PR = 确认「已知晓这些条目有变动，且已按原文修正或暂标待核实」。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICIES = ROOT / "data" / "policies.json"
REPORT_DEFAULT = ROOT / "reports" / "patrol-latest.json"
PR_BODY = ROOT / ".github" / "pr-body.md"

CST = timezone(timedelta(hours=8))

SEV_LABEL = {"high": "🔴 高", "medium": "🟠 中", "low": "🟡 低", "info": "⚪"}
TYPE_LABEL = {
    "link_dead": "链接失效",
    "page_changed": "页面内容变动",
    "amount_mismatch": "额度疑似不一致",
    "window_mismatch": "申报窗口疑似变动",
    "stale": "核实已超期",
    "patrol_error": "巡检异常",
}
# 这些类型说明「数据可能已不准」，需把条目标为待核实以保护用户
NEEDS_FLAG = {"link_dead", "page_changed", "amount_mismatch", "window_mismatch", "stale"}


def today():
    return datetime.now(CST).strftime("%Y-%m-%d")


def find_policy(data, pid):
    for p in data["policies"]:
        if p["id"] == pid:
            return p
    return None


def flag_policy(p, findings_for_p):
    """把政策标为待核实，并把巡检发现写进 pending_items"""
    v = p.setdefault("verification", {})
    prev_status = v.get("status", "")
    if prev_status != "superseded":     # 已废止的不覆盖
        v["status"] = "unverified"
    items = v.setdefault("pending_items", [])
    added = []
    for f in findings_for_p:
        note = (f"[{today()} 巡检] {TYPE_LABEL.get(f['type'], f['type'])}"
                f"{('·' + f['field']) if f.get('field') else ''}：{f['detail']}")
        if note not in items:
            items.append(note)
            added.append(note)
    return prev_status, added


def build_pr_body(report, grouped, flagged):
    stamp = report.get("stamp", "")
    scope = report.get("scope", "all")
    findings = report.get("findings", [])
    highs = [f for f in findings if f["severity"] == "high"]

    L = []
    L += [
        f"## 政策巡检发现变更 · {stamp}",
        "",
        f"巡检范围 `{scope}`　|　发现 **{len(findings)}** 条待确认　|　"
        f"🔴 高 {len(highs)}　|　请求 {report.get('requests_made','?')} 次",
        "",
    ]
    if report.get("aborted"):
        L += ["> ⚠️ **本次巡检被安全阀提前中止，结果不完整。**"
              "可能被限流或网络异常，建议手动重跑确认。", ""]

    L += [
        "### 这个 PR 做了什么",
        "",
        "自动巡检发现下列政策的官方来源页面发生变动。**本 PR 没有猜测新数值**，"
        "只把受影响条目标记为「待核实」，让前台立即显示提示以保护用户。",
        "",
        "### 你需要做什么",
        "",
        "1. 逐条打开下面的官方链接，核对原文",
        "2. 若确有变动 → 直接在本 PR 里改 `data/policies.json` 填正确值，并把 "
        "`verification.status` 改回 `verified`、更新 `last_verified`",
        "3. 若是误报 → 从 `pending_items` 删掉对应条目，`status` 改回 `verified`",
        "4. 处理完 **合并**；暂时没空处理也可以先合并（前台会显示待核实，比显示错误金额安全）",
        "5. 完全不认可 → **关闭 PR**，正式数据不受任何影响",
        "",
        "---",
        "",
        "### 逐条核对清单",
        "",
    ]

    for pid, items in grouped.items():
        name = items[0]["policy_name"]
        L += [f"#### `{pid}` {name}", ""]
        for f in items:
            L += [
                f"- [ ] **{SEV_LABEL.get(f['severity'],'')} "
                f"{TYPE_LABEL.get(f['type'], f['type'])}**"
                + (f"　字段 `{f['field']}`" if f.get("field") else ""),
                f"  - 数据表现值：`{f['old_value']}`",
                f"  - 巡检发现：{f['new_value']}",
                f"  - 置信度：`{f['confidence']}`"
                + ("　（低置信度多为脚本抽取局限，常见于分档表格，请以原文为准）"
                   if f["confidence"] == "low" else ""),
                f"  - 官方来源：{f['url']}",
            ]
            if f.get("quote"):
                L += [f"  - 页面片段：`{f['quote'][:120]}`"]
            L += [f"  - 建议：{f['suggested_action']}", ""]
        L += [""]

    if flagged:
        L += [
            "---",
            "",
            "### 本 PR 的数据改动",
            "",
            "以下条目被标为 `unverified`，前台将显示「待核实」标记：",
            "",
        ]
        for pid, name, prev in flagged:
            L += [f"- `{pid}` {name}　（原状态：`{prev or '未设置'}` → `unverified`）"]
        L += [""]

    L += [
        "---",
        "",
        "### 参考",
        "",
        "- 字段规范：`data/schema.md`",
        "- 待核实事项总表：`data/核实清单.md`",
        "- 维护手册：`docs/维护手册.md`",
        "",
        "关键申报窗口提醒：**个税补贴每年 1/1–3/31**（须先办个税汇算清缴）；"
        "**前海十二条每年 8 月**，窗口仅约两周。",
        "",
        "<sub>本 PR 由 `tools/propose.py` 自动生成。机器负责发现变化，人负责确认发版。</sub>",
    ]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="把巡检发现转成 PR 提案")
    ap.add_argument("--report", help="巡检报告 JSON，默认 reports/patrol-latest.json")
    ap.add_argument("--print-only", action="store_true",
                    help="只打印 PR 正文，不修改任何文件")
    args = ap.parse_args()

    rp = Path(args.report) if args.report else REPORT_DEFAULT
    if not rp.exists():
        print(f"找不到巡检报告：{rp}")
        print("请先运行：python3 tools/patrol.py")
        sys.exit(1)

    report = json.loads(rp.read_text(encoding="utf-8"))
    findings = report.get("findings", [])

    if not findings:
        print("巡检没有发现需处理的变更，无需生成 PR。")
        sys.exit(0)

    try:
        data = json.loads(POLICIES.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"错误：policies.json 格式有误 → {e}")
        sys.exit(2)

    # 按政策分组
    grouped = {}
    for f in findings:
        grouped.setdefault(f["policy_id"], []).append(f)

    # 标记需要提示用户的条目
    flagged = []
    for pid, items in grouped.items():
        need = [f for f in items if f["type"] in NEEDS_FLAG]
        if not need:
            continue
        p = find_policy(data, pid)
        if not p:
            print(f"警告：报告提到 {pid} 但数据表中不存在，已跳过")
            continue
        prev, added = flag_policy(p, need)
        if added or prev != "unverified":
            flagged.append((pid, p["name"], prev))

    body = build_pr_body(report, grouped, flagged)

    if args.print_only:
        print(body)
        return

    PR_BODY.parent.mkdir(parents=True, exist_ok=True)
    PR_BODY.write_text(body, encoding="utf-8")

    if flagged:
        data["meta"]["data_as_of"] = today()
        POLICIES.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    print(f"PR 正文已生成：{PR_BODY.relative_to(ROOT)}")
    print(f"涉及政策 {len(grouped)} 条，标为待核实 {len(flagged)} 条")
    for pid, name, prev in flagged:
        print(f"  · {pid} {name}（{prev or '未设置'} → unverified）")
    if not flagged:
        print("  （无需标记，均为提示类发现）")

    # 供 Actions 判断
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"flagged_count={len(flagged)}\n")
            fh.write(f"policy_count={len(grouped)}\n")
            title = (f"政策巡检：{len(grouped)} 条政策来源变动待核实"
                     f"（{report.get('stamp','')}）")
            fh.write(f"pr_title={title}\n")


if __name__ == "__main__":
    main()
