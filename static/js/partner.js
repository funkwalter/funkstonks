// Category dropdown without reload (user request 2026-09-01: the page jumped
// back to the top after every change). Same AJAX pattern as in
// /transactions, /rules and /splitwise.
document.querySelectorAll('select.tx-cat').forEach(function(sel) {
  sel.addEventListener('change', function() {
    var fd = new FormData();
    fd.append('category_id', sel.value);
    fd.append('ajax', '1');
    sel.disabled = true;
    fetch('/tx/' + sel.dataset.txId + '/update', {method: 'POST', body: fd})
      .finally(function() { sel.disabled = false; });
  });
});
