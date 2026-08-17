/**
 * 政策匹配引擎
 * 职责：读取 policies.json，按用户画像匹配政策。
 * 约束：本文件不含任何政策内容（金额/条件/文号）。改政策只改 data/policies.json。
 */

const EDU_RANK = { associate: 1, bachelor: 2, master: 3, doctor: 4 };

/** 判断某条政策对指定身份是否适用 */
function checkIdentity(policy, identityKey) {
  const e = policy.eligible_identities[identityKey];
  if (!e) return { pass: false, level: 'no' };
  if (e.applicable === true) return { pass: true, level: 'yes', note: e.note };
  if (e.applicable === 'unverified') return { pass: true, level: 'unverified', note: e.note };
  return { pass: false, level: 'no', note: e.note };
}

/** 逐条检查结构化申领条件，返回未满足项 */
function checkConditions(policy, profile) {
  const c = policy.conditions || {};
  const unmet = [];

  if (c.age_max != null && profile.age > c.age_max) {
    unmet.push({ field: 'age', need: `${c.age_max}周岁以下`, got: `${profile.age}周岁` });
  }
  if (c.age_min != null && profile.age < c.age_min) {
    unmet.push({ field: 'age', need: `${c.age_min}周岁以上`, got: `${profile.age}周岁` });
  }
  if (c.education_min && EDU_RANK[profile.education] < EDU_RANK[c.education_min]) {
    unmet.push({ field: 'education', need: c.education_min, got: profile.education });
  }
  if (c.employment_types?.length && !c.employment_types.includes(profile.employment)) {
    unmet.push({ field: 'employment', need: c.employment_types.join('/'), got: profile.employment });
  }
  if (c.work_districts?.length && !c.work_districts.includes(profile.work_district)) {
    unmet.push({ field: 'work_district', need: c.work_districts.join('/'), got: profile.work_district });
  }
  if (c.social_insurance?.length && !c.social_insurance.includes(profile.social_insurance)) {
    unmet.push({ field: 'social_insurance', need: c.social_insurance.join('/'), got: profile.social_insurance });
  }
  if (c.graduate_within_years != null) {
    if (profile.years_since_graduation == null ||
        profile.years_since_graduation > c.graduate_within_years) {
      unmet.push({ field: 'graduation', need: `毕业${c.graduate_within_years}年内`, got: '不满足' });
    }
  }
  if (c.no_property_in_sz === true && profile.has_property_in_sz !== false) {
    unmet.push({ field: 'property', need: '本人及配偶、未满18周岁子女在深无房', got: '未确认无房' });
  }
  if (c.talent_recognition_required === true && profile.talent_recognized !== true) {
    unmet.push({ field: 'talent', need: '已获高端/紧缺人才认定', got: '未认定', soft: true });
  }
  if (c.nationality_cn_required === true && profile.nationality_cn === false) {
    unmet.push({ field: 'nationality', need: '具有中国国籍', got: '否' });
  }
  return unmet;
}

/** 计算补贴额度，支持学历分档 / 各区差异 / 公式 */
function resolveBenefit(policy, profile) {
  const b = policy.benefit || {};
  const override = b.district_overrides?.[profile.work_district];
  const src = override || b;
  const mode = src.calc_mode || b.calc_mode;
  const amounts = src.amounts || b.amounts || {};

  let value = null;
  let display = '';

  if (mode === 'by_education') {
    value = amounts[profile.education] ?? null;
    display = value != null ? `${value.toLocaleString()} ${b.unit}` : '按学历分档';
  } else if (mode === 'fixed') {
    value = amounts.default ?? null;
    display = value != null ? `${value.toLocaleString()} ${b.unit}` : '';
  } else if (mode === 'formula') {
    display = b.formula || '按公式计算';
    if (amounts.min != null && amounts.max != null) {
      display = `${amounts.min.toLocaleString()} - ${amounts.max.toLocaleString()} ${b.unit}`;
    }
  } else if (mode === 'by_district') {
    display = b.tiers ? '按载体类型与人员类别分档' : '';
  }

  return {
    value,
    display,
    unit: b.unit,
    type: b.type,
    duration: b.duration_desc,
    cap: b.cap_total,
    tiers: b.tiers || null,
    alternatives: b.alternatives || null,
    override_note: override?.note || null,
    formula: b.formula || null
  };
}

/** 计算个税补贴估算（仅用于 tax_rebate 类型） */
function estimateTaxRebate(annualIncome) {
  const taxable = Math.max(0, annualIncome - 60000 - annualIncome * 0.1);
  const brackets = [
    [36000, 0.03, 0], [144000, 0.10, 2520], [300000, 0.20, 16920],
    [420000, 0.25, 31920], [660000, 0.30, 52920], [960000, 0.35, 85920],
    [Infinity, 0.45, 181920]
  ];
  let paid = 0;
  for (const [cap, rate, ded] of brackets) {
    if (taxable <= cap) { paid = taxable * rate - ded; break; }
  }
  paid = Math.max(0, paid);
  const line15 = taxable * 0.15;
  const rebate = Math.max(0, Math.min(paid - line15, 5000000));
  return {
    taxable: Math.round(taxable),
    paid: Math.round(paid),
    rebate: Math.round(rebate),
    disclaimer: '粗略估算，未考虑专项附加扣除、年终奖单独计税等因素，实际以税务机关核算为准'
  };
}

