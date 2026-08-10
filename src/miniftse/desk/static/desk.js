// miniftse ops desk - small shared behaviour.
//
// Screen-specific interaction (the chaos console's live-drill fragment, the ask/draft
// forms) belongs with the screen that introduces it in a later task. This file only
// holds behaviour every page needs regardless of which screen it is: surfacing an HTMX
// transport failure instead of leaving a control looking like it silently did nothing.

document.body.addEventListener("htmx:responseError", (event) => {
  const target = event.detail.target;
  if (target) {
    target.insertAdjacentHTML(
      "beforeend",
      '<p class="htmx-error">Request failed - the server returned '
        + event.detail.xhr.status
        + ". Try again.</p>"
    );
  }
});

document.body.addEventListener("htmx:sendError", (event) => {
  const target = event.detail.target;
  if (target) {
    target.insertAdjacentHTML(
      "beforeend",
      '<p class="htmx-error">Could not reach the server. Check your connection and '
        + "try again.</p>"
    );
  }
});
