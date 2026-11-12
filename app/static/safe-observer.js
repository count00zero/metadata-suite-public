/* Firefox + extensions (autofill, password managers, ...) inject nodes that page scripts may not touch
   ("Permission denied to access property nodeType"). Alpine's MutationObserver then throws and stops
   processing the rest of the batch. This wrapper (loaded BEFORE Alpine) makes each observer callback
   fail-soft: the nodes that cannot be read are skipped, everything else keeps working. */
(function () {
  var Native = window.MutationObserver;
  if (!Native) return;
  function SafeMO(cb) {
    return new Native(function (records, obs) {
      var ok = [];
      for (var i = 0; i < records.length; i++) {
        try {
          var r = records[i], nodes = r.addedNodes;
          for (var j = 0; nodes && j < nodes.length; j++) void nodes[j].nodeType;   // throws for foreign nodes
          nodes = r.removedNodes;
          for (var k = 0; nodes && k < nodes.length; k++) void nodes[k].nodeType;
          void r.target.nodeType;
          ok.push(r);
        } catch (e) { /* foreign (extension) node: skip this record */ }
      }
      if (!ok.length) return;
      try { cb(ok, obs); } catch (e) { if (window.console) console.warn("MutationObserver callback:", e); }
    });
  }
  SafeMO.prototype = Native.prototype;
  window.MutationObserver = SafeMO;
})();
