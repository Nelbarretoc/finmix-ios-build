(() => {
  try {
    const saved = JSON.parse(localStorage.getItem("finmix-appearance") || "{}");
    const mode = ["light", "dark", "system"].includes(saved.mode)
      ? saved.mode
      : "system";
    document.documentElement.dataset.theme =
      mode === "system"
        ? matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light"
        : mode;
  } catch {
    /* Keep the readable default if browser storage is unavailable. */
  }
})();
