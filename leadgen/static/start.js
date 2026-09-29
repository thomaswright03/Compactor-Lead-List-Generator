/* Part 6: start the page once every part is loaded. */
applyTheme(themeChoice());
if (PAUSED) setGo(false, "Searching is paused by the administrator.", true);
(async () => { await loadLeads(); route(); if (parseHash().page !== "find") loadSearches(); })();
