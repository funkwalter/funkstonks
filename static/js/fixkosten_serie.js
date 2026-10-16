(function () {
  var inp = document.getElementById('merge-filter'), rows = document.querySelectorAll('#merge-table tr.merge-row');
  if (!inp) return;
  inp.addEventListener('input', function () {
    var q = inp.value.trim().toLowerCase();
    rows.forEach(function (r) { r.hidden = q && r.textContent.toLowerCase().indexOf(q) < 0; });
  });
})();
