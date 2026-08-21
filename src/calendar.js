/**
 * .ics 日历文件生成
 *
 * 为什么用 .ics 而不是账号 + 短信：
 *   不用存手机号、不用做隐私合规、不用付短信费。用户点一下加进手机日历，
 *   之后由系统负责提醒。对一个刚上线的工具，这是性价比最高的方案。
 *
 * 三类事件：
 *   1. 窗口已知且未结束 → 开始日 + 截止日两个事件，各带提前提醒
 *   2. 新年度指南未发布 → 在去年同期前 2 周放一个「留意公告」提醒
 *   3. 常态受理 → 不生成（没有截止日，提醒无意义）
 */

(function (root) {

  const PRODID = '-//港澳台来深补贴匹配器//ZH//';

  function pad(n) { return String(n).padStart(2, '0'); }

  /** Date → YYYYMMDD（全天事件用 DATE 值类型） */
  function dstamp(d) {
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;
  }

  /** Date → UTC 时间戳，用于 DTSTAMP */
  function utcstamp(d) {
    return d.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
  }

  function addDays(d, n) {
    const x = new Date(d.getTime());
    x.setDate(x.getDate() + n);
    return x;
  }

  /**
   * RFC 5545 要求单行不超过 75 字节，超出需折行（续行以空格开头）。
   * 中文是多字节，必须按字节数折而不是字符数，否则 Outlook 等客户端会解析异常。
   */
  function fold(line) {
    const enc = new TextEncoder();
    const bytes = enc.encode(line);
    if (bytes.length <= 73) return line;

    const out = [];
    let cur = '';
    let curBytes = 0;
    for (const ch of line) {
      const chBytes = enc.encode(ch).length;
      const limit = out.length === 0 ? 73 : 72;  // 续行前面要加一个空格
      if (curBytes + chBytes > limit) {
        out.push(cur);
        cur = ch;
        curBytes = chBytes;
      } else {
        cur += ch;
        curBytes += chBytes;
      }
    }
    if (cur) out.push(cur);
    return out[0] + out.slice(1).map(s => '\r\n ' + s).join('');
  }

  /** 转义 ics 文本字段中的特殊字符 */
  function esc(s) {
    return String(s ?? '')
      .replace(/\\/g, '\\\\')
      .replace(/;/g, '\\;')
      .replace(/,/g, '\\,')
      .replace(/\r?\n/g, '\\n');
  }

  function uid(policyId, kind) {
    return `${policyId}-${kind}-${Date.now().toString(36)}@hkmo-subsidy`;
  }

  /**
   * 生成一个全天事件
   * @param {Object} o { start, end, summary, description, uid, alarms }
   *   alarms: 提前天数数组，如 [30, 7]
   */
  function vevent(o) {
    const L = [
      'BEGIN:VEVENT',
      `UID:${o.uid}`,
      `DTSTAMP:${utcstamp(new Date())}`,
      `DTSTART;VALUE=DATE:${dstamp(o.start)}`,
      // 全天事件的 DTEND 是排他的，要 +1 天才能覆盖当天
      `DTEND;VALUE=DATE:${dstamp(addDays(o.end || o.start, 1))}`,
      fold(`SUMMARY:${esc(o.summary)}`),
      fold(`DESCRIPTION:${esc(o.description)}`),
      'TRANSP:TRANSPARENT',
    ];
    if (o.url) L.push(fold(`URL:${o.url}`));

    for (const days of (o.alarms || [])) {
      L.push(
        'BEGIN:VALARM',
        'ACTION:DISPLAY',
        `TRIGGER:-P${days}D`,
        fold(`DESCRIPTION:${esc(o.summary)}（还有 ${days} 天）`),
        'END:VALARM'
      );
    }
    L.push('END:VEVENT');
    return L;
  }

  /**
   * 为单条政策生成事件行
   * @returns {Array<string>} 事件行数组，无可提醒内容时返回空数组
   */
  function eventsForPolicy(item, today) {
    const p = item.policy;
    const w = item.window;
    const ap = p.application || {};
    const lines = [];

    const baseDesc = [
      p.full_name || p.name,
      p.doc_number ? `依据：${p.doc_number}` : '',
      ap.channel_name ? `办理渠道：${ap.channel_name}` : '',
      ap.channel_url ? ap.channel_url : '',
      ap.submit_via === 'employer' ? '注意：需通过所在单位提交申请' : '',
      '',
      '本提醒由「港澳台来深补贴优惠匹配器」生成，仅供参考。',
      '申报时间以官方最新公告为准，请提前核对。',
    ].filter(Boolean).join('\n');

    // 情况 1：窗口已知且尚未结束
    if ((w.state === 'open' || w.state === 'upcoming') && w.window_end) {
      const startDate = w.window_start ? new Date(w.window_start) : null;
      const endDate = new Date(w.window_end);

      if (startDate && startDate > today) {
        lines.push(...vevent({
          uid: uid(p.id, 'start'),
          start: startDate,
          summary: `【申报开放】${p.name}`,
          description: `申报窗口开放：${w.window_start} 至 ${w.window_end}\n\n${baseDesc}`,
          url: ap.channel_url,
          alarms: [14, 3],
        }));
      }
      lines.push(...vevent({
        uid: uid(p.id, 'deadline'),
        start: endDate,
        summary: `【申报截止】${p.name}`,
        description: `今天是申报最后一天。\n窗口：${w.window_start || '?'} 至 ${w.window_end}\n\n${baseDesc}`,
        url: ap.channel_url,
        alarms: [7, 1],
      }));
      return lines;
    }

    // 情况 2：新年度指南尚未发布 → 提醒用户盯公告
    if (w.state === 'pending' && w.last_year) {
      const ly = w.last_year;
      const lyStart = new Date(ly.start);

      // 按去年同期推算今年的预期开窗日
      const expected = new Date(today.getFullYear(), lyStart.getMonth(), lyStart.getDate());
      const twoWeeksBefore = addDays(expected, -14);

      // 关键判断：预期开窗期是否还没过去？
      // 去年窗口约两周，给 45 天宽限 —— 指南晚发很常见，不能因为过了去年
      // 同期就把提醒推到明年。用户现在就该盯公告，等一年是错的。
      const graceEnd = addDays(expected, 45);
      let watch, isUrgent;

      if (today <= graceEnd) {
        // 还在今年的合理发布窗口内 → 明天就提醒，因为随时可能发布
        watch = addDays(today, 1);
        isUrgent = true;
      } else if (twoWeeksBefore > today) {
        // 今年同期还没到 → 提前两周提醒
        watch = twoWeeksBefore;
        isUrgent = false;
      } else {
        // 今年确实已经错过 → 顺延到明年同期前两周
        watch = addDays(
          new Date(today.getFullYear() + 1, lyStart.getMonth(), lyStart.getDate()), -14);
        isUrgent = false;
      }

      const watchUrl = (w.watch_urls && w.watch_urls[0]) || ap.channel_url;
      const summary = isUrgent
        ? `【尽快查看】${p.name} 申报指南是否已发布`
        : `【留意公告】${p.name} 申报指南`;

      lines.push(...vevent({
        uid: uid(p.id, 'watch'),
        start: watch,
        summary,
        description: [
          '该政策为年度集中受理，本年度申报指南尚未发布。',
          isUrgent
            ? `按往年节奏，本年度指南可能已发布或即将发布，请尽快查看官网确认。`
            : '',
          `去年窗口：${ly.start} 至 ${ly.actual_end || ly.end}` +
            (ly.guide_published ? `（指南 ${ly.guide_published} 发布）` : ''),
          '',
          '请留意官方公告：',
          ...(w.watch_urls || []),
          '也可关注「深圳前海」官方微信公众号。',
          '',
          baseDesc,
        ].filter(Boolean).join('\n'),
        url: watchUrl,
        alarms: isUrgent ? [] : [7],
      }));
      return lines;
    }

    // 情况 3：本年度已结束，但明年会再开 → 提醒下一轮
    // 这类最容易被漏掉：用户今天看到「已结束」就走了，明年照样错过。
    if (w.state === 'closed') {
      const aw = p.application_window || {};
      const prevStart = aw.start ? new Date(aw.start) : null;
      if (prevStart) {
        // 明年同期
        const nextStart = new Date(prevStart.getTime());
        nextStart.setFullYear(today.getFullYear() + (prevStart.getMonth() >= today.getMonth() ? 0 : 1));
        if (nextStart <= today) nextStart.setFullYear(nextStart.getFullYear() + 1);

        const watch = addDays(nextStart, -21);
        lines.push(...vevent({
          uid: uid(p.id, 'nextround'),
          start: watch < today ? addDays(today, 1) : watch,
          summary: `【准备申报】${p.name} 即将开放`,
          description: [
            `本年度申报已结束，下一轮预计 ${aw.next_expected || '待公布'}。`,
            `上一轮窗口：${aw.start} 至 ${aw.actual_end_extended || aw.end}`,
            '',
            '建议提前准备材料，避免临期手忙脚乱。',
            aw.note ? `注意：${aw.note}` : '',
            '',
            baseDesc,
          ].filter(Boolean).join('\n'),
          url: ap.channel_url,
          alarms: [7],
        }));
        return lines;
      }
    }

    // 常态受理 → 不生成（没有截止日，提醒无意义）
    return lines;
  }

  /**
   * 生成完整 .ics 内容
   * @param {Array} items 匹配结果条目（含 policy 与 window）
   * @param {Date} today
   * @returns {{ics: string, count: number, skipped: number}}
   */
  function buildICS(items, today = new Date()) {
    const body = [];
    let count = 0, skipped = 0;

    for (const item of items) {
      const ev = eventsForPolicy(item, today);
      if (ev.length === 0) { skipped++; continue; }
      body.push(...ev);
      count += ev.filter(l => l === 'BEGIN:VEVENT').length;
    }

    if (count === 0) {
      return { ics: null, count: 0, skipped };
    }

    const lines = [
      'BEGIN:VCALENDAR',
      'VERSION:2.0',
      `PRODID:${PRODID}`,
      'CALSCALE:GREGORIAN',
      'METHOD:PUBLISH',
      fold('X-WR-CALNAME:深圳补贴申报提醒'),
      'X-WR-TIMEZONE:Asia/Shanghai',
      ...body,
      'END:VCALENDAR',
    ];
    // RFC 5545 要求 CRLF 换行
    return { ics: lines.join('\r\n') + '\r\n', count, skipped };
  }

  /** 触发浏览器下载 */
  function downloadICS(ics, filename) {
    const blob = new Blob([ics], { type: 'text/calendar;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || '深圳补贴申报提醒.ics';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const api = { buildICS, eventsForPolicy, downloadICS, fold, esc };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.PolicyCalendar = api;
  }
})(typeof self !== 'undefined' ? self : this);
