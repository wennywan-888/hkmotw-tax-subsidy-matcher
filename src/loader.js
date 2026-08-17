/**
 * 政策数据加载器 — 失效兜底
 *
 * 三层保护：
 *   1. 数据文件格式错误 → 加载上一个正常版本，前台不白屏
 *   2. 单条政策超过阈值未核实 → 标记降级，前台显示「信息可能过时」
 *   3. 连备份都读不到 → 返回明确的错误态，让前台显示友好提示而非崩溃
 *
 * 设计意图：上线三个月后维护频率必然下降。这一层的作用是——
 * 在维护者偷懒时替他向用户承认「我可能过时了」，而不是默默给出错误信息。
 */

const DEFAULT_STALE_DAYS = 180;

/** 校验数据结构基本完整性 */
function validateShape(data) {
  const errs = [];
  if (!data || typeof data !== 'object') { errs.push('数据不是对象'); return errs; }
  if (!data.meta) errs.push('缺少 meta');
  if (!Array.isArray(data.policies)) errs.push('缺少 policies 数组');
  if (Array.isArray(data.policies)) {
    if (data.policies.length === 0) errs.push('policies 为空数组');
    data.policies.forEach((p, i) => {
      if (!p.id) errs.push(`policies[${i}] 缺少 id`);
      if (!p.name) errs.push(`policies[${i}] 缺少 name`);
      if (!p.eligible_identities) errs.push(`policies[${i}] (${p.id}) 缺少 eligible_identities`);
      if (!p.benefit) errs.push(`policies[${i}] (${p.id}) 缺少 benefit`);
      if (!p.verification) errs.push(`policies[${i}] (${p.id}) 缺少 verification`);
    });
  }
  return errs;
}

/** 为每条政策附加时效状态 */
function annotateStaleness(data, today = new Date()) {
  const threshold = data.meta?.stale_threshold_days ?? DEFAULT_STALE_DAYS;
  let staleCount = 0;

  for (const p of data.policies) {
    const lv = p.verification?.last_verified;
    if (!lv) {
      p._staleness = { level: 'unknown', days: null,
        notice: '该条政策缺少核实日期，请以官方最新文件为准' };
      staleCount++;
      continue;
    }
    const days = Math.floor((today - new Date(lv)) / 86400000);
    if (days > threshold) {
      p._staleness = {
        level: 'stale', days,
        notice: `本条信息已 ${days} 天未核实，可能已过时，请以官方最新文件与经办部门口径为准`
      };
      staleCount++;
    } else if (days > threshold * 0.7) {
      p._staleness = { level: 'aging', days,
        notice: `本条信息核实于 ${lv}，建议同时查阅官方最新公告` };
    } else {
      p._staleness = { level: 'fresh', days, notice: '' };
    }
  }

  // 数据整体时效
  const asOf = data.meta?.data_as_of;
  let globalDays = null;
  if (asOf) globalDays = Math.floor((today - new Date(asOf)) / 86400000);

  data._health = {
    stale_policy_count: staleCount,
    total_policy_count: data.policies.length,
    data_age_days: globalDays,
    global_notice: globalDays != null && globalDays > threshold
      ? `本站政策数据整体已 ${globalDays} 天未更新，请务必核对官方最新文件`
      : '',
  };
  return data;
}

/**
 * 加载政策数据
 * @param {Function} readJson  读取函数，签名 (path) => Object|null（浏览器端可传 fetch 包装）
 * @param {Object}   opts      { primary, versionsList, today }
 * @returns {Object} { ok, data, source, degraded, errors, message }
 */
function loadPolicies(readJson, opts = {}) {
  const primary = opts.primary || 'data/policies.json';
  const versions = opts.versionsList || [];
  const today = opts.today || new Date();

  // --- 第一层：尝试主数据文件 ---
  let raw = null, parseErr = null;
  try {
    raw = readJson(primary);
  } catch (e) {
    parseErr = e.message || String(e);
  }

  if (raw && !parseErr) {
    const errs = validateShape(raw);
    if (errs.length === 0) {
      return {
        ok: true,
        data: annotateStaleness(raw, today),
        source: primary,
        degraded: false,
        errors: [],
        message: '',
      };
    }
    parseErr = `数据结构校验失败：${errs.slice(0, 5).join('；')}`;
  }

  // --- 第二层：回退到最近的正常版本 ---
  const sorted = [...versions].sort().reverse();
  for (const v of sorted) {
    try {
      const bak = readJson(v);
      if (!bak) continue;
      const errs = validateShape(bak);
      if (errs.length === 0) {
        return {
          ok: true,
          data: annotateStaleness(bak, today),
          source: v,
          degraded: true,
          errors: [parseErr],
          message: `当前政策数据文件异常，已自动加载备份版本（${v}）。` +
                   `数据可能不是最新，请联系维护者修复。`,
        };
      }
    } catch (_) {
      continue;
    }
  }

  // --- 第三层：全部失败，返回明确错误态 ---
  return {
    ok: false,
    data: null,
    source: null,
    degraded: true,
    errors: [parseErr, '所有备份版本均无法加载'].filter(Boolean),
    message: '政策数据暂时无法加载，我们正在修复。' +
             '在此期间请直接查阅深圳市人力资源和社会保障局官网，或致电 12333 咨询。',
  };
}

/** 政策卡片应显示的提示徽标 */
function badgesFor(policy) {
  const badges = [];
  const vs = policy.verification?.status;

  if (vs === 'unverified') {
    badges.push({ key: 'unverified', level: 'warn', label: '待核实',
      tip: '该条信息尚未取得官方一手来源确认，仅供参考，请务必向经办部门核实' });
  }
  if (vs === 'superseded') {
    badges.push({ key: 'superseded', level: 'danger', label: '已废止',
      tip: '该政策已被新文件替代，仅作历史记录保留' });
  }
  if (vs === 'outdated') {
    badges.push({ key: 'outdated', level: 'warn', label: '可能已调整',
      tip: '来源页面已变动，条款可能调整，请核对官方原文' });
  }

  const st = policy._staleness;
  if (st?.level === 'stale') {
    badges.push({ key: 'stale', level: 'warn', label: '信息可能过时', tip: st.notice });
  } else if (st?.level === 'unknown') {
    badges.push({ key: 'no_verify_date', level: 'warn', label: '核实日期缺失', tip: st.notice });
  }

  // 身份适用性待核实
  const unverifiedIds = Object.entries(policy.eligible_identities || {})
    .filter(([, v]) => v.applicable === 'unverified')
    .map(([k]) => k);
  if (unverifiedIds.length) {
    badges.push({ key: 'identity_unverified', level: 'warn', label: '身份适用性待核实',
      tip: `以下身份的适用性尚未确认：${unverifiedIds.join('、')}。请向经办部门核实` });
  }

  if (badges.length === 0) {
    const lv = policy.verification?.last_verified;
    badges.push({ key: 'verified', level: 'ok', label: `已核实 ${lv || ''}`.trim(),
      tip: '该条已比对官方一手来源，但政策仍以官方最新文件为准' });
  }
  return badges;
}

if (typeof module !== 'undefined') {
  module.exports = { loadPolicies, validateShape, annotateStaleness, badgesFor };
}
