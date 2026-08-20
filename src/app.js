/**
 * 页面逻辑
 * 职责：读取数据 → 填充表单选项 → 收集画像 → 调用匹配引擎 → 渲染结果
 * 约束：本文件不含任何政策内容。选项从 policies.json 的字典动态生成。
 */

(function () {
  const { match } = window.PolicyMatcher;
  const { loadPoliciesAsync, badgesFor } = window.PolicyLoader;
  const { buildICS, downloadICS } = window.PolicyCalendar;

  let DATA = null;
  let LAST = null;   // 最近一次匹配结果，供日历导出使用

  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = n => '¥' + Number(n).toLocaleString('zh-CN');

  // matcher 里的单位是 "CNY/月" 这种机器友好写法，展示给用户要换成 ¥
  const pretty = s => String(s ?? '')
    .replace(/CNY\s*\/\s*/g, '元/')
    .replace(/\bCNY\b/g, '元')
    .replace(/（上限）/g, ' 上限');

  // 示例画像：让陌生人 30 秒内看到价值，不用自己想怎么填
  const EXAMPLES = [
    { label: '前海就业的香港本科生',
      v: { identity: 'hk_permanent', age: 27, education: 'bachelor', employment: 'employed',
           work_district: 'qianhai', live_district: 'nanshan', port: 'shenzhenbay',
           social_insurance: 'qianhai_paid', years_since_graduation: 3,
           annual_income: 420000, talent_recognized: false, no_property: true } },
    { label: '福田的台湾研发工程师',
      v: { identity: 'tw_resident', age: 32, education: 'master', employment: 'employed',
           work_district: 'futian', live_district: 'futian', port: '',
           social_insurance: 'shenzhen_paid', years_since_graduation: 9,
           annual_income: 900000, talent_recognized: true, no_property: false } },
    { label: '南山创业的澳门青年',
      v: { identity: 'mo_permanent', age: 26, education: 'bachelor', employment: 'startup',
           work_district: 'nanshan', live_district: 'nanshan', port: '',
           social_insurance: 'shenzhen_paid', years_since_graduation: 1,
           annual_income: 180000, talent_recognized: false, no_property: true } },
    { label: '前海就业的香港博士',
      v: { identity: 'hk_permanent', age: 33, education: 'doctor', employment: 'employed',
           work_district: 'qianhai', live_district: 'futian', port: 'futian',
           social_insurance: 'qianhai_paid', years_since_graduation: 1,
           annual_income: 1800000, talent_recognized: false, no_property: true } },
  ];

  // ---------- 初始化 ----------

  async function init() {
    const r = await loadPoliciesAsync({
      primary: 'data/policies.json',
      fallbacks: [],   // 部署后可挂 CDN 备份地址
    });

    const notice = $('globalNotice');

    if (!r.ok) {
      $('dataVersion').textContent = '数据加载失败';
      notice.className = 'notice danger';
      notice.innerHTML = esc(r.message);
      $('results').innerHTML =
        '<div class="empty">政策数据暂时不可用，请稍后重试。</div>';
      return;
    }

    DATA = r.data;
    const m = DATA.meta || {};
    $('dataVersion').textContent = `政策数据 v${m.data_version}，截至 ${m.data_as_of}`;
    $('dataCount').textContent = `共 ${DATA.policies.length} 项政策`;

    if (r.degraded) {
      notice.className = 'notice danger';
      notice.innerHTML = esc(r.message);
    } else if (DATA._health?.global_notice) {
      notice.className = 'notice warn';
      notice.innerHTML = esc(DATA._health.global_notice);
    }

    fillOptions();
    renderExamples();
    $('matchBtn').disabled = false;
    $('matchBtn').addEventListener('click', run);
  }

  function fillOptions() {
    const idSel = $('identity');
    (DATA.identity_types || []).forEach(t => {
      const o = document.createElement('option');
      o.value = t.key; o.textContent = t.label;
      idSel.appendChild(o);
    });

    const districts = DATA.districts || [];
    const workSel = $('work_district');
    districts.forEach(d => {
      const o = document.createElement('option');
      o.value = d.key;
      o.textContent = d.label + (d.note ? `（${d.note}）` : '');
      workSel.appendChild(o);
    });
    const outside = document.createElement('option');
    outside.value = 'none'; outside.textContent = '未在深圳就业';
    workSel.appendChild(outside);

    const liveSel = $('live_district');
    const na = document.createElement('option');
    na.value = ''; na.textContent = '不在深圳居住 / 跨境通勤';
    liveSel.appendChild(na);
    districts.filter(d => d.key !== 'qianhai').forEach(d => {
      const o = document.createElement('option');
      o.value = d.key; o.textContent = d.label;
      liveSel.appendChild(o);
    });
  }

  function renderExamples() {
    const box = $('examples');
    box.innerHTML = '';
    EXAMPLES.forEach(ex => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = ex.label;
      b.addEventListener('click', () => { applyExample(ex.v); run(); });
      box.appendChild(b);
    });
  }

  function applyExample(v) {
    Object.entries(v).forEach(([k, val]) => {
      const el = $(k);
      if (!el) return;
      if (el.type === 'checkbox') el.checked = !!val;
      else el.value = String(val);
    });
  }

  // ---------- 收集画像 ----------

  function profile() {
    return {
      identity: $('identity').value,
      age: parseInt($('age').value, 10) || 0,
      education: $('education').value,
      employment: $('employment').value,
      work_district: $('work_district').value,
      live_district: $('live_district').value,
      port: $('port').value,
      social_insurance: $('social_insurance').value,
      years_since_graduation: parseInt($('years_since_graduation').value, 10),
      annual_income: parseInt($('annual_income').value, 10),
      talent_recognized: $('talent_recognized').checked,
      has_property_in_sz: !$('no_property').checked,
      nationality_cn: true,
    };
  }

  // ---------- 渲染 ----------

  function run() {
    if (!DATA) return;
    const p = profile();
    const r = match(DATA, p, new Date());
    LAST = r;
    $('results').innerHTML = renderAll(r, p);
    bindActions();
  }

  /** 结果渲染后绑定日历导出等交互 */
  function bindActions() {
    const all = $('exportAll');
    if (all) all.addEventListener('click', () => exportCalendar(null));

    document.querySelectorAll('[data-ical]').forEach(btn => {
      btn.addEventListener('click', () => exportCalendar(btn.getAttribute('data-ical')));
    });
  }

  /**
   * 导出日历
   * @param {string|null} policyId 传 null 导出全部
   */
  function exportCalendar(policyId) {
    if (!LAST) return;
    const pool = [...LAST.strong, ...LAST.conditional, ...LAST.unverified];
    const items = policyId ? pool.filter(i => i.policy.id === policyId) : pool;

    const res = buildICS(items, new Date());
    if (!res.ics) {
      alert('这些项目都是常态受理，没有固定截止日期，暂不需要日历提醒。\n\n' +
            '有申报窗口的项目（如前海十二条、个税补贴）才会生成提醒。');
      return;
    }
    const name = policyId
      ? `${items[0].policy.name.replace(/[\\/:*?"<>|·\s]/g, '')}-申报提醒.ics`
      : '深圳补贴申报提醒.ics';
    downloadICS(res.ics, name);
  }

  function renderAll(r, p) {
    const s = r.summary;
    const total = s.count_strong + s.count_conditional + s.count_unverified;
    if (total === 0) return renderNothing(r);

    let h = renderSummary(s);
    h += renderToolbar(r);

    if (r.strong.length) {
      h += `<div class="group-title">条件已满足 · ${r.strong.length} 项</div>`;
      h += r.strong.map(i => card(i, 'strong')).join('');
    }
    if (r.conditional.length) {
      h += `<div class="group-title">需先完成前置动作 · ${r.conditional.length} 项</div>`;
      h += r.conditional.map(i => card(i, 'cond')).join('');
    }
    if (r.unverified.length) {
      h += `<div class="group-title">待核实 · ${r.unverified.length} 项（官方依据尚未确认，仅供参考）</div>`;
      h += r.unverified.map(i => card(i, 'unv')).join('');
    }
    if (r.nearMiss.length) h += renderNear(r.nearMiss);
    if (r.exclusion_warnings.length) h += renderExcl(r.exclusion_warnings);
    return h;
  }

  function renderToolbar(r) {
    const pool = [...r.strong, ...r.conditional, ...r.unverified];
    const hasDated = pool.some(i =>
      ['open', 'upcoming', 'pending', 'closed'].includes(i.window.state));
    if (!hasDated) return '';
    return `<div class="toolbar">
      <button id="exportAll">加入日历提醒</button>
      <span class="tip">导出 .ics 文件，点开即可加进手机日历。不用注册，我们不存你的任何信息。</span>
    </div>`;
  }

  function renderSummary(s) {
    let h = '<div class="summary"><div class="row">';
    if (s.monthly_total) {
      h += `<div class="kpi"><div class="v">${money(s.monthly_total)}</div>
            <div class="k">每月可领（已满足项合计）</div></div>`;
      h += `<div class="kpi"><div class="v">${money(s.annual_from_monthly)}</div>
            <div class="k">折合年度</div></div>`;
    }
    if (s.one_time_total) {
      h += `<div class="kpi"><div class="v">${money(s.one_time_total)}</div>
            <div class="k">一次性补贴合计</div></div>`;
    }
    h += `<div class="kpi"><div class="v">${s.count_strong}</div>
          <div class="k">条件已满足</div></div>`;
    h += '</div>';
    if (s.nearest_deadline) {
      const d = s.nearest_deadline;
      h += `<div class="deadline">最近截止：${esc(d.name)}　还剩 ${d.days_left} 天</div>`;
    }
    h += '</div>';
    return h;
  }

  function card(item, kind) {
    const p = item.policy;
    const b = item.benefit;
    const cls = kind === 'cond' ? 'card cond' : kind === 'unv' ? 'card unv' : 'card';

    let h = `<div class="${cls}"><h3>${esc(p.name)}</h3>`;

    // 徽标：待核实 / 已核实 / 信息可能过时
    const badges = badgesFor(p);
    if (badges.length) {
      h += '<div class="badges">' + badges.map(x =>
        `<span class="badge ${x.level === 'ok' ? 'ok' : x.level === 'danger' ? 'danger' : 'warn'}"
               title="${esc(x.tip)}">${esc(x.label)}</span>`).join('') + '</div>';
    }

    // 金额
    if (item.tax_estimate) {
      const t = item.tax_estimate;
      h += `<div class="amount">${money(t.rebate)}</div>
            <div class="reason">预估应纳税所得额 ${money(t.taxable)}，实缴个税约 ${money(t.paid)}，
            超过 15% 的部分可申请补贴。${esc(t.disclaimer)}</div>`;
    } else if (b.value) {
      h += `<div class="amount">${esc(pretty(b.display))}</div>`;
    } else if (b.display || b.formula) {
      h += `<div class="amount small">${esc(pretty(b.display || b.formula))}</div>`;
    }
    if (b.override_note) {
      h += `<div class="reason">本区特别规定：${esc(b.override_note)}</div>`;
    }
    if (b.duration) h += `<div class="reason">期限：${esc(b.duration)}</div>`;

    // 分档明细
    if (b.tiers?.length) {
      h += '<details><summary>查看分档标准</summary><div class="body"><ul>' +
        b.tiers.map(t => {
          const parts = [];
          if (t.year1) parts.push(`第一年 ${money(t.year1)}/月`);
          if (t.year2) parts.push(`第二年 ${money(t.year2)}/月`);
          if (t.year3) parts.push(`第三年 ${money(t.year3)}/月`);
          if (t.monthly_max) parts.push(`每月最高 ${money(t.monthly_max)}`);
          if (t.annual_max) parts.push(`每年最高 ${money(t.annual_max)}`);
          return `<li><b>${esc(t.scenario)}</b>：${parts.join('，')}</li>`;
        }).join('') + '</ul></div></details>';
    }
    if (b.alternatives?.length) {
      h += '<details><summary>两种方式择一</summary><div class="body"><ul>' +
        b.alternatives.map(a =>
          `<li><b>${esc(a.option)}</b>：${esc(a.desc)}</li>`).join('') +
        '</ul></div></details>';
    }

    // 前置动作
    if (item.soft_unmet?.length) {
      h += '<div class="reason">还需先完成：' +
        item.soft_unmet.map(u => esc(u.need)).join('、') + '</div>';
    }
    if (item.identity_note) {
      h += `<div class="reason">身份说明：${esc(item.identity_note)}</div>`;
    }

    // 申报窗口
    const w = item.window;
    h += `<div><span class="win ${w.state}">${esc(w.label)}`;
    if (w.days_left != null && w.state === 'open') h += `　还剩 ${w.days_left} 天`;
    if (w.days_left != null && w.state === 'upcoming') h += `　${w.days_left} 天后开放`;
    h += '</span></div>';

    // 新年度指南未发布时，把「该盯哪里」讲清楚 —— 这是用户最需要的信息
    if (w.state === 'pending') {
      let n = '<div class="winnote">';
      if (w.status_note) n += esc(w.status_note);
      if (w.last_year) {
        n += `<div style="margin-top:5px">去年参考：${esc(w.last_year.start)} 至 ` +
             `${esc(w.last_year.actual_end || w.last_year.end)}` +
             (w.last_year.guide_published
               ? `，指南于 ${esc(w.last_year.guide_published)} 发布` : '') + '</div>';
      }
      if (w.watch_urls?.length) {
        n += '<div style="margin-top:5px">关注这里：' + w.watch_urls.map(u =>
          `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`).join('　') +
          '</div>';
      }
      n += '</div>';
      h += n;
    }

    // 已结束时提示下一轮时间
    if (w.state === 'closed' && w.watch_urls?.length) {
      h += '<div class="winnote">下一轮开放前请留意：' + w.watch_urls.map(u =>
        `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`).join('　') +
        '</div>';
    }

    // 单条日历导出（仅对有时间概念的项显示）
    if (['open', 'upcoming', 'pending', 'closed'].includes(w.state)) {
      h += `<div><button class="ical" data-ical="${esc(p.id)}">加入日历提醒</button></div>`;
    }

    // 办理信息
    const ap = p.application || {};
    const via = { self: '本人申请', employer: '通过所在单位申请',
                  school: '通过学校申请', institution: '通过机构申请' }[ap.submit_via] || '';
    h += '<div class="metas">';
    if (via) h += `<span><b>申请方式</b> ${esc(via)}</span>`;
    if (ap.channel_name) h += `<span><b>办理渠道</b> ${esc(ap.channel_name)}</span>`;
    h += `<span><b>发文</b> ${esc(p.issuing_authority)}</span>`;
    if (p.doc_number) h += `<span><b>文号</b> ${esc(p.doc_number)}</span>`;
    h += '</div>';

    // 材料与流程
    if (ap.materials?.length || ap.steps?.length) {
      const n = (ap.materials?.length || 0);
      h += `<details><summary>办理流程与材料${n ? `（${n} 份材料）` : ''}</summary>` +
           '<div class="body">';

      if (ap.steps?.length) {
        h += '<b>办理步骤</b><ol class="steps">' + ap.steps.map(s =>
          `<li>${esc(s.action)}` +
          (s.duration ? `<div class="dur">用时：${esc(s.duration)}</div>` : '') +
          '</li>').join('') + '</ol>';
      }
      if (ap.processing_time) {
        h += `<div style="margin:6px 0"><b>整体时长</b>：${esc(ap.processing_time)}</div>`;
      }
      if (ap.materials?.length) {
        h += '<b>需要准备的材料</b><ul class="matlist">' +
          ap.materials.map(m => `<li>${esc(m)}</li>`).join('') + '</ul>';
      }
      if (ap.tax_note) {
        h += `<div class="quote">${esc(ap.tax_note)}</div>`;
      }
      if (ap.termination_rules) {
        h += `<div class="quote">终止发放的情形：${esc(ap.termination_rules)}</div>`;
      }
      if (ap.channel_url) {
        h += `<div style="margin-top:8px"><b>办理入口</b>：
              <a href="${esc(ap.channel_url)}" target="_blank" rel="noopener">
              ${esc(ap.channel_url)}</a></div>`;
      }
      if (ap.office_address) {
        h += `<div><b>办理地址</b>：${esc(ap.office_address)}</div>`;
      }
      if (ap.contact_phone?.length) {
        h += `<div><b>咨询电话</b>：${ap.contact_phone.slice(0, 4).map(esc).join('　')}</div>`;
      }
      if (ap.handling_note) {
        h += `<div style="margin-top:4px">${esc(ap.handling_note)}</div>`;
      }
      h += '</div></details>';
    }

    // 待核实事项
    if (item.pending_items?.length) {
      h += '<details><summary>待核实事项（' + item.pending_items.length + ' 项）</summary>' +
        '<div class="body"><ul>' +
        item.pending_items.map(x => `<li>${esc(x)}</li>`).join('') +
        '</ul></div></details>';
    }

    // 官方来源
    const srcs = p.sources || [];
    if (srcs.length) {
      h += '<div class="src">官方来源：' + srcs.map(s =>
        `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>` +
        (s.is_primary ? '（一手）' : '')).join('　') + '</div>';
      const prim = srcs.find(s => s.is_primary && s.quote);
      if (prim) h += `<div class="quote">原文：${esc(prim.quote.slice(0, 200))}</div>`;
    }

    h += '</div>';
    return h;
  }

  function renderNothing(r) {
    let h = '<div class="empty">按当前条件暂未匹配到明确可申领项。</div>';
    if (r.nearMiss.length) h += renderNear(r.nearMiss);
    else h += `<div class="near">可以试试调整这几项：工作地区改为「前海合作区」、
               社保状态改为已参保、或勾选已获人才认定。</div>`;
    return h;
  }

  function renderNear(near) {
    const tips = near.slice(0, 5).map(i => {
      const un = i.unmet.map(u => {
        const label = { age: '年龄', education: '学历', employment: '就业形态',
                        work_district: '工作地区', social_insurance: '社保状态',
                        graduation: '毕业年限', property: '住房情况',
                        nationality: '国籍' }[u.field] || u.field;
        return `${label}需为「${u.need}」`;
      }).join('，');
      return `<li><b>${esc(i.policy.name)}</b>：${esc(un)}</li>`;
    }).join('');
    return `<div class="near"><b>差一点就能匹配的项</b>
            <ul style="margin:6px 0 0 18px">${tips}</ul></div>`;
  }

  function renderExcl(warns) {
    const seen = new Set();
    const uniq = warns.filter(w => {
      if (seen.has(w.note)) return false;
      seen.add(w.note); return true;
    }).slice(0, 5);
    return '<div class="excl"><b>互斥关系提醒（不能重复享受）</b>' +
      uniq.map(w => `<div>· ${esc(w.policy)}：${esc(w.note)}</div>`).join('') + '</div>';
  }

  document.addEventListener('DOMContentLoaded', init);
})();
