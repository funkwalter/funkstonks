// After an update the server quits and a fresh copy takes the port a moment
// later: poll until it answers, then open the settings page.
(function () {
  var box = document.getElementById('neustart');
  if (!box) { return; }
  var target = box.dataset.url || '/';
  var tries = 0;
  function ping() {
    tries += 1;
    fetch('/hilfe', {cache: 'no-store'}).then(function (r) {
      if (r.ok && tries > 2) { window.location.href = target; } else { setTimeout(ping, 1500); }
    }).catch(function () { setTimeout(ping, 1500); });
  }
  setTimeout(ping, 3000);
})();
