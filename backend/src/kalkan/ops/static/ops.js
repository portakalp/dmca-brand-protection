// Keyboard shortcuts of the ops panel (architecture.md §19): on a page with a label form,
// 1-6 pick the category and save, N skips, / goes to the note, Esc leaves a field. On the
// review page every decision button carries its key sequence in data-keys (A then P, R then
// 1-6, V, W, C); a key that starts a sequence waits for the next one, Esc cancels it.
(function () {
  "use strict";

  function typing(target) {
    return target instanceof HTMLElement &&
      (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
  }

  // Copy buttons of the manual tasks: data-copy names the element whose text is copied.
  document.addEventListener("click", function (event) {
    var button = event.target instanceof Element ? event.target.closest("[data-copy]") : null;
    if (!button) return;
    var source = document.getElementById(button.getAttribute("data-copy"));
    if (source && navigator.clipboard) {
      navigator.clipboard.writeText(source.textContent || "").then(function () {
        button.textContent = "Copied";
      });
    }
  });

  var pending = "";

  function showPending(form) {
    var hint = document.getElementById("key-pending");
    if (hint) hint.textContent = pending ? "waiting: " + pending.toUpperCase() : "";
    form.classList.toggle("pending", pending !== "");
    form.querySelectorAll("button[data-keys]").forEach(function (button) {
      button.classList.toggle("match", pending !== "" && button.dataset.keys.indexOf(pending) === 0);
    });
  }

  function reviewKey(form, event) {
    var key = event.key.toLowerCase();
    if (key === "escape") {
      pending = "";
      showPending(form);
      return;
    }
    if (key === "n" && pending === "") {
      var skip = document.getElementById("skip");
      if (skip) {
        event.preventDefault();
        window.location.assign(skip.href);
      }
      return;
    }
    var sequence = pending + key;
    var buttons = Array.prototype.slice.call(form.querySelectorAll("button[data-keys]"));
    var exact = buttons.filter(function (button) { return button.dataset.keys === sequence; });
    if (exact.length === 1) {
      event.preventDefault();
      pending = "";
      showPending(form);
      form.requestSubmit(exact[0]);
      return;
    }
    var started = buttons.some(function (button) { return button.dataset.keys.indexOf(sequence) === 0; });
    pending = started ? sequence : "";
    if (started) event.preventDefault();
    showPending(form);
  }

  document.addEventListener("keydown", function (event) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "Escape" && typing(event.target)) {
      event.target.blur();
      return;
    }
    if (typing(event.target)) return;
    var review = document.getElementById("review-form");
    if (review) {
      reviewKey(review, event);
      return;
    }
    var form = document.getElementById("label-form");
    if (!form) return;
    if (/^[1-6]$/.test(event.key)) {
      var button = form.querySelector('button[data-key="' + event.key + '"]');
      if (button) {
        event.preventDefault();
        form.requestSubmit(button);
      }
    } else if (event.key === "n" || event.key === "N") {
      var skip = document.getElementById("skip");
      if (skip) {
        event.preventDefault();
        window.location.assign(skip.href);
      }
    } else if (event.key === "/") {
      var note = document.getElementById("label-note");
      if (note) {
        event.preventDefault();
        note.focus();
      }
    }
  });
})();
