/* Part 7: start the page once every part is loaded. */
applyTheme(themeChoice());
if (PAUSED) setGo(false, "Searching is paused by the administrator.");
(async () => {
  const { page, params } = parseHash();
  if (page === "leads") readLeadView(params);      // ask for the view in the address straight away
  await loadLeads();
  route();
  if (parseHash().page !== "find") loadSearches();   // the sidebar's Yelp count and the day's state
})();
