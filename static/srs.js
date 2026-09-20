// srs.js — fixed SRS engine. UTC-safe dates, proper graduation
(function (root) {
  'use strict';
  var INTERVALS = [1, 3, 7, 14, 30];
  var MAX_STAGE = INTERVALS.length;
  function todayStr(now) {
    var d = now ? new Date(now) : new Date();
    var y = d.getFullYear();
    var m = String(d.getMonth() + 1).padStart(2, '0');
    var day = String(d.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + day;
  }
  function addDays(dateStr, n) {
    if (!dateStr || typeof dateStr !== 'string') return todayStr();
    var parts = dateStr.split('-');
    if (parts.length !== 3) return todayStr();
    var y = Number(parts[0]), mo = Number(parts[1]) - 1, da = Number(parts[2]);
    if (isNaN(y) || isNaN(mo) || isNaN(da)) return todayStr();
    var d = new Date(Date.UTC(y, mo, da));
    d.setUTCDate(d.getUTCDate() + n);
    var yy = d.getUTCFullYear();
    var mm = String(d.getUTCMonth() + 1).padStart(2, '0');
    var dd = String(d.getUTCDate()).padStart(2, '0');
    return yy + '-' + mm + '-' + dd;
  }
  function makeNewEntry(id, today) {
    return { id: id, stage: 0, nextDue: today, graduated: false };
  }
  function completeEntry(entry, today) {
    if (!entry) entry = makeNewEntry(0, today);
    if (entry.stage >= MAX_STAGE || entry.graduated) {
      return { id: entry.id, stage: MAX_STAGE, nextDue: null, graduated: true };
    }
    var wait = INTERVALS[entry.stage];
    if (wait === undefined) {
      return { id: entry.id, stage: MAX_STAGE, nextDue: null, graduated: true };
    }
    return { id: entry.id, stage: entry.stage + 1, nextDue: addDays(today, wait), graduated: false };
  }
  function isNew(entry) {
    if (!entry) return true;
    return !entry.graduated && entry.stage === 0;
  }
  function isDue(entry, today) {
    if (!entry || entry.graduated) return false;
    if (entry.stage === 0) return false;
    if (!entry.nextDue) return false;
    return entry.nextDue <= today;
  }
  function buildSession(progress, allIds, today, settings) {
    var newLimit = (settings && settings.dailyNewLimit) || 10;
    var revLimit = (settings && settings.dailyReviewLimit) || 60;
    newLimit = Math.max(0, Math.min(200, newLimit));
    revLimit = Math.max(0, Math.min(500, revLimit));
    var due = []; var news = [];
    for (var i = 0; i < allIds.length; i++) {
      var id = allIds[i];
      var entry = progress[id];
      if (!entry) { news.push(id); }
      else if (entry.graduated) { continue; }
      else if (entry.stage === 0) { news.push(id); }
      else if (entry.nextDue && entry.nextDue <= today) { due.push(entry); }
    }
    due.sort(function (a, b) {
      if (a.nextDue !== b.nextDue) return a.nextDue < b.nextDue ? -1 : 1;
      return a.id - b.id;
    });
    var reviews = due.slice(0, revLimit).map(function (e) { return e.id; });
    news = news.slice(0, newLimit);
    return { reviews: reviews, news: news, session: reviews.concat(news) };
  }
  var api = { INTERVALS: INTERVALS, MAX_STAGE: MAX_STAGE, todayStr: todayStr, addDays: addDays, makeNewEntry: makeNewEntry, completeEntry: completeEntry, isDue: isDue, isNew: isNew, buildSession: buildSession };
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; } else { root.SRS = api; }
})(typeof window !== 'undefined' ? window : this);
