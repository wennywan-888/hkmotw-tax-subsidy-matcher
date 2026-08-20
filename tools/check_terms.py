#!/usr/bin/env python3
"""
术语规范检查 —— 全站涉港澳台表述门禁

为什么单独做一个脚本：
这个项目的全部内容都围绕中国香港、中国澳门、中国台湾居民，
涉港澳台表述出错是最不能犯的错误。靠人肉通读不可靠，
而且每次改政策数据都可能引入新问题，所以做成可重复执行的门禁。

检查范围：所有前台可见文本 + 政策数据 + 文档。

用法：
  python3 tools/check_terms.py          # 检查全部
  python3 tools/check_terms.py --json   # 输出 JSON（供 CI 消费）

退出码：
  0 = 通过
  1 = 发现违规
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 检查这些文件。前台文件必查，文档也查——文档会被人看到。
TARGETS = [
    "index.html",
    "src/app.js",
    "src/matcher.js",
    "src/loader.js",
    "src/calendar.js",
    "data/policies.json",
    "README.md",
    "data/schema.md",
    "data/核实清单.md",
    "data/CHANGELOG.md",
    "docs/维护手册.md",
    "docs/上线步骤.md",
    "docs/推送与上线.md",
]

# ---------- 硬违规：出现即失败 ----------
# 把港澳台当作国家、或使用不规范政治表述
HARD_FORBIDDEN = [
    (r"香港国(?!际)", "「香港国」——香港是中国的特别行政区，不是国家"),
    (r"澳门国(?!际)", "「澳门国」——澳门是中国的特别行政区，不是国家"),
    (r"台湾国", "「台湾国」——台湾是中国的一部分，不是国家"),
    (r"台湾政府", "「台湾政府」——应表述为台湾地区，不得暗示其为独立政治实体"),
    (r"中华民国", "「中华民国」——不得使用"),
    (r"台湾共和国", "「台湾共和国」——不得使用"),
    (r"台独|一中一台|两个中国", "分裂表述，不得使用"),
    (r"香港政府(?!新闻)", "「香港政府」——应为香港特别行政区政府"),
    (r"澳门政府", "「澳门政府」——应为澳门特别行政区政府"),
    (r"台湾总统|台湾外交", "不得使用暗示主权的表述"),
]

# ---------- 并列违规：把港澳台与国家并列 ----------
# 例如「中国、香港、台湾」这种把三者平级列举的写法
PARALLEL_PATTERNS = [
    (r"中国[、,，/]\s*(香港|澳门|台湾)",
     "把中国与香港/澳门/台湾并列——三者不是平级关系"),
    (r"(香港|澳门|台湾)[、,，/]\s*中国(?!香港|澳门|台湾|大陆|内地)",
     "把香港/澳门/台湾与中国并列——三者不是平级关系"),
    (r"(日本|韩国|美国|新加坡|英国|德国|法国|越南|泰国|马来西亚)[、,，/]\s*(香港|澳门|台湾)(?!特别行政区|地区)",
     "把港澳台与外国并列，且未加「中国」限定或「地区」后缀"),
    (r"(香港|澳门|台湾)(?!特别行政区|地区|居民|同胞|青年|人才|籍)[、,，/]\s*(日本|韩国|美国|新加坡|英国|德国|法国)",
     "把港澳台与外国并列，且未加限定"),
]

# ---------- 英文违规 ----------
ENGLISH_FORBIDDEN = [
    (r"\bRepublic of China\b", "「Republic of China」——不得使用"),
    (r"\bTaiwan,?\s+(?:a\s+)?country\b", "把 Taiwan 称为 country"),
    (r"\bHong\s*Kong,?\s+(?:a\s+)?country\b", "把 Hong Kong 称为 country"),
    (r"\bcountries\b[^.。]{0,40}\b(?:Hong\s*Kong|Macao|Macau|Taiwan)\b",
     "把港澳台归入 countries 列举"),
    (r"\b(?:Hong\s*Kong|Macao|Macau|Taiwan)\b[^.。]{0,30}\bcountries\b",
     "把港澳台归入 countries 列举"),
    # 英文语境下应写 Hong Kong, China 一类；裸用并列外国名是问题
    (r"\b(?:Japan|Korea|Singapore|Vietnam|Thailand)\s*(?:,|and|/)\s*(?:Hong\s*Kong|Macao|Macau|Taiwan)\b",
     "英文中把港澳台与外国并列，须写 Hong Kong, China 等规范形式"),
]

# ---------- 建议规范（警告，不阻断）----------
# 裸用「香港/澳门/台湾」在多数语境下可接受（如「香港永久性居民」），
# 但作为地区主体单独出现时，建议加「中国」限定。这里只做提示。
SOFT_HINTS = [
    (r"(?<![中国])(?<!特别行政区)台湾同胞", "「台湾同胞」可接受，但正式表述建议「中国台湾居民」"),
]


def load_text(rel):
    fp = ROOT / rel
    if not fp.exists():
        return None
    return fp.read_text(encoding="utf-8")


# 豁免标记。
# 为什么需要：规范文档必须能列举反面例子（「不要写成香港国」），
# 检查脚本自身也要写规则模式。这些地方出现违规词是正当的，
# 但不能因此放弃检查整个文件——那等于给文档开后门。
#
# 做法：用行内标记逐行豁免，范围最小化。
#   行尾加 <!-- terms-ok --> 或 # terms-ok 豁免该行
#   区块用 <!-- terms-ok:start --> / <!-- terms-ok:end --> 包裹
INLINE_EXEMPT = re.compile(r"(?:<!--\s*terms-ok\s*-->|#\s*terms-ok\b)")
BLOCK_START = re.compile(r"<!--\s*terms-ok:start\s*-->|#\s*terms-ok:start\b")
BLOCK_END = re.compile(r"<!--\s*terms-ok:end\s*-->|#\s*terms-ok:end\b")


def exempt_lines(text):
    """返回被豁免的行号集合（1-based）"""
    out = set()
    in_block = False
    for i, line in enumerate(text.split("\n"), 1):
        if BLOCK_START.search(line):
            in_block = True
            out.add(i)
            continue
        if BLOCK_END.search(line):
            in_block = False
            out.add(i)
            continue
        if in_block or INLINE_EXEMPT.search(line):
            out.add(i)
    return out


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def context(text, pos, span=36):
    a = max(0, pos - span)
    b = min(len(text), pos + span)
    frag = text[a:b].replace("\n", " ")
    return ("…" if a > 0 else "") + frag + ("…" if b < len(text) else "")


def scan(rel, text):
    """返回 (violations, warnings)"""
    vio, warn = [], []
    skip = exempt_lines(text)

    groups = [
        ("hard", HARD_FORBIDDEN),
        ("parallel", PARALLEL_PATTERNS),
        ("english", ENGLISH_FORBIDDEN),
    ]
    for kind, rules in groups:
        for pat, why in rules:
            for m in re.finditer(pat, text, re.IGNORECASE if kind == "english" else 0):
                ln = line_of(text, m.start())
                # 跨行匹配时，只要起止行都被豁免才跳过——
                # 否则一行豁免可能掩盖相邻行的真问题
                end_ln = line_of(text, m.end())
                if all(x in skip for x in range(ln, end_ln + 1)):
                    continue
                vio.append({
                    "file": rel,
                    "line": ln,
                    "kind": kind,
                    "matched": m.group(0)[:60],
                    "reason": why,
                    "context": context(text, m.start()),
                })

    for pat, why in SOFT_HINTS:
        for m in re.finditer(pat, text):
            ln = line_of(text, m.start())
            if ln in skip:
                continue
            warn.append({
                "file": rel,
                "line": ln,
                "matched": m.group(0)[:60],
                "reason": why,
                "context": context(text, m.start()),
            })

    return vio, warn


def check_identity_labels():
    """
    额外检查：policies.json 里四类身份的中文标签必须以规范表述开头。
    这是前台直接展示给用户的文字，不能出错。

    只校验前缀而非全等——「中国香港居民（优才/专业人士/企业家入境计划）」
    这类带括号说明的写法是合法的，不该被判违规。
    """
    errs = []
    fp = ROOT / "data" / "policies.json"
    if not fp.exists():
        return errs
    data = json.loads(fp.read_text(encoding="utf-8"))

    # key → 必须以之开头的规范前缀
    required_prefix = {
        "hk_permanent": "中国香港",
        "hk_non_permanent": "中国香港",
        "mo_permanent": "中国澳门",
        "tw_resident": "中国台湾",
    }

    items = data.get("identity_types") or []
    if not items:
        errs.append("policies.json 缺少 identity_types 定义")
        return errs

    seen = {}
    for it in items:
        k = it.get("key") or it.get("value") or it.get("id")
        seen[k] = it.get("label")

    for key, prefix in required_prefix.items():
        if key not in seen:
            errs.append(f"identity_types 缺少 {key}")
            continue
        label = seen[key] or ""
        if not label.startswith(prefix):
            errs.append(
                f"{key} 标签为「{label}」，必须以「{prefix}」开头"
                f"（不得裸用香港/澳门/台湾作为地区主体）"
            )

    # 顺带检查 scope_note 也用了规范表述
    note = (data.get("meta") or {}).get("scope_note") or ""
    if note:
        for bare, good in [("香港", "中国香港"), ("澳门", "中国澳门"), ("台湾", "中国台湾")]:
            # 出现了裸用且没有「中国」前缀
            for m in re.finditer(bare, note):
                before = note[max(0, m.start() - 2):m.start()]
                if before != "中国" and "特别行政区" not in note[m.start():m.start() + 8]:
                    errs.append(
                        f"meta.scope_note 中「{bare}」未加规范限定，应为「{good}」："
                        f"…{note[max(0, m.start()-12):m.start()+12]}…"
                    )
                    break
    return errs


def main():
    ap = argparse.ArgumentParser(description="涉港澳台术语规范检查")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    all_vio, all_warn, missing = [], [], []
    for rel in TARGETS:
        text = load_text(rel)
        if text is None:
            missing.append(rel)
            continue
        v, w = scan(rel, text)
        all_vio += v
        all_warn += w

    label_errs = check_identity_labels()

    if args.json:
        print(json.dumps({
            "violations": all_vio,
            "warnings": all_warn,
            "label_errors": label_errs,
            "missing_files": missing,
            "passed": not all_vio and not label_errs,
        }, ensure_ascii=False, indent=2))
        return 1 if (all_vio or label_errs) else 0

    print(f"检查 {len(TARGETS) - len(missing)} 个文件")
    if missing:
        print(f"（跳过不存在的文件：{', '.join(missing)}）")
    print()

    if all_vio:
        print(f"发现 {len(all_vio)} 处违规：\n")
        for v in all_vio:
            print(f"  [{v['kind']}] {v['file']}:{v['line']}")
            print(f"    命中：{v['matched']}")
            print(f"    原因：{v['reason']}")
            print(f"    上下文：{v['context']}")
            print()

    if label_errs:
        print(f"身份标签问题 {len(label_errs)} 处：")
        for e in label_errs:
            print(f"  - {e}")
        print()

    if all_warn:
        print(f"提示 {len(all_warn)} 处（不阻断）：")
        for w in all_warn:
            print(f"  {w['file']}:{w['line']} — {w['matched']}：{w['reason']}")
        print()

    if all_vio or label_errs:
        print("术语检查未通过")
        return 1

    print("术语检查通过：未发现不规范的涉港澳台表述")
    return 0


if __name__ == "__main__":
    sys.exit(main())
