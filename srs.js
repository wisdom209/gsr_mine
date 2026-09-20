// srs.js — pure SRS engine. Works in Node and browser. ES5 syntax.
(function (root) {
  'use strict';

  // Wait times after completing each stage.
  // stage 0 (new)  → wait 1 day  → stage 1
  // stage 1        → wait 3 days → stage 2
  // stage 2        → wait 7 days → stage 3
  // stage 3        → wait 14 days→ stage 4
  // stage 4        → wait 30 days→ stage 5
  // stage 5        → graduate
  var INTERVALS = [1, 3, 7, 14, 30];
  var MAX_STAGE = INTERVALS.length; // 5

  // ---------- Date helpers ----------
  function todayStr(now) {
    var d = now ? new Date(now) : new Date();
    var y = d.getFullYear();
    var m = String(d.getMonth() + 1).padStart(2, '0');
    var day = String(d.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + day;
  }

  function addDays(dateStr, n) {
    var parts = dateStr.split('-');
    var d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
    d.setDate(d.getDate() + n);
    var y = d.getFullYear();
    var m = String(d.getMonth() + 1).padStart(2, '0');
    var day = String(d.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + day;
  }

  // ---------- Sentence lifecycle ----------

  // Create a brand-new entry (never shown). nextDue is today but
  // buildSession treats stage===0 as "new", not "due".
  function makeNewEntry(id, today) {
    return { id: id, stage: 0, nextDue: today, graduated: false };
  }

  // Advance an entry after completing it today.
  // Pure: does not mutate input.
  function completeEntry(entry, today) {
    var wait = INTERVALS[entry.stage];
    if (wait === undefined) {
      // Past the last interval → graduated
      return { id: entry.id, stage: entry.stage, nextDue: null, graduated: true };
    }
    return {
      id: entry.id,
      stage: entry.stage + 1,
      nextDue: addDays(today, wait),
      graduated: false,
    };
  }

  // A stage===0 entry is NEW; stage>=1 entries can be DUE.
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

  // ---------- Session builder ----------
  function buildSession(progress, allIds, today, settings) {
    var newLimit = (settings && settings.dailyNewLimit) || 10;
    var revLimit = (settings && settings.dailyReviewLimit) || 60;

    var due = [];
    var news = [];

    for (var i = 0; i < allIds.length; i++) {
      var id = allIds[i];
      var entry = progress[id];

      if (!entry) {
        // Never seen → new
        news.push(id);
      } else if (entry.graduated) {
        // Done → skip
        continue;
      } else if (entry.stage === 0) {
        // Created but not shown yet → new
        news.push(id);
      } else if (entry.nextDue && entry.nextDue <= today) {
        // Older-review → due
        due.push(entry);
      }
    }

    // Oldest-due-first, tie-break by id
    due.sort(function (a, b) {
      if (a.nextDue !== b.nextDue) return a.nextDue < b.nextDue ? -1 : 1;
      return a.id - b.id;
    });

    var reviews = due.slice(0, revLimit).map(function (e) { return e.id; });
    news = news.slice(0, newLimit);

    return {
      reviews: reviews,
      news: news,
      session: reviews.concat(news),
    };
  }

  // ---------- Exports ----------
  var api = {
    INTERVALS: INTERVALS,
    MAX_STAGE: MAX_STAGE,
    todayStr: todayStr,
    addDays: addDays,
    makeNewEntry: makeNewEntry,
    completeEntry: completeEntry,
    isDue: isDue,
    isNew: isNew,
    buildSession: buildSession,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.SRS = api;
  }

})(typeof window !== 'undefined' ? window : this);