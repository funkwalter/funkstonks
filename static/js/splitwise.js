// Category selection without page reload (user request 2026-08-06: "wenn ich
// ein payment aendere, wirft es mich raus statt in der position zu bleiben" -
// changing a payment throws me out instead of staying in position)
document.querySelectorAll('.sw-cat-form select[name="category_id"]').forEach(function(sel) {
  sel.addEventListener('change', function() {
    fetch(sel.closest('form').action, {method: 'POST', body: new FormData(sel.closest('form'))});
  });
});
// "Als Urlaub markieren" (mark as vacation) checkbox per group, also without
// reload - the server returns which rows changed so the visible dropdowns
// follow immediately (otherwise only the DB changes but the page looks
// unchanged - user request 2026-08-06: "tut sich nichts bei den kategorien?"
// - nothing happens to the categories?).
document.querySelectorAll('.urlaub-toggle').forEach(function(cb) {
  cb.addEventListener('change', function() {
    var fd = new FormData();
    fd.append('ajax', '1');
    if (cb.checked) fd.append('on', '1');
    fetch('/splitwise/' + encodeURIComponent(cb.dataset.group) + '/urlaub', {method: 'POST', body: fd})
      .then(function(r) { return r.json(); })
      .then(function(changed) {
        changed.forEach(function(row) {
          var sel = document.querySelector('select[data-tx-id="' + row.id + '"]');
          if (sel) sel.value = row.category_id || '';
        });
      });
  });
});

// Confirm or undo the Splitwise <-> account amount match, without reload
// (user request 2026-09-01: red marker, but with a way to confirm).
document.querySelectorAll('.mm-row').forEach(function(row) {
  function post(undo) {
    var fd = new FormData();
    fd.append('ajax', '1');
    if (undo) { fd.append('undo', '1'); }
    return fetch('/splitwise/amount-ok/' + row.dataset.txId, {method: 'POST', body: fd});
  }
  var ok = row.querySelector('.mm-ok'), done = row.querySelector('.mm-done');
  ok.addEventListener('click', function() {
    post(false).then(function() {
      ok.classList.add('hide'); done.classList.remove('hide'); row.classList.add('dim-50');
    });
  });
  row.querySelector('.mm-undo').addEventListener('click', function() {
    post(true).then(function() {
      ok.classList.remove('hide'); done.classList.add('hide'); row.classList.remove('dim-50');
    });
  });
});