/** 判断申报窗口状态 */
function windowStatus(policy, today = new Date()) {
  const w = policy.application_window || {};
  if (w.window_type === 'rolling') {
    return { state: 'rolling', label: '常态受理', days_left: null };
  }
  const end = w.actual_end_extended || w.end;
  if (!end) {
    return { state: 'unknown', label: w.next_expected || '窗口待公布', days_left: null };
  }
  const endDate = new Date(end);
  const startDate = w.start ? new Date(w.start) : null;
  const dayMs = 86400000;

  if (today > endDate) {
    return { state: 'closed', label: `本年度已结束，下次预计：${w.next_expected || '待公布'}`, days_left: null };
  }
  if (startDate && today < startDate) {
    return {
      state: 'upcoming',
      label: `将于 ${w.start} 开放`,
      days_left: Math.ceil((startDate - today) / dayMs)
    };
  }
  return {
    state: 'open',
    label: `受理中，截止 ${end}`,
    days_left: Math.ceil((endDate - today) / dayMs)
  };
}

/** 数据是否过期 */
function stalenessCheck(policy, meta, today = new Date()) {
  const lv = policy.verification?.last_verified;
  if (!lv) return { stale: true, days: null };
  const days = Math.floor((today - new Date(lv)) / 86400000);
  return { stale: days > (meta.stale_threshold_days ?? 180), days };
}

/**
 * 主匹配函数
 * @param {Object} data    policies.json 解析结果
 * @param {Object} profile 用户画像
 * @returns {Object} 分组后的匹配结果
 */
function match(data, profile, today = new Date()) {
  const strong = [], conditional = [], unverified = [], nearMiss = [];

  for (const p of data.policies) {
    const idCheck = checkIdentity(p, profile.identity);
    if (!idCheck.pass) continue;

    // 已废止的政策不参与匹配
    if (p.verification?.status === 'superseded') continue;

    const unmet = checkConditions(p, profile);
    const hardUnmet = unmet.filter(u => !u.soft);
    const softUnmet = unmet.filter(u => u.soft);

    const item = {
      policy: p,
      benefit: resolveBenefit(p, profile),
      window: windowStatus(p, today),
      staleness: stalenessCheck(p, data.meta, today),
      identity_level: idCheck.level,
      identity_note: idCheck.note,
      unmet: hardUnmet,
      soft_unmet: softUnmet,
      verification_status: p.verification?.status,
      pending_items: p.verification?.pending_items || []
    };

    if (item.benefit.type === 'tax_rebate' && profile.annual_income) {
      item.tax_estimate = estimateTaxRebate(profile.annual_income);
    }

    if (hardUnmet.length === 0) {
      if (idCheck.level === 'unverified' || p.verification?.status === 'unverified') {
        unverified.push(item);
      } else if (softUnmet.length > 0) {
        conditional.push(item);
      } else {
        strong.push(item);
      }
    } else if (hardUnmet.length <= 2) {
      nearMiss.push(item);
    }
  }

  // 按窗口紧迫度排序：开放中且剩余天数少的优先
  const byUrgency = (a, b) => {
    const rank = s => ({ open: 0, upcoming: 1, rolling: 2, unknown: 3, closed: 4 }[s.state] ?? 5);
    const d = rank(a.window) - rank(b.window);
    if (d !== 0) return d;
    if (a.window.days_left != null && b.window.days_left != null) {
      return a.window.days_left - b.window.days_left;
    }
    return 0;
  };
  strong.sort(byUrgency);
  conditional.sort(byUrgency);
  unverified.sort(byUrgency);

  // 汇总
  const monthlyTotal = strong
    .filter(i => i.benefit.type === 'monthly' && i.benefit.value)
    .reduce((s, i) => s + i.benefit.value, 0);
  const oneTimeTotal = strong
    .filter(i => i.benefit.type === 'one_time' && i.benefit.value)
    .reduce((s, i) => s + i.benefit.value, 0);

  const openItems = [...strong, ...conditional].filter(i => i.window.state === 'open');
  const nearestDeadline = openItems.length
    ? openItems.reduce((m, i) => (i.window.days_left < m.window.days_left ? i : m))
    : null;

  return {
    meta: data.meta,
    summary: {
      count_strong: strong.length,
      count_conditional: conditional.length,
      count_unverified: unverified.length,
      monthly_total: monthlyTotal,
      annual_from_monthly: monthlyTotal * 12,
      one_time_total: oneTimeTotal,
      nearest_deadline: nearestDeadline
        ? { name: nearestDeadline.policy.name, days_left: nearestDeadline.window.days_left }
        : null
    },
    strong,        // 强匹配：条件全满足
    conditional,   // 需前置动作：如需先取得人才认定
    unverified,    // 待核实：官方依据不明确，须显著标注
    nearMiss,      // 差一两个条件：用于提示「调整哪个条件可能就有了」
    exclusion_warnings: collectExclusions([...strong, ...conditional])
  };
}

/** 收集互斥关系提示 */
function collectExclusions(items) {
  const warns = [];
  for (const i of items) {
    const ex = i.policy.exclusions;
    if (ex?.note) warns.push({ policy: i.policy.name, note: ex.note });
  }
  return warns;
}

if (typeof module !== 'undefined') {
  module.exports = { match, checkIdentity, checkConditions, resolveBenefit,
                     windowStatus, stalenessCheck, estimateTaxRebate };
}
