(function(){
  // Three separate views instead of nine series on two axes (user
  // 2026-10-14: "das sieht nicht richtig aus" - that does not look right):
  // cash flow (euro per month), shares (100 % = the month's income, stacked),
  // cumulative (euro, summed up). No spline interpolation - it invented dips
  // between the months. A partial month (data-partial) is drawn faded and
  // marked in the tooltip.
  var R = Array.prototype.slice.call(document.querySelectorAll('tr.inv-row')).map(function(tr){
    return {m: tr.dataset.month, inc: +tr.dataset.inc, exp: +tr.dataset.exp, inv: +tr.dataset.invest,
            saldo: +tr.dataset.saldo, depotCost: +tr.dataset.depotcost || 0, depotCur: tr.dataset.depotcur === '1',
            depot: tr.dataset.depot === '' ? null : +tr.dataset.depot, partial: tr.dataset.partial === '1'};
  }).reverse();   // table is descending, chart ascending
  var cumInv = 0, cumSaldo = 0;
  R.forEach(function(r){ cumInv += r.inv; cumSaldo += r.saldo; r.cumInv = Math.round(cumInv * 100) / 100; r.cumSaldo = Math.round(cumSaldo * 100) / 100; });
  function pct(a, b) { return b > 0.004 ? Math.round(1000 * a / b) / 10 : null; }
  function themeVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  var el = document.getElementById('invchart');
  var chart = echarts.init(el);
  // Cash flow and cumulative as lines, shares as bars - fixed (user 2026-10-14),
  // no line/bar switch any more.
  var view = 'cash';
  try { var v = localStorage.getItem('mf-inv-view'); if (v === 'share' || v === 'cum') view = v; } catch (e) {}
  var selected = {};   // keep the legend selection per view across theme/view switches
  function pointStyle(r) { return r.partial ? {opacity: 0.45} : {}; }
  function series(key, name, color, type, extra) {
    return Object.assign({name: name, type: type, color: color, barMaxWidth: 34, symbolSize: 6,
      lineStyle: {width: 2}, itemStyle: type === 'bar' ? {borderRadius: [3, 3, 0, 0]} : {},
      data: R.map(function(r){ return {value: r[key], itemStyle: pointStyle(r)}; })}, extra || {});
  }
  function option() {
    var ink = themeVar('--ink', '#1B2733'), mute = themeVar('--mute', '#5C6771'), line = themeVar('--line', '#D8DEE4');
    var cPos = themeVar('--pos', '#2F6B4F'), cNeg = themeVar('--neg', '#9B3D3D'), cInv = themeVar('--invest', '#2E6FBF'),
        cSaldo = themeVar('--chartsaldo', '#8A5BB5'), cCumInv = themeVar('--chartcuminv', '#D9772E'),
        cCumSaldo = themeVar('--chartcumsaldo', '#B8911F'), cDepot = themeVar('--chartdepot', '#1F9E90');
    var ser, yAxis;
    if (view === 'share') {
      // 100 % = the month's income (real income only; refunds are already
      // subtracted from expenses): expenses + invested + remaining = exactly
      // 100 %. The axis ALWAYS ends at 100 % (user 2026-10-14: "die achse darf
      // nicht ueber 100% anzeigen" - the axis must not show more than 100 %):
      // spent more than earned -> remaining 0, expenses capped to the rest,
      // the real percentages are in the tooltip. A partial month and months
      // without income get no bar.
      var shareOf = function(r, key) { return (!r.partial && r.inc > 0.004) ? pct(Math.max(r[key], 0), r.inc) : null; };
      var mk = function(fn, name, color) {
        return {name: name, type: 'bar', stack: 'anteil', color: color, barMaxWidth: 44,
          data: R.map(function(r){ return {value: fn(r), itemStyle: pointStyle(r)}; })};
      };
      ser = [mk(function(r){ var e = shareOf(r, 'exp'), i = shareOf(r, 'inv'); return e === null ? null : Math.min(e, Math.max(100 - (i || 0), 0)); }, 'Ausgaben', cNeg),
             mk(function(r){ var i = shareOf(r, 'inv'); return i === null ? null : Math.min(i, 100); }, 'Investiert', cInv),
             mk(function(r){ var e = shareOf(r, 'exp'), i = shareOf(r, 'inv'); return e === null ? null : Math.max(100 - e - (i || 0), 0); }, 'Übrig', cPos)];
      yAxis = {type: 'value', min: 0, max: 100, interval: 25,
               axisLabel: {color: mute, formatter: '{value} %'}, splitLine: {lineStyle: {color: line}}};
    } else if (view === 'cum') {
      // Four curves: transfers into the depot (category Investitionen), cost
      // basis of the depot orders, depot value at prices (history, otherwise
      // the current price), balance - all cumulative or as of month end.
      ser = [series('cumInv', 'Investiert kumuliert', cCumInv, 'line', {areaStyle: {opacity: 0.10}}),
             series('depotCost', 'Depot-Einstand', cInv, 'line', {lineStyle: {width: 2, type: 'dashed'}}),
             series('depot', 'Depotwert (Kurse)', cDepot, 'line', {symbolSize: 8, connectNulls: false, lineStyle: {width: 3}}),
             series('cumSaldo', 'Saldo kumuliert', cCumSaldo, 'line')];
      yAxis = {type: 'value', axisLabel: {color: mute, formatter: function(v){ return v.toLocaleString('de-AT') + ' €'; }},
               splitLine: {lineStyle: {color: line}}};
    } else {
      ser = [series('inc', 'Einnahmen', cPos, 'line'), series('exp', 'Ausgaben', cNeg, 'line'),
             series('inv', 'Investiert', cInv, 'line'), series('saldo', 'Saldo', cSaldo, 'line')];
      yAxis = {type: 'value', axisLabel: {color: mute, formatter: function(v){ return v.toLocaleString('de-AT') + ' €'; }},
               splitLine: {lineStyle: {color: line}}};
    }
    if (!selected[view]) { selected[view] = {}; ser.forEach(function(s){ selected[view][s.name] = true; }); }
    return {
      animation: false,
      textStyle: {fontFamily: 'Segoe UI, sans-serif', color: ink},
      tooltip: {trigger: 'axis', axisPointer: {type: view === 'share' ? 'shadow' : 'line'},
        backgroundColor: themeVar('--card', '#FFFFFF'), borderColor: line, textStyle: {color: ink},
        formatter: function(ps) {
          var r = R[ps[0].dataIndex] || {};
          var out = '<b>' + ps[0].axisValue + '</b>' + (r.partial ? ' <span style="opacity:.7">(angebrochen)</span>' : '');
          if (view === 'cum' && r.depotCur && r.depot !== null) {
            out += ' <span style="opacity:.7">(Depotwert zum aktuellen Kurs)</span>';
          }
          ps.forEach(function(p) {
            var v = p.value && typeof p.value === 'object' ? p.value.value : p.value;
            if (view === 'share') {
              // real shares (uncapped) and the euro amount behind them
              var key = {'Ausgaben': 'exp', 'Investiert': 'inv', 'Übrig': null}[p.seriesName];
              var eur = key ? r[key] : r.inc - r.exp - r.inv;
              var real = r.inc > 0.004 ? Math.round(1000 * eur / r.inc) / 10 : null;
              v = real;
              var txt = v === null ? '–' : v.toFixed(1) + ' % (' + eur.toFixed(2) + ' €)';
            } else {
              var txt = v === null || v === undefined ? '–' : v.toFixed(2) + ' €';
            }
            out += '<br>' + p.marker + ' ' + p.seriesName + ': <b>' + txt + '</b>';
          });
          return out;
        }},
      legend: {top: 0, textStyle: {color: ink}, selected: selected[view], inactiveColor: mute},
      grid: {left: 70, right: 24, top: 44, bottom: 30},
      xAxis: {type: 'category', data: R.map(function(r){ return r.m; }), axisLine: {lineStyle: {color: line}},
              axisLabel: {color: mute, formatter: function(v, i){ return R[i] && R[i].partial ? v + ' *' : v; }}},
      yAxis: yAxis,
      series: ser
    };
  }
  function render() { chart.setOption(option(), true); }
  chart.on('legendselectchanged', function(e){ selected[view] = e.selected; });
  function setView(v) {
    view = v;
    try { localStorage.setItem('mf-inv-view', v); } catch (e) {}
    ['cash', 'share', 'cum'].forEach(function(k){ document.getElementById('view-' + k).classList.toggle('on', k === v); });
    render();
  }
  ['cash', 'share', 'cum'].forEach(function(k){ document.getElementById('view-' + k).addEventListener('click', function(){ setView(k); }); });
  setView(view);
  document.addEventListener('moneyflow:themechange', render);
  chart.on('click', function(p){ if (p.name && p.componentType === 'series') { window.location = '/transactions?month=' + p.name; } });
  window.addEventListener('resize', function(){ chart.resize(); });
  document.querySelector('[data-persist="inv-table"]').addEventListener('toggle', function(){ chart.resize(); });
})();
