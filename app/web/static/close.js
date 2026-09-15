(function () {
  "use strict";

  var fieldNames = {
    actor: "your name",
    reason: "a reason",
    evidence_note: "an evidence note",
    confirm_close: "close confirmation",
    confirm_prepare: "statement matrix confirmation",
    confirm_reopen: "reopen confirmation",
    confirm_acknowledgement: "acknowledgement confirmation",
    confirm_adjustment: "adjustment confirmation",
    confirm_withdrawal: "withdrawal confirmation"
  };

  function isComplete(field) {
    if (field.type === "checkbox" || field.type === "radio") {
      return field.checked;
    }
    return String(field.value || "").trim().length > 0;
  }

  function displayName(field) {
    return fieldNames[field.name] || field.name.replaceAll("_", " ");
  }

  document.querySelectorAll("form[data-required-submit]").forEach(function (form, index) {
    var submit = form.querySelector("[data-submit-control]");
    var requirements = form.querySelector("[data-form-requirements]");
    var requiredFields = Array.from(form.querySelectorAll("[required]"));
    var policyBlocked = form.dataset.policyBlocked === "true";
    var policyBlockedReason = form.dataset.policyBlockedReason || "";
    if (!submit || !requirements || requiredFields.length === 0) return;

    if (!requirements.id) requirements.id = "close-form-requirements-" + index;
    submit.setAttribute("aria-describedby", requirements.id);

    function refresh() {
      var missing = requiredFields.filter(function (field) {
        return !isComplete(field);
      });
      var ready = missing.length === 0 && !policyBlocked;
      submit.disabled = !ready;
      submit.setAttribute("aria-disabled", String(!ready));
      requirements.classList.toggle("is-ready", ready);
      if (policyBlocked) {
        requirements.textContent = policyBlockedReason;
      } else {
        requirements.textContent = ready
          ? "Required review details complete."
          : "To continue, add " + missing.map(displayName).join(", ") + ".";
      }
    }

    form.addEventListener("input", refresh);
    form.addEventListener("change", refresh);
    refresh();
  });
})();
