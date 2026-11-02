const SD = JSON.parse(document.getElementById('split-data').textContent);
const CAT_GROUPS = SD.groups;
function catOptions(){
  return CAT_GROUPS.map(g => {
    const opts = g.options.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
    return g.label ? `<optgroup label="${g.label}">${opts}</optgroup>` : opts;
  }).join('');
}
function addRow(){
  const table = document.getElementById('rows');
  const tr = document.createElement('tr');
  tr.innerHTML = `<td><select name="cat">${catOptions()}</select></td>
<td class="num"><input type="text" name="amt" value="0,00" style="width:100px;text-align:right"></td>
<td><button type="button" class="ghost row-remove">–</button></td>`;
  table.appendChild(tr);
  updateRest();
}
// Running remainder: saving only once the parts add up exactly to the amount
const TOTAL = SD.total;
function parseAmt(v){ v = (v || '').trim().replace(/\s|€/g, ''); if (v.indexOf(',') >= 0) v = v.replace(/\./g, '').replace(',', '.'); const n = parseFloat(v); return isNaN(n) ? 0 : n; }
function updateRest(){
  let sum = 0; document.querySelectorAll('input[name=amt]').forEach(i => { sum += parseAmt(i.value); });
  const rest = Math.round((TOTAL - sum) * 100) / 100;
  const el = document.getElementById('split-rest'), btn = document.getElementById('split-save');
  if (!el) return;
  el.textContent = Math.abs(rest) < 0.005 ? 'Summe passt.' : 'Rest: ' + rest.toFixed(2) + ' €.';
  el.className = Math.abs(rest) < 0.005 ? 'pos' : 'neg';
  if (btn) btn.disabled = Math.abs(rest) >= 0.005;
}
document.addEventListener('input', e => { if (e.target.name === 'amt') updateRest(); });
document.addEventListener('click', e => {
  const rm = e.target.closest('.row-remove');
  if (rm) rm.closest('tr').remove();
  if (e.target.closest('#rows button')) setTimeout(updateRest, 0);
});
document.getElementById('split-add').addEventListener('click', addRow);
updateRest();
