/* Part 7: start the page once every part is loaded. */
applyTheme(themeChoice());
if (PAUSED) setGo(false, "Searching is paused by the administrator.");
// Until the first answer, the page says it is loading (#starting). The site sleeps after a quiet
// spell and its first answer can then take a while (its database wakes too): after a few seconds
// it says so, so nobody takes the wait for a fault, and that the leads appear by themselves.
const wakingTimers = [
  setTimeout(() => {
    $("starting-text").textContent = "Starting up… The Lead Finder sleeps when nobody has used it for a " +
      "while, and the first visit can take up to a minute to wake it. Your leads appear here by themselves.";
  }, TIMING.waking),
  setTimeout(() => $("starting").append(el("span", "Still starting. If nothing appears within two minutes, " +
                                                   "reload the page.", "slow")), TIMING.stillWaking)];
(async () => {
  const { page, params } = parseHash();
  if (page === "leads") readLeadView(params);      // ask for the view in the address straight away
  await loadLeads();
  for (const t of wakingTimers) clearTimeout(t);
  $("starting").hidden = true;
  route();
  if (parseHash().page !== "find") loadSearches();   // the sidebar's Yelp count and the day's state
})();
