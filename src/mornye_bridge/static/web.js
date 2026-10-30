"use strict";
document.querySelectorAll("[data-copy]").forEach(button => {
  button.addEventListener("click", async () => {
    const input = document.getElementById(button.dataset.copy);
    try {
      await navigator.clipboard.writeText(input.value);
      button.textContent = "Copied";
      setTimeout(() => { button.textContent = "Copy"; }, 1800);
    } catch (_) {
      input.focus(); input.select(); button.textContent = "Select & copy";
    }
  });
});
const pending = document.querySelector("[data-pending]");
if (pending) {
  const deadline = Date.now() + 180000;
  const poll = async () => {
    if (Date.now() > deadline) { pending.textContent = "Login wait expired. Start verification again if needed."; return; }
    if (!document.hidden) {
      try {
        const body = new URLSearchParams({csrf: pending.dataset.csrf, operation: "poll"});
        const response = await fetch(pending.dataset.pending, {method: "POST", body, redirect: "error"});
        if (response.ok && !(await response.json()).pending) { location.reload(); return; }
      } catch (_) { /* Retry quietly; never log sensitive response data. */ }
    }
    setTimeout(poll, 4000);
  };
  setTimeout(poll, 4000);
}
