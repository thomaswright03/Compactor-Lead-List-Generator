# Leaflet 1.9.4 (vendored)

The Map page's map library, served from here (not a CDN) so the page doesn't depend on
another site being up. Only the Map page loads it, the first time it is opened
(`leadgen/static/map.js`).

- Source: the npm package `leaflet@1.9.4` (`npm pack leaflet@1.9.4`; tarball SHA-1
  `23fae724e282fa25745aff82ca4d394748db7d8d`, as the npm registry lists it), files
  `dist/leaflet.js`, `dist/leaflet.css` and `LICENSE`, unchanged.
- Licence: BSD 2-Clause, © 2010-2023 Volodymyr Agafonkin, © 2010-2011 CloudMade (`LICENSE`).
- Left out: `dist/images/` (the default marker and the layers control's icons; the
  Map page draws its own pins and toggles) and the source map (`leaflet.js` still names
  `leaflet.js.map` on its last line, which only a browser's developer tools ask for).
- To update: unpack the new version's package and copy the same three files here, then
  check the Map page (`tests/test_browser.py`: `test_the_map_shows_aarco_its_area_the_areas_and_every_saved_business`
  and `test_the_map_takes_the_dark_theme_and_fits_a_phone`). Leaflet takes the global names `L` and `leaflet`
  for itself, so no page script may use them.
