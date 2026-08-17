# 政策数据表字段规范 v1.0

> 本文件定义 `policies.json` 的字段结构。修改政策内容时**只改 policies.json，不改代码**。

## 设计目标

1. 政策每年变动，维护者只需编辑 JSON
2. 支持港/澳/台三类身份**分别标注**适用性
3. 支持同一政策**各区额度不同**
4. 支持同一政策**不同年度额度不同**
5. 支持政策被新文件替代后**保留历史版本**
6. 每条政策可追溯到**官方一手来源 + 原文引句**

---

## 顶层结构

```
{
  "meta": { ... },          // 数据版本元信息
  "identity_types": [ ... ],// 身份类型字典
  "districts": [ ... ],     // 行政区字典
  "policies": [ ... ]       // 政策条目数组
}
```

## meta 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `data_version` | string | 语义化版本号，如 `1.0.0` |
| `data_as_of` | date | 数据截至日期，前台显示用 |
| `last_full_review` | date | 最近一次全量核实日期 |
| `changelog_ref` | string | 变更日志文件路径 |
| `stale_threshold_days` | number | 超期天数阈值，超过则前台降级提示（默认 180） |

## policies[] 核心字段

### 标识与出处

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 唯一标识，格式 `<层级>-<主题>-<序号>`，如 `qh-employ-001` |
| `name` | string | 政策项简称，用于卡片标题 |
| `full_name` | string | 政策项全称 |
| `issuing_authority` | string | 发文机构 |
| `doc_number` | string | 文号，如 `深前海规〔2024〕12号` |
| `doc_title` | string | 依据文件全名 |
| `level` | enum | `national` / `provincial` / `municipal` / `district` / `zone` |
| `clause_ref` | string | 对应条款，如 `第六条` / `第1条就业补贴` |

### 适用身份（三类分别标注）

```json
"eligible_identities": {
  "hk_permanent":      { "applicable": true,  "note": "" },
  "hk_non_permanent":  { "applicable": true,  "note": "限优才/专业人士/企业家入境计划" },
  "mo_permanent":      { "applicable": true,  "note": "" },
  "tw_resident":       { "applicable": true,  "note": "依深人社函〔2026〕8号同等适用" }
}
```

`applicable` 取值：
- `true` — 有明确官方依据适用
- `false` — 有明确官方依据不适用
- `"unverified"` — **未找到明确依据，前台须显示「待核实」**

### 申领条件（结构化，禁止写成自然语言）

```json
"conditions": {
  "age_max": 45,
  "age_min": null,
  "nationality_cn_required": true,
  "education_min": "associate",        // associate|bachelor|master|doctor
  "employment_types": ["employed"],    // employed|flexible|startup|student|intern
  "work_districts": ["qianhai"],       // 见 districts 字典；空数组=全市
  "social_insurance": ["shenzhen_paid"],
  "graduate_within_years": null,
  "first_employment_after": "2021-07-01",
  "no_property_in_sz": false,
  "talent_recognition_required": false,
  "labor_contract_min_years": 1,
  "extra": [
    { "key": "social_insurance_location", "value": "qianhai",
      "desc": "社保关系须在前海合作区" }
  ]
}
```

### 补贴额度（支持学历分档 / 各区差异 / 年度差异）

```json
"benefit": {
  "type": "monthly",              // monthly|annual|one_time|tax_rebate|loan
  "calc_mode": "by_education",    // fixed|by_education|by_district|by_year|formula
  "amounts": {
    "doctor":   8000,
    "master":   4000,
    "bachelor": 3000,
    "associate":2000
  },
  "unit": "CNY/月",
  "cap_total": null,
  "duration_desc": "累计不超过3年",
  "duration_months_max": 36,
  "district_overrides": {          // 各区额度差异
    "futian": { "amounts": { "bachelor": 2250 }, "note": "港澳台籍提高50%" }
  },
  "year_history": [                // 年度差异，保留历史
    { "year": 2025, "amounts": { "doctor": 8000 } }
  ]
}
```

### 申报窗口与提醒

```json
"application_window": {
  "window_type": "annual_batch",   // annual_batch|rolling|one_time
  "start": "2025-08-01",
  "end":   "2025-08-14",
  "actual_end_extended": "2025-08-18",
  "next_expected": "每年8月，逐年公布",
  "note": "2025年度实际延长至8月18日"
}
```

### 办理渠道与材料

```json
"application": {
  "channel_name": "前海企业服务一体化平台",
  "channel_url": "https://qhsk.sz.gov.cn",
  "submit_via": "employer",        // self|employer|school|institution
  "materials": [ "身份证明", "劳动合同", "个税纳税证明" ],
  "steps": [
    { "step": 1, "action": "登录平台由「领补贴」进入港澳青年专区", "duration": "" }
  ],
  "processing_time": "初审→补正5工作日→审核→公示5工作日→拨付",
  "contact_phone": ["0755-88105454"]
}
```

### 来源与核实状态

```json
"sources": [
  { "title": "《十二条措施》2025年度个人类补贴申报指南",
    "url": "https://qh.sz.gov.cn/gkmlpt/content/12/12304/post_12304451.html",
    "publisher": "深圳市前海管理局",
    "published_date": "2025-07-31",
    "quote": "申请《十二条措施》支持的港澳青年，应当是45周岁以下、具有中国国籍的港澳居民",
    "is_primary": true }
],
"verification": {
  "status": "verified",            // verified|unverified|outdated|superseded
  "last_verified": "2026-08-17",
  "verified_by": "web_fetch_primary_source",
  "pending_items": [],
  "notes": ""
}
```

### 有效期与互斥关系

```json
"validity": {
  "effective_from": "2024-07-01",
  "effective_until": "2027-06-30",
  "supersedes": ["深前海规〔2023〕7号"],
  "superseded_by": null
},
"exclusions": {
  "cannot_combine_with": ["sz-*", "nanshan-*", "baoan-*"],
  "note": "同时符合深圳市、南山区、宝安区、前海其他同类性质支持政策的，不重复享受"
}
```

---

## 维护规则

1. **新增政策**：复制模板，逐字段填写，`verification.status` 初始设为 `unverified`
2. **核实通过后**才改为 `verified`，并填 `last_verified`
3. **政策废止**：`verification.status` 改 `superseded`，填 `validity.superseded_by`，**不要删除条目**
4. **金额变动**：在 `benefit.year_history` 追加新年度，同时更新 `benefit.amounts`
5. `applicable` 为 `"unverified"` 的身份，前台必须显示「待核实」标记
6. 每次修改后递增 `meta.data_version` 并更新 `meta.data_as_of`
