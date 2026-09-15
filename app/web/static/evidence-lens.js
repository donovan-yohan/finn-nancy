/* Progressive enhancement for the split-screen statement verifier.
 *
 * Everything here is optional. Without it, rows and source regions are plain
 * anchors linked to each other, so :target still highlights and navigates.
 * This adds hover preview, multi-region highlighting, and paired scrolling.
 */
(function () {
  "use strict";
  var lens = document.getElementById("lens");
  if (!lens) return;

  var rows = Array.prototype.slice.call(lens.querySelectorAll(".lens-row"));
  var regions = Array.prototype.slice.call(lens.querySelectorAll(".region"));
  if (!rows.length) return;

  var pagesPane = document.getElementById("lens-pages");
  var rowsPane = document.getElementById("lens-rows");
  var activeRow = null;

  function regionsFor(number) {
    return regions.filter(function (node) {
      return node.getAttribute("data-row") === number;
    });
  }

  function rowFor(number) {
    return rows.filter(function (node) {
      return node.getAttribute("data-row") === number;
    })[0];
  }

  function clear() {
    rows.forEach(function (node) { node.classList.remove("is-active"); });
    regions.forEach(function (node) { node.classList.remove("is-active"); });
  }

  function scrollWithin(pane, node) {
    if (!pane || !node) return;
    var paneBox = pane.getBoundingClientRect();
    var nodeBox = node.getBoundingClientRect();
    if (nodeBox.top >= paneBox.top && nodeBox.bottom <= paneBox.bottom) return;
    pane.scrollTop += nodeBox.top - paneBox.top - paneBox.height / 3;
  }

  function select(number, options) {
    var settings = options || {};
    clear();
    var row = rowFor(number);
    var matches = regionsFor(number);
    if (row) row.classList.add("is-active");
    matches.forEach(function (node) { node.classList.add("is-active"); });
    activeRow = number;
    if (settings.scrollPages !== false && matches.length) {
      scrollWithin(pagesPane, matches[0]);
    }
    if (settings.scrollRows && row) scrollWithin(rowsPane, row);
  }

  function bind(nodes, options) {
    nodes.forEach(function (node) {
      var number = node.getAttribute("data-row");
      node.addEventListener("mouseenter", function () { select(number, options); });
      node.addEventListener("focus", function () { select(number, options); });
      node.addEventListener("click", function (event) {
        event.preventDefault();
        select(number, options);
      });
    });
  }

  bind(rows, { scrollPages: true });
  bind(regions, { scrollRows: true, scrollPages: false });

  lens.addEventListener("keydown", function (event) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    var index = rows.findIndex(function (node) {
      return node.getAttribute("data-row") === activeRow;
    });
    var next = rows[index + (event.key === "ArrowDown" ? 1 : -1)];
    if (!next) return;
    event.preventDefault();
    next.focus();
  });

  // Deep links such as #row-47 arrive selected, so the reviewer lands on the
  // evidence rather than having to hunt for it.
  var hash = (window.location.hash || "").match(/^#row-(\d+)$/);
  if (hash) select(hash[1], { scrollPages: true, scrollRows: true });
})();
