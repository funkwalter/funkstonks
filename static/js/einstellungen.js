// Show a source's settings only once the source is checked (user 2026-10-13:
// "wenn ich splitwise anklicke soll dann erst das menu mit den einstellungen
// kommen" - the settings menu should only appear after I tick Splitwise) -
// immediately on checking; saving happens via "Datenquellen speichern".
document.querySelectorAll('input[name^="src_"]').forEach(function (cb) {
  cb.addEventListener('change', function () {
    var key = cb.name.slice(4);
    document.querySelectorAll('[data-source-block="' + key + '"]').forEach(function (el) { el.hidden = !cb.checked; });
  });
});
