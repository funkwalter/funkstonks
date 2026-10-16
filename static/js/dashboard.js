const SANKEY_EL = document.getElementById('sankey-data');
const S = JSON.parse(SANKEY_EL.textContent);
const labelIndex = {};
S.labels.forEach((l, i) => { labelIndex[l] = i; });
function filterForName(name) {
  const idx = labelIndex[name];
  if (idx === undefined) return null;
  const f = S.filter[idx];
  return (f === null || f === undefined) ? null : f;
}

const nodes = S.labels.map((name, i) => ({
  name: name, depth: S.node_col[i],
  itemStyle: {color: S.colors[i]},
  // Pass-through node (placeholder for a branch in an intermediate column): no label
  label: S.spacers.indexOf(i) >= 0 ? {show: false} : undefined,
}));
const links = S.source.map((s, i) => ({
  source: S.labels[s], target: S.labels[S.target[i]], value: S.value[i],
}));

const sankeyDiv = document.getElementById('sankey');
const chart = echarts.init(sankeyDiv);

// Text color and label background come from the CSS variables so the chart
// follows the color scheme. Previously these were fixed values (#1B2733 on a
// white halo) - in dark mode the labels were black on white in the middle of
// a dark page.
function themeVar(name, fallback) {
  var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
function sankeyOption() {
  return {
  animation: false,
  textStyle: {fontFamily: 'Segoe UI, sans-serif'},
  tooltip: {trigger: 'item', triggerOn: 'mousemove',
    backgroundColor: themeVar('--card', '#FFFFFF'), borderColor: themeVar('--line', '#D8DEE4'),
    textStyle: {color: themeVar('--ink', '#1B2733')},
    formatter: function(p) {
      if (p.dataType === 'edge') {
        return p.data.source + ' → ' + p.data.target + '<br><b>' + p.data.value.toFixed(2) + ' €</b>';
      }
      return '<b>' + p.name.trim() + '</b>';
    }},
  series: [{
    type: 'sankey', layout: 'none', nodeAlign: 'left', layoutIterations: 0,
    nodeWidth: 18, nodeGap: 10,
    data: nodes, links: links,
    label: {color: themeVar('--ink', '#1B2733'), fontSize: 12,
      fontFamily: 'Segoe UI, sans-serif',
      backgroundColor: themeVar('--halo', 'rgba(255,255,255,0.85)'),
      padding: [2, 5], borderRadius: 3},
    itemStyle: {borderColor: 'transparent', borderWidth: 0},
    lineStyle: {color: 'source', opacity: 0.45, curveness: 0.5},
    emphasis: {focus: 'adjacency',
      itemStyle: {borderColor: themeVar('--ink', '#1B2733'), borderWidth: 2},
      lineStyle: {opacity: 0.75}},
    // Everything NOT hovered stays normally visible (no fading) - only the
    // hovered item gets a dark border (user request 2026-08-02: "everything
    // else gets completely white" with the default focus:'adjacency' fading
    // was confusing).
    blur: {itemStyle: {opacity: 1}, lineStyle: {opacity: 0.45}, label: {opacity: 1}},
  }],
  };
}
chart.setOption(sankeyOption());

// Recolor when the color scheme changes - without this the chart would keep
// the old colors until the next page load.
document.addEventListener('moneyflow:themechange', function () {
  chart.setOption(sankeyOption());
});
// Card loaded collapsed and then opened: ECharts only learns the size now
document.querySelector('[data-persist="dash-sankey"]').addEventListener('toggle', function () { chart.resize(); });

chart.on('click', function(params) {
  let f = null;
  if (params.dataType === 'node') {
    f = filterForName(params.data.name);
  } else if (params.dataType === 'edge') {
    f = filterForName(params.data.target);
    if (f === null) f = filterForName(params.data.source);
  }
  if (f === null) return;
  // |safe: inside a JavaScript string '&amp;' is NOT turned back into '&'
  // (unlike in an href attribute). Without it, clicking a Sankey node produced
  // '?from=2026-05&amp;to=2026-07&cat=4' - 'to' was no longer a parameter of
  // its own, the period shrank to the first month and the list was often
  // empty. The content is harmless: period_qs is built only from months that
  // really exist in the DB.
  window.location = '/transactions?' + SANKEY_EL.dataset.periodQs + '&cat=' + encodeURIComponent(f);
});
chart.on('mouseover', function(params) {
  const clickable = (params.dataType === 'node' && filterForName(params.data.name) !== null) ||
                     (params.dataType === 'edge');
  sankeyDiv.style.cursor = clickable ? 'pointer' : 'default';
});
window.addEventListener('resize', function(){ chart.resize(); });
