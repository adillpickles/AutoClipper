// Copy buttons for the setup commands. The page works without JavaScript; the
// buttons stay hidden unless the Clipboard API is available.
(() => {
  if (!navigator.clipboard) return;
  for (const button of document.querySelectorAll("[data-copy]")) {
    const code = button.parentElement.querySelector("code");
    if (!code) continue;
    button.hidden = false;
    button.setAttribute("aria-label", `Copy command: ${code.textContent.split("\n")[0]}`);
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent.trim());
        button.textContent = "Copied";
        button.dataset.done = "";
      } catch {
        button.textContent = "Select and copy";
      }
      setTimeout(() => {
        button.textContent = "Copy";
        delete button.dataset.done;
      }, 1800);
    });
  }
})();
