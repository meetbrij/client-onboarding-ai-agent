// Busy state for slow forms. No framework, no inline code, no network access.
// A form marked data-busy="Label..." shows a spinner on its button, locks its fields right after the browser has read
// them, and refuses a second submit, so a slow request (a case can take a minute) is not sent twice.
(function () {
  "use strict";

  function fieldsOf(form) {
    return Array.prototype.filter.call(form.elements, function (el) {
      return el.type !== "hidden";
    });
  }

  function lock(form, button) {
    form.dataset.submitting = "true";
    form.classList.add("submitting");
    form.setAttribute("aria-busy", "true");
    var note = form.querySelector(".busy-note");
    if (note) {
      note.hidden = false;
    }
    if (button) {
      button.dataset.label = button.textContent;
      button.textContent = form.getAttribute("data-busy");
      button.classList.add("loading");
    }
    // Disabled controls are not submitted, so lock them only after the browser has built the form data.
    window.setTimeout(function () {
      fieldsOf(form).forEach(function (el) {
        el.disabled = true;
      });
    }, 0);
  }

  function unlock(form) {
    delete form.dataset.submitting;
    form.classList.remove("submitting");
    form.removeAttribute("aria-busy");
    var note = form.querySelector(".busy-note");
    if (note) {
      note.hidden = true;
    }
    fieldsOf(form).forEach(function (el) {
      el.disabled = false;
      if (el.dataset && el.dataset.label) {
        el.textContent = el.dataset.label;
        el.classList.remove("loading");
        delete el.dataset.label;
      }
    });
  }

  document.addEventListener(
    "submit",
    function (event) {
      var form = event.target;
      if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-busy")) {
        return;
      }
      if (form.dataset.submitting === "true") {
        event.preventDefault(); // already sent: ignore a second click or Enter
        return;
      }
      lock(form, event.submitter || form.querySelector("button[type=submit]"));
    },
    true
  );

  // Coming back with the browser's Back button restores the page from cache: give the form back to the user.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      Array.prototype.forEach.call(document.querySelectorAll("form[data-busy]"), unlock);
    }
  });
})();
