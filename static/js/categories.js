// "+" reveals the input row for a subcategory - instead of permanently
// showing an empty field under every category (user 2026-09-15).
document.querySelectorAll('.cat-add-btn').forEach(function (b) {
  b.addEventListener('click', function () {
    var row = document.getElementById(b.dataset.target);
    if (!row) return;
    row.hidden = !row.hidden;
    if (!row.hidden) { var inp = row.querySelector('input[name=add_name]'); if (inp) inp.focus(); }
  });
});
document.querySelectorAll('.cat-add-cancel').forEach(function (b) {
  b.addEventListener('click', function () { var row = b.closest('tr'); if (row) row.hidden = true; });
});
