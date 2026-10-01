// Progressive enhancement only: the dashboard works without JavaScript.
// Shows the "publish at" field when "Scheduled public" is chosen.
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t instanceof HTMLInputElement && t.name === "visibility") {
    const box = document.getElementById("when");
    if (box) box.classList.toggle("show", t.value === "scheduled_public");
  }
});
