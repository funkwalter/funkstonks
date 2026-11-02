document.querySelectorAll('.rule-delete-form').forEach(function(f) {
  f.addEventListener('submit', function(e) {
    e.preventDefault();
    var fd = new FormData();
    fd.append('ajax', '1');
    fd.append('delete', f.querySelector('button[name="delete"]').value);
    fetch('/rules', {method: 'POST', body: fd})
      .then(function() { f.closest('tr').remove(); });
  });
});
// Edit an existing rule without reload (same AJAX pattern as deleting) -
// user request 2026-08-07: "ich moechte ebenfalls bestehende regeln aendern
// koennen" (I also want to be able to change existing rules).
function setRuleRowMode(row, editing) {
  row.querySelectorAll('.rule-view').forEach(function(el) { el.classList.toggle('hide', editing); });
  row.querySelectorAll('.rule-edit').forEach(function(el) { el.classList.toggle('hide', !editing); });
}
document.querySelectorAll('.rule-edit-btn').forEach(function(btn) {
  btn.addEventListener('click', function() { setRuleRowMode(btn.closest('.rule-row'), true); });
});
document.querySelectorAll('.rule-cancel-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    var row = btn.closest('.rule-row');
    row.querySelector('.rule-edit-pattern').value = row.querySelector('.rule-view code').textContent;
    setRuleRowMode(row, false);
  });
});
document.querySelectorAll('.rule-save-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    var row = btn.closest('.rule-row');
    var pattern = row.querySelector('.rule-edit-pattern').value.trim();
    var categoryId = row.querySelector('.rule-edit-cat').value;
    var categoryChanged = row.querySelector('.rule-edit-cat').selectedOptions[0].defaultSelected === false;
    if (!pattern) return;
    var fd = new FormData();
    fd.append('ajax', '1');
    fd.append('edit', row.dataset.ruleId);
    fd.append('pattern', pattern);
    fd.append('category_id', categoryId);
    fetch('/rules', {method: 'POST', body: fd}).then(function() {
      if (categoryChanged) {
        // The rule now belongs to a different category card - an in-place
        // update is not enough, a reload sorts it into the right group.
        location.reload();
        return;
      }
      row.querySelector('.rule-view code').textContent = pattern;
      setRuleRowMode(row, false);
    });
  });
});

// Apply a rule retroactively to already booked payments (user request
// 2026-09-01). Deliberately two-step: count first (dry=1), then ask, then
// write - a rule can move many bookings at once, nobody should trigger that
// by accident.
document.querySelectorAll('.rule-apply-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    var row = btn.closest('.rule-row');
    var id = row.dataset.ruleId;
    var dry = new FormData(); dry.append('dry', '1');
    btn.disabled = true;
    fetch('/rules/' + id + '/apply', {method: 'POST', body: dry})
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d.changed) {
          alert('"' + d.pattern + '" trifft ' + d.matched +
                ' Zahlungen – alle stehen schon auf "' + d.category + '". Nichts zu tun.');
          return;
        }
        if (!confirm('"' + d.pattern + '" trifft ' + d.matched + ' Zahlungen.\n\n' +
                     d.changed + ' davon werden auf "' + d.category + '" umgestellt ' +
                     '(bestehende Kategorien werden dabei überschrieben).\n\n' +
                     'Es wird KEINE Sicherung angelegt – wenn du eine willst, vorher unter Sicherung anlegen.\n\nFortfahren?')) {
          return;
        }
        return fetch('/rules/' + id + '/apply', {method: 'POST', body: new FormData()})
          .then(function(r) { return r.json(); })
          .then(function(res) {
            alert(res.changed + ' Zahlungen auf "' + res.category + '" umgestellt.');
            location.reload();
          });
      })
      .finally(function() { btn.disabled = false; });
  });
});
