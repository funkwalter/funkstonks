function updateSum(){
  var sum = 0;
  document.querySelectorAll('.link-cb:checked').forEach(function(cb){ sum += parseFloat(cb.dataset.amt); });
  document.getElementById('sel-sum').textContent = sum.toFixed(2) + ' €';
  var any = document.querySelectorAll('.link-cb:checked').length > 0;
  document.getElementById('sel-warn').hidden = !(any && sum + 0.004 < parseFloat(document.getElementById('sel-warn').dataset.amount));
}
document.querySelectorAll('.link-cb').forEach(function(cb){ cb.addEventListener('change', updateSum); });
updateSum();
