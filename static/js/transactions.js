// Free-text comment per transaction, without reload (user request 2026-08-07) -
// same AJAX pattern as editing rules on /rules.
document.querySelectorAll('.tx-comment').forEach(function(box) {
  var view = box.querySelector('.tx-comment-view');
  var viewText = view.querySelector('i');
  var addLink = box.querySelector('.tx-comment-add-link');
  var form = box.querySelector('.tx-comment-form');
  var input = box.querySelector('.tx-comment-input');
  function showForm(e) {
    e.preventDefault();
    view.style.display = 'none';
    addLink.style.display = 'none';
    form.classList.remove('hide');
    input.focus();
  }
  box.querySelector('.tx-comment-edit-link').addEventListener('click', showForm);
  addLink.addEventListener('click', showForm);
  box.querySelector('.tx-comment-save').addEventListener('click', function() {
    var val = input.value.trim();
    var fd = new FormData();
    fd.append('comment', val);
    fd.append('ajax', '1');
    fetch('/tx/' + box.dataset.txId + '/update', {method: 'POST', body: fd}).then(function() {
      form.classList.add('hide');
      viewText.textContent = val;
      view.style.display = val ? '' : 'none';
      addLink.style.display = val ? 'none' : '';
    });
  });
});

// Category dropdown and "does not count" without reload (user request
// 2026-09-01: after a change at the very bottom of the list the page jumped
// back to the top, you had to scroll down again every time). Same AJAX
// pattern as the comment above, /rules and /splitwise.
function txPost(txId, fields) {
  var fd = new FormData();
  Object.keys(fields).forEach(function(k) { fd.append(k, fields[k]); });
  fd.append('ajax', '1');
  return fetch('/tx/' + txId + '/update', {method: 'POST', body: fd});
}

// Ask for the pattern: a substring is enough, case does not matter, no *
// needed (user request 2026-09-15: "sonst weiss ich ja nicht was als
// regeltext uebernommen wird" - otherwise I do not know what becomes the
// rule text). Returns false when cancelled.
function askRulePattern(form) {
  var sel = form.querySelector('select.tx-cat');
  var cat = sel.value && sel.selectedOptions[0] ? sel.selectedOptions[0].text : '';
  var hint = cat ? 'Zahlungen mit diesem Text immer als „' + cat + '“ verbuchen.'
                 : 'Zahlungen mit diesem Text immer so verbuchen wie die Kategorie, die du gleich wählst.';
  var p = prompt('Regel anlegen: ' + hint + '\n\n' +
                 'Ein Teil von Empfänger oder Verwendungszweck reicht, Groß/Klein ist egal, kein * nötig.',
                 form.querySelector('input[name=rule_pattern]').value || sel.dataset.rule || '');
  if (p === null || !p.trim()) { return false; }
  form.querySelector('input[name=rule_pattern]').value = p.trim();
  var lab = form.querySelector('.rule-label');
  if (lab) { lab.textContent = 'Regel: ' + p.trim(); }
  return true;
}
document.querySelectorAll('input[name=make_rule]').forEach(function(cb) {
  cb.addEventListener('change', function() {
    var form = cb.form;
    if (!cb.checked) {
      form.querySelector('input[name=rule_pattern]').value = '';
      var lab = form.querySelector('.rule-label'); if (lab) { lab.textContent = 'Regel'; }
      return;
    }
    if (!askRulePattern(form)) { cb.checked = false; return; }
    // Category already chosen: create the rule right away, otherwise it waits for the selection
    var sel = form.querySelector('select.tx-cat');
    if (sel && sel.value) { form.submit(); }
  });
});
document.querySelectorAll('select.tx-cat').forEach(function(sel) {
  sel.dataset.prev = sel.value;
  sel.addEventListener('focus', function() { sel.dataset.prev = sel.value; });
  sel.addEventListener('change', function() {
    // With "Regel" (rule) checked, deliberately the old way with a reload:
    // apply_rules() may categorize OTHER rows of the list too, which would
    // otherwise stay there stale.
    var rule = sel.form.querySelector('input[name=make_rule]');
    if (rule && rule.checked) {
      // Rule checkbox is set (the pattern was asked for when checking it) -
      // save with a reload because apply_rules() changes other rows as well.
      if (!sel.form.querySelector('input[name=rule_pattern]').value && !askRulePattern(sel.form)) {
        sel.value = sel.dataset.prev || ''; return;
      }
      sel.form.submit(); return;
    }
    sel.disabled = true;
    txPost(sel.dataset.txId, {category_id: sel.value})
      .finally(function() { sel.disabled = false; });
  });
});

document.querySelectorAll('.tx-toggle').forEach(function(btn) {
  btn.addEventListener('click', function() {
    btn.disabled = true;
    txPost(btn.dataset.txId, {toggle: '1'}).then(function() {
      var nowExcluded = btn.dataset.excluded !== '1';
      btn.dataset.excluded = nowExcluded ? '1' : '';
      btn.textContent = nowExcluded ? '↩' : '✕';
      var row = btn.closest('tr');
      if (row) { row.classList.toggle('dim', nowExcluded); }
    }).finally(function() { btn.disabled = false; });
  });
});
