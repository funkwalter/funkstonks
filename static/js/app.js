// Switch the color scheme immediately, without a server round trip - the
// setting is saved as well so it survives a restart.
(function () {
  var sel = document.getElementById('theme-toggle');
  if (!sel) return;
  sel.addEventListener('change', function () {
    var next = sel.value;
    document.documentElement.setAttribute('data-theme', next);
    // Pages with their own rendering (Sankey, investments chart) have to
    // recolor themselves - CSS alone does not reach canvas content.
    document.dispatchEvent(new CustomEvent('moneyflow:themechange', {detail: next}));
    var fd = new FormData();
    fd.append('action', 'theme');
    fd.append('theme', next);
    fetch('/einstellungen', {method: 'POST', body: fd});
  });
})();

// Collapse/expand the sidebar (state is kept in the browser) and open it via
// the top button on narrow screens.
(function () {
  var root = document.documentElement;
  var col = document.getElementById('nav-collapse');
  function setNav(collapsed) {
    if (collapsed) { root.setAttribute('data-nav', 'collapsed'); } else { root.removeAttribute('data-nav'); }
    if (col) { col.setAttribute('aria-expanded', collapsed ? 'false' : 'true'); }
    try { localStorage.setItem('mf-nav', collapsed ? 'collapsed' : 'open'); } catch (e) {}
    // Re-layout the charts (Sankey, investments) once the width animation has finished
    window.dispatchEvent(new Event('resize'));
    setTimeout(function () { window.dispatchEvent(new Event('resize')); }, 200);
  }
  if (col) {
    col.setAttribute('aria-expanded', root.getAttribute('data-nav') === 'collapsed' ? 'false' : 'true');
    col.addEventListener('click', function () { setNav(root.getAttribute('data-nav') !== 'collapsed'); });
  }
  var tc = document.getElementById('nav-theme-collapsed');
  if (tc) {
    tc.addEventListener('click', function () {
      setNav(false);
      var sel = document.getElementById('theme-toggle');
      if (sel) { sel.focus(); }
    });
  }
  var open = document.getElementById('nav-open');
  var side = document.getElementById('sidebar');
  if (open && side) {
    open.addEventListener('click', function () {
      side.classList.toggle('open');
      open.setAttribute('aria-expanded', side.classList.contains('open') ? 'true' : 'false');
    });
    document.addEventListener('click', function (e) {
      if (side.classList.contains('open') && !side.contains(e.target) && e.target !== open) {
        side.classList.remove('open'); open.setAttribute('aria-expanded', 'false');
      }
    });
  }
})();

// Confirmation before submitting: the text lives in data-confirm (HTML-escaped,
// so safe even with an apostrophe or quotes in the name) instead of an inline
// onsubmit, where a ' in a rule pattern broke the JavaScript string and the
// form was submitted WITHOUT asking (review 2026-10-10). Capture phase, so
// AJAX forms with their own submit listener ask as well.
document.addEventListener('submit', function (e) {
  var f = e.target, b = e.submitter;
  // data-confirm on the button wins (a form with several buttons asks only for the risky one)
  var text = (b && b.dataset && b.dataset.confirm) || (f && f.dataset && f.dataset.confirm);
  if (text && !window.confirm(text)) {
    e.preventDefault(); e.stopImmediatePropagation();
  }
}, true);

// Filters that submit themselves: class="autosubmit" on a select or checkbox,
// class="autosubmit-typing" on a search field (400 ms after the last key).
// form.submit() on purpose - no submit event, so no confirmation dialog.
document.addEventListener('change', function (e) {
  var el = e.target;
  if (el.classList && el.classList.contains('autosubmit') && el.form) { el.form.submit(); }
});
var typingTimer = null;
document.addEventListener('input', function (e) {
  var el = e.target;
  if (el.classList && el.classList.contains('autosubmit-typing') && el.form) {
    clearTimeout(typingTimer);
    typingTimer = setTimeout(function () { el.form.submit(); }, 400);
  }
});

// Period picker (templates/_period.html): "Eigener Zeitraum" reveals the
// from/to fields, every other choice submits right away. The comparison page
// has two pickers on one page, so each form is wired on its own.
document.querySelectorAll('form.period-picker').forEach(function (form) {
  var sel = form.querySelector('.pp-range'), box = form.querySelector('.pp-custom');
  if (!sel || !box) return;
  function custom(on) {
    box.hidden = !on;
    box.querySelectorAll('select').forEach(function (s) { s.disabled = !on; });
  }
  sel.addEventListener('change', function () {
    if (sel.value === 'custom') { custom(true); box.querySelector('select').focus(); return; }
    custom(false); form.submit();
  });
});

// Collapsible cards (<details data-persist="key">): the state is kept in the
// browser so everyone can arrange their dashboard the way they need it.
document.querySelectorAll('details[data-persist]').forEach(function (d) {
  var k = 'mf-open-' + d.dataset.persist;
  try { var v = localStorage.getItem(k); if (v === '0') { d.open = false; } else if (v === '1') { d.open = true; } } catch (e) {}
  d.addEventListener('toggle', function () { try { localStorage.setItem(k, d.open ? '1' : '0'); } catch (e) {} });
});

// Collapsing/expanding sub-rows, the same on every page: a button with
// class="tgl" and data-key="X" toggles all rows with class="sub-X".
// The initial state is the button text (down triangle = open, right triangle
// = closed) - so each page decides for itself whether it starts expanded or
// collapsed (dashboard open, year view closed, user request 2026-09-11).
document.querySelectorAll('.tgl').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var open = btn.textContent.trim() === '▾';
    btn.textContent = open ? '▸' : '▾';
    btn.setAttribute('aria-expanded', open ? 'false' : 'true');
    document.querySelectorAll('.sub-' + btn.dataset.key).forEach(function (tr) {
      tr.hidden = open;
    });
  });
});
