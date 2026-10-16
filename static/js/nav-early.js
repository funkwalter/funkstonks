// Restore the collapsed sidebar BEFORE the first render - otherwise the
// layout briefly jumps on every page change.
try { if (localStorage.getItem('mf-nav') === 'collapsed') { document.documentElement.setAttribute('data-nav', 'collapsed'); } } catch (e) {}
