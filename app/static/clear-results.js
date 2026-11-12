// A new scan ("Vorschau laden" / "Scannen" / ...) starts a new run: clear the previous run's
// "Ergebnis" panel immediately, so stale results never sit next to a fresh preview.
document.addEventListener('htmx:beforeRequest', function (evt) {
  var elt = evt.detail && evt.detail.elt;
  if (elt && elt.id === 'browse-form') {
    var panel = document.getElementById('results-panel');
    if (panel) panel.innerHTML = '';
  }
});

// Bring a freshly started progress panel into view (otherwise it appears below the fold and
// the person has to scroll down by hand); when the run finishes, scroll to its result.
(function () {
  var shown = false, holder = null;
  document.addEventListener('htmx:afterSwap', function () {
    var p = document.querySelector('.progress-panel');
    if (p) {
      if (!shown) {
        shown = true;
        holder = p.closest('section.panel') || p;
        try { holder.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (e) { p.scrollIntoView(); }
      }
    } else if (shown) {
      shown = false;
      if (holder && holder.isConnected) {
        try { holder.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (e) {}
      }
      holder = null;
    }
  });
})();
