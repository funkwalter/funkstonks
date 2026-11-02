// Show the Splitwise name field only when Splitwise is checked - otherwise
// the setup asks for something the user does not need at all.
(function () {
  var cb = document.querySelector('input[name="src_splitwise"]');
  var block = document.getElementById('sw-block');
  if (!cb || !block) return;
  function sync() { block.style.display = cb.checked ? '' : 'none'; }
  cb.addEventListener('change', sync);
  sync();
})();
