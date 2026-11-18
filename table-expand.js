/* Expand/collapse long Cause, Affected, Start, ETR, and Updated table cells. */
(function () {
  document.addEventListener("click", function (event) {
    var button = event.target.closest(".clamp-toggle");
    if (!button) return;
    var cell = button.closest(".clamp-cell");
    if (!cell) return;
    var expanded = cell.classList.toggle("is-expanded");
    button.setAttribute("aria-expanded", expanded ? "true" : "false");
    button.setAttribute("aria-label", expanded ? "Show less" : "Show full text");
    button.textContent = expanded ? "less" : "…";
  });
})();
