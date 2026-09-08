(function () {
  "use strict";

  var sessionKey = "sgx-dairy-access-granted";
  var isAuthorized = false;

  document.documentElement.style.visibility = "hidden";

  try {
    isAuthorized = window.sessionStorage.getItem(sessionKey) === "true";
  } catch (error) {
    // Continue with the access gate when session storage is unavailable.
  }

  if (isAuthorized) {
    document.documentElement.style.visibility = "";
    return;
  }

  function showAccessGate() {
    var style = document.createElement("style");
    style.id = "site-access-gate-style";
    style.textContent = [
      "#site-access-gate{width:min(380px,calc(100vw - 32px));padding:0;border:0;border-radius:18px;color:#101828;background:#fff;box-shadow:0 24px 64px rgba(16,24,40,.28);font-family:Inter,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}",
      "#site-access-gate::backdrop{background:#f4f7fb}",
      "#site-access-gate form{padding:30px}",
      "#site-access-gate h1{margin:0 0 8px;font-size:24px;line-height:1.25;letter-spacing:-.02em}",
      "#site-access-gate p{margin:0 0 20px;color:#667085;font-size:13px;line-height:1.5}",
      "#site-access-gate input{display:block;width:100%;height:44px;padding:0 13px;border:1px solid #d0d5dd;border-radius:9px;color:#101828;background:#fff;font:600 16px inherit;outline:none}",
      "#site-access-gate input:focus{border-color:#175cd3;box-shadow:0 0 0 4px rgba(23,92,211,.12)}",
      "#site-access-gate button{width:100%;height:44px;margin-top:12px;border:0;border-radius:9px;color:#fff;background:#175cd3;font:700 14px inherit;cursor:pointer}",
      "#site-access-gate button:hover{background:#1849a9}",
      "#site-access-error{min-height:20px;margin:10px 0 0!important;color:#b42318!important;font-weight:650}"
    ].join("");

    var dialog = document.createElement("dialog");
    dialog.id = "site-access-gate";
    dialog.setAttribute("aria-labelledby", "site-access-question");
    dialog.innerHTML = [
      "<form method='dialog'>",
      "<h1 id='site-access-question'>milk solid %?</h1>",
      "<p>Enter the answer to access the dashboard.</p>",
      "<input id='site-access-answer' type='password' autocomplete='off' spellcheck='false' aria-label='Answer'>",
      "<button type='submit'>Enter website</button>",
      "<p id='site-access-error' role='alert' aria-live='polite'></p>",
      "</form>"
    ].join("");

    document.head.appendChild(style);
    document.body.appendChild(dialog);
    document.documentElement.style.visibility = "";

    var form = dialog.querySelector("form");
    var answer = dialog.querySelector("#site-access-answer");
    var errorMessage = dialog.querySelector("#site-access-error");

    dialog.addEventListener("cancel", function (event) {
      event.preventDefault();
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();

      if (answer.value.trim() !== "12%") {
        errorMessage.textContent = "Incorrect answer. Please try again.";
        answer.select();
        return;
      }

      try {
        window.sessionStorage.setItem(sessionKey, "true");
      } catch (error) {
        // The current page can still be viewed without persisting the session.
      }

      dialog.close();
      dialog.remove();
      style.remove();
    });

    dialog.showModal();
    answer.focus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", showAccessGate, { once: true });
  } else {
    showAccessGate();
  }
})();
