#!/usr/bin/env python3
"""
变更确认与版本发布工具 — 人工确认关卡

用法：
  python3 tools/confirm.py              # 逐条确认最新巡检报告
  python3 tools/confirm.py --list       # 只看待确认项，不做决定
  python3 tools/confirm.py --report X   # 指定报告文件
  python3 tools/confirm.py --rollback   # 回滚到上一版本
  python3 tools/confirm.py --versions   # 列出所有历史版本

铁律：
  1. 未经确认的变更，一条都不会写入 policies.json
  2. 每次发版前自动备份当前版本到 data/versions/
  3. 发版后自动追加 data/CHANGELOG.md
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICIES = ROOT / "data" / "policies.json"
VERSIONS = ROOT / "data" / "versions"
CHANGELOG = ROOT / "data" / "CHANGELOG.md"
REPORT_DIR = ROOT / "reports"

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


def today():
    return datetime.now(CST).strftime("%Y-%m-%d")


def load_policies():
    """加载政策数据，格式错误时明确报错并中止"""
    try:
        return json.loads(POLICIES.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"错误：policies.json 格式有误 → {e}")
        print(f"请先修复，或运行 python3 tools/confirm.py --rollback 回滚")
        sys.exit(2)


def latest_report():
    files = sorted(REPORT_DIR.glob("patrol-*.json"))
    return files[-1] if files else None


def bump(version, level="patch"):
    try:
        a, b, c = (int(x) for x in version.split("."))
    except Exception:
        return "1.0.1"
    if level == "major":
        return f"{a+1}.0.0"
    if level == "minor":
        return f"{a}.{b+1}.0"
    return f"{a}.{b}.{c+1}"


def find_policy(data, pid):
    for p in data["policies"]:
        if p["id"] == pid:
            return p
    return None


def show_finding(i, total, f):
    print("\n" + "=" * 68)
    print(f"[{i}/{total}]  {SEV_LABEL.get(f['severity'],'')}  "
          f"{TYPE_LABEL.get(f['type'], f['type'])}")
    print("=" * 68)
    print(f"政策　　: {f['policy_name']}  ({f['policy_id']})")
    print(f"字段　　: {f['field'] or '（整体）'}")
    print(f"现有值　: {f['old_value']}")
    print(f"巡检发现: {f['new_value']}")
    print(f"置信度　: {f['confidence']}")
    print(f"来源　　: {f['url']}")
    print(f"\n说明　　: {f['detail']}")
    if f.get("quote"):
        print(f"页面片段: {f['quote']}")
    print(f"\n建议动作: {f['suggested_action']}")
    print("-" * 68)
    print("请先打开上面的来源链接，核对官方原文，再做决定。")


def ask(prompt, default=""):
    """
    读取一行输入。遇到 EOF（脚本被误接管道、无终端环境）时返回默认值，
    而不是抛 EOFError 崩在半途——崩在写数据中途可能留下不一致状态。
    """
    try:
        return input(prompt).strip()
    except EOFError:
        print(f"\n[输入流已结束，按默认处理：{default or '取消'}]")
        return default


def prompt_decision():
    print("\n  [a] 采纳 — 我已核对官方原文，确认变更成立")
    print("  [r] 驳回 — 误报或不需改动")
    print("  [u] 标记待核实 — 存疑，前台显示「待核实」")
    print("  [s] 跳过 — 本轮不处理，留到下次")
    print("  [q] 退出 — 放弃本轮全部未处理项")
    tries = 0
    while True:
        c = ask("\n你的决定 [a/r/u/s/q]: ", "q").lower()
        if c in ("a", "r", "u", "s", "q"):
            return c
        tries += 1
        if tries >= 3:
            print("连续输入无效，本条按跳过处理。")
            return "s"
        print("请输入 a / r / u / s / q")


def apply_adopt(data, f):
    """采纳变更。需要人工输入新值，脚本不猜。"""
    p = find_policy(data, f["policy_id"])
    if not p:
        return None, "找不到该政策条目"

    ftype = f["type"]

    if ftype == "link_dead":
        print("\n链接失效的处理方式：")
        print("  1) 我找到了新地址")
        print("  2) 政策已废止（标记为 superseded）")
        c = ask("选择 [1/2]: ", "")
        if c == "1":
            new_url = ask("请粘贴新的官方 URL: ", "")
            if not new_url.startswith("http"):
                return None, "URL 无效，本条未改动"
            for s in p.get("sources", []):
                if s.get("url") == f["old_value"]:
                    s["url"] = new_url
                    break
            p["verification"]["last_verified"] = today()
            return f"sources.url: {f['old_value']} → {new_url}", None
        elif c == "2":
            p["verification"]["status"] = "superseded"
            note = ask("替代文件文号（可留空）: ", "")
            if note:
                p["validity"]["superseded_by"] = note
            return f"verification.status → superseded" + (f"（被 {note} 替代）" if note else ""), None
        return None, "未选择有效操作，本条未改动"

    if ftype == "stale":
        print("\n确认已重新核对官方原文？这会把 last_verified 更新为今天。")
        if ask("确认 [y/N]: ", "").lower() != "y":
            return None, "未确认，本条未改动"
        old = p["verification"]["last_verified"]
        p["verification"]["last_verified"] = today()
        return f"verification.last_verified: {old} → {today()}", None

    if ftype in ("amount_mismatch", "window_mismatch", "page_changed"):
        print("\n请输入变更内容。字段路径用点号，例如：")
        print("  benefit.amounts.bachelor")
        print("  application_window.end")
        print("  application_window.actual_end_extended")
        path = ask("字段路径（留空则仅更新核实日期）: ", "")
        if not path:
            old = p["verification"]["last_verified"]
            p["verification"]["last_verified"] = today()
            return f"verification.last_verified: {old} → {today()}（仅确认核实，字段未改）", None

        raw = ask("新值（数字直接输入，文本原样输入）: ", "")
        val = raw
        try:
            val = int(raw)
        except ValueError:
            try:
                val = float(raw)
            except ValueError:
                pass

        node = p
        keys = path.split(".")
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                return None, f"字段路径无效：{path}（{k} 不存在）"
            node = node[k]
        last = keys[-1]
        old_val = node.get(last)
        node[last] = val
        p["verification"]["last_verified"] = today()

        # 金额变动时写入 year_history，保留历史
        if path.startswith("benefit.amounts"):
            yh = p["benefit"].setdefault("year_history", [])
            yh.append({
                "year": datetime.now(CST).year,
                "amounts": dict(p["benefit"].get("amounts", {})),
                "note": f"经巡检确认于 {today()} 更新",
            })
        return f"{path}: {old_val} → {val}", None

    if ftype == "patrol_error":
        return None, "巡检异常无需数据变更，请检查脚本或网络后重跑"

    return None, f"未支持的变更类型：{ftype}"


def apply_unverified(data, f):
    p = find_policy(data, f["policy_id"])
    if not p:
        return None, "找不到该政策条目"
    p["verification"]["status"] = "unverified"
    reason = ask("待核实原因（会写入 pending_items）: ", "")
    if reason:
        p["verification"].setdefault("pending_items", []).append(
            f"[{today()}] {reason}")
    return "verification.status → unverified（前台将显示待核实标记）", None


def write_changelog(version, entries, report_name):
    lines = [
        "",
        f"## v{version} — {today()}",
        "",
        f"经人工确认发布。巡检报告：`{report_name}`",
        "",
        "| 政策 | 变更内容 |",
        "|---|---|",
    ]
    for pid, name, desc in entries:
        lines.append(f"| {name}（`{pid}`） | {desc} |")
    lines.append("")

    old = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else "# 政策数据变更日志\n"
    # 新版本插到标题之后、旧版本之前
    marker = "---\n"
    if marker in old:
        head, rest = old.split(marker, 1)
        CHANGELOG.write_text(head + marker + "\n".join(lines) + rest, encoding="utf-8")
    else:
        CHANGELOG.write_text(old + "\n".join(lines), encoding="utf-8")


def backup_current(data):
    VERSIONS.mkdir(parents=True, exist_ok=True)
    v = data["meta"]["data_version"]
    dst = VERSIONS / f"policies-v{v}-{today()}.json"
    shutil.copy2(POLICIES, dst)
    return dst


def cmd_rollback():
    VERSIONS.mkdir(parents=True, exist_ok=True)
    files = sorted(VERSIONS.glob("policies-v*.json"))
    if not files:
        print("没有历史版本可回滚。")
        return
    print("可回滚的版本：")
    for i, f in enumerate(files, 1):
        print(f"  {i}) {f.name}")
    c = ask(f"\n选择版本 [1-{len(files)}]，回车取消: ", "")
    if not c.isdigit() or not (1 <= int(c) <= len(files)):
        print("已取消。")
        return
    src = files[int(c) - 1]
    # 回滚前先把当前状态也备份，避免误操作丢数据
    cur = json.loads(POLICIES.read_text(encoding="utf-8"))
    safety = VERSIONS / f"before-rollback-{datetime.now(CST).strftime('%Y%m%d-%H%M%S')}.json"
    shutil.copy2(POLICIES, safety)
    shutil.copy2(src, POLICIES)
    print(f"\n已回滚到 {src.name}")
    print(f"回滚前状态已保存到 {safety.name}（如需撤销回滚可从此恢复）")


def cmd_versions():
    files = sorted(VERSIONS.glob("*.json"))
    if not files:
        print("暂无历史版本。")
        return
    print("历史版本：")
    for f in files:
        size = f.stat().st_size / 1024
        print(f"  {f.name}  ({size:.1f} KB)")


def main():
    ap = argparse.ArgumentParser(description="变更确认与版本发布")
    ap.add_argument("--report", help="指定巡检报告 JSON")
    ap.add_argument("--list", action="store_true", help="仅列出待确认项")
    ap.add_argument("--rollback", action="store_true", help="回滚到历史版本")
    ap.add_argument("--versions", action="store_true", help="列出历史版本")
    args = ap.parse_args()

    if args.rollback:
        return cmd_rollback()
    if args.versions:
        return cmd_versions()

    rp = Path(args.report) if args.report else latest_report()
    if not rp or not rp.exists():
        print("找不到巡检报告。请先运行：python3 tools/patrol.py")
        return

    report = json.loads(rp.read_text(encoding="utf-8"))
    findings = report.get("findings", [])

    print(f"巡检报告：{rp.name}")
    print(f"生成时间：{report.get('generated_at','')}")
    print(f"待确认项：{len(findings)} 条")

    if not findings:
        print("\n本次巡检没有需要处理的变更。")
        return

    if args.list:
        print("\n待确认清单：")
        for i, f in enumerate(findings, 1):
            print(f"  {i:2d}. {SEV_LABEL.get(f['severity'],'')} "
                  f"[{TYPE_LABEL.get(f['type'], f['type'])}] "
                  f"{f['policy_name']} — {f['field'] or '整体'}")
        print("\n运行 python3 tools/confirm.py 开始逐条确认。")
        return

    data = load_policies()
    original = json.dumps(data, ensure_ascii=False, sort_keys=True)

    adopted, rejected, marked, skipped = [], 0, [], 0
    quit_early = False

    for i, f in enumerate(findings, 1):
        show_finding(i, len(findings), f)
        d = prompt_decision()

        if d == "q":
            quit_early = True
            skipped += len(findings) - i + 1
            break
        if d == "s":
            skipped += 1
            continue
        if d == "r":
            rejected += 1
            print("  已驳回，数据未改动。")
            continue
        if d == "u":
            desc, err = apply_unverified(data, f)
            if err:
                print(f"  未生效：{err}")
            else:
                marked.append((f["policy_id"], f["policy_name"], desc))
                print(f"  已标记：{desc}")
            continue
        if d == "a":
            desc, err = apply_adopt(data, f)
            if err:
                print(f"  未生效：{err}")
            else:
                adopted.append((f["policy_id"], f["policy_name"], desc))
                print(f"  已采纳：{desc}")

    changes = adopted + marked

    print("\n" + "=" * 68)
    print("本轮结果")
    print("=" * 68)
    print(f"采纳 {len(adopted)} · 标记待核实 {len(marked)} · 驳回 {rejected} · 跳过 {skipped}")
    if quit_early:
        print("（已提前退出，未处理项保留在报告中，下次可继续）")

    if not changes:
        print("\n没有任何数据变更，不发布新版本。policies.json 保持原样。")
        return

    print("\n即将写入的变更：")
    for pid, name, desc in changes:
        print(f"  · {name}：{desc}")

    if json.dumps(data, ensure_ascii=False, sort_keys=True) == original:
        print("\n数据实际未发生变化，不发布新版本。")
        return

    print("\n发布新版本？这会：")
    print("  1) 备份当前版本到 data/versions/")
    print("  2) 更新 policies.json 与版本号")
    print("  3) 追加 data/CHANGELOG.md")
    if ask("\n确认发布 [y/N]: ", "").lower() != "y":
        print("已取消。policies.json 未被修改。")
        return

    bak = backup_current(json.loads(POLICIES.read_text(encoding="utf-8")))
    level = "minor" if len(changes) >= 5 else "patch"
    old_v = data["meta"]["data_version"]
    new_v = bump(old_v, level)
    data["meta"]["data_version"] = new_v
    data["meta"]["data_as_of"] = today()

    POLICIES.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_changelog(new_v, changes, rp.name)

    print(f"\n发布完成：v{old_v} → v{new_v}")
    print(f"  备份　: data/versions/{bak.name}")
    print(f"  数据　: data/policies.json（data_as_of {today()}）")
    print(f"  日志　: data/CHANGELOG.md")
    print(f"\n如需撤销：python3 tools/confirm.py --rollback")


if __name__ == "__main__":
    main()
