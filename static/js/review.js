(function(){
  const HKEY = 'reviewHistory';
  const RD = JSON.parse(document.getElementById('review-data').textContent);
  const curId = RD.id;
  let hist = [];
  try { hist = JSON.parse(sessionStorage.getItem(HKEY) || '[]'); } catch (e) { hist = []; }
  if (!RD.correction && hist[hist.length-1] !== curId) {
    hist.push(curId);
    sessionStorage.setItem(HKEY, JSON.stringify(hist));
  }
  const nextUrl = RD.next;
  // .cat-slot = the top, always visible row (standalone categories +
  // categories-with-subcategories as an expand button) - the number keys
  // mirror exactly what is visible, whether leaf category or expander.
  const btns = document.querySelectorAll('.cat-slot');
  // Digits on the first nine buttons so the key mapping is visible - ONCE on
  // load (until 2026-09-15 this was in the keydown handler: it only appeared
  // after the first keypress and doubled with every further one)
  btns.forEach((b, i) => { if (i < 9) b.insertAdjacentHTML('afterbegin',
    '<span class="mute" style="font-size:10px;margin-right:4px">' + (i+1) + '</span>'); });
  // Show "Letzte korrigieren" (fix the last one) only if there is a previous payment
  const uidx = hist.lastIndexOf(curId);
  const prevId = uidx > 0 ? hist[uidx-1] : (hist.length && !hist.includes(curId) ? hist[hist.length-1] : null);
  const undo = document.getElementById('review-undo');
  if (undo && prevId) { undo.hidden = false; undo.href = '/review?tx=' + prevId; }
  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT') return;
    if (e.key >= '1' && e.key <= '9' && btns[e.key-1]) { btns[e.key-1].click(); return; }
    if (e.key === '0' || e.key === 'ArrowRight') { location.href = nextUrl; return; }
    if (e.key === 'ArrowLeft' && prevId) { location.href = '/review?tx=' + prevId; }
  });
  // Category with subcategories: a click expands the subcategories instead of
  // booking directly (user request 2026-08-07: "sollte es mich dann gleich
  // fragen welche unterkategorie" - it should ask right away which
  // subcategory, and the answer is mandatory) - the parent category itself
  // is no longer a valid target.
  document.querySelectorAll('.cat-parent-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const panel = btn.nextElementSibling;
      const willOpen = panel.style.display !== 'flex';
      document.querySelectorAll('.cat-children').forEach(p => { p.style.display = 'none'; });
      panel.style.display = willOpen ? 'flex' : 'none';
    });
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('.cat-parent-wrap')) {
      document.querySelectorAll('.cat-children').forEach(p => { p.style.display = 'none'; });
    }
  });
})();
