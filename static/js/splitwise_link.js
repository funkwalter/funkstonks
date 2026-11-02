function updateSum(){
  var sum = 0;
  document.querySelectorAll('.link-cb:checked').forEach(function(cb){ sum += parseFloat(cb.dataset.amt); });
  document.getElementById('sel-sum').textContent = sum.toFixed(2).replace('.', ',') + ' €';
}
document.querySelectorAll('.link-cb').forEach(function(cb){ cb.addEventListener('change', updateSum); });
updateSum();
