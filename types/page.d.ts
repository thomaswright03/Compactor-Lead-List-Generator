// What the page's scripts (leadgen/static/*.js) use that no script declares: the theme boot
// script in templates/_theme.html, which runs before them.
interface Window {
  /** Point the browser's bar colour at the theme shown ("light", "dark" or "system"). */
  setThemeColor(choice: string): void;
}
