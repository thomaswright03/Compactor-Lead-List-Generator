# Fraunces and Inter (vendored)

The site's two typefaces, served from here (not Google Fonts or a CDN) so the pages don't
depend on another site being up. `leadgen/static/app.css` and the login and error pages
declare them with `@font-face`; anything outside the Latin set falls back to the system font.

- Fraunces (titles, business names, big numbers): the npm package
  `@fontsource-variable/fraunces@5.3.0` (`npm pack`; tarball SHA-1
  `6c8a69a0dcac753643a04e068ed2ad5689e99229`), files `files/fraunces-latin-opsz-normal.woff2`
  and `files/fraunces-latin-opsz-italic.woff2` (weight 100 to 900 and optical size), unchanged.
- Inter (everything else): the npm package `@fontsource-variable/inter@5.3.0` (tarball SHA-1
  `351dd1e02dab63a6cf66d57ec36dcfd10c07f07b`), file `files/inter-latin-wght-normal.woff2`
  (weight 100 to 900), unchanged.
- Licence: SIL Open Font License 1.1 (`LICENSE-Fraunces.txt`, `LICENSE-Inter.txt`).
- To update: unpack the new versions' packages and copy the same files here.
