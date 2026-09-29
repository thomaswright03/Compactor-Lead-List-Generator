"""A small, fast Excel (.xlsx) writer for the lead downloads.

openpyxl builds a cell object per value and writes its XML one element at a
time, so 10,000 saved leads took several seconds. This writes the same file
straight as text, in time that grows in step with the rows (10,000 leads in
well under a second). It covers only what the downloads use: text and number
cells, a few cell styles (bold header, fill colours, link font, text that stays
text), column widths, a frozen header, a filter, one dropdown list per sheet
and web links. The files open in Excel, LibreOffice, Google Sheets and openpyxl
(the tests read them back with openpyxl).
"""

import io
import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from xml.sax.saxutils import escape, quoteattr

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_DOC = "application/vnd.openxmlformats-officedocument.spreadsheetml"
_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
# Characters XML 1.0 can't hold at all (a tab, new line and carriage return are fine).
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Text starting with these would be taken for a formula if someone edits the cell.
_FORMULA_START = ("=", "+", "-", "@")

FONTS = {
    "header": '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>',
    "link": '<font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/></font>',
}


@dataclass
class Cell:
    """A value with a style: fill is a hex colour ("C6EFCE"), font a FONTS name,
    link a web address the cell opens, wrap wraps the text (centred vertically)."""

    value: object = None
    fill: str | None = None
    font: str | None = None
    link: str | None = None
    wrap: bool = False


@lru_cache(maxsize=1024)
def column_letter(n: int) -> str:
    """1 -> "A", 27 -> "AA"."""
    letters = ""
    while n:
        n, rest = divmod(n - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def clean(text: str) -> str:
    """Text without the control characters a spreadsheet can't hold."""
    return _ILLEGAL.sub("", text)


class Sheet:
    def __init__(self, book: "Book", name: str, widths: list[float], freeze: str | None = None) -> None:
        self.book, self.name, self.widths, self.freeze = book, name, widths, freeze
        self.rows: list[str] = []        # each row's finished XML
        self.links: list[tuple[str, str]] = []   # (cell reference, address)
        self.filter = False
        self.dropdown: tuple[int, Sequence[str]] | None = None   # (column number, choices)

    def append(self, values: Iterable[object]) -> None:
        """Add a row: plain values (None or "" leave the cell empty) or Cells."""
        r = len(self.rows) + 1
        out = []
        for i, value in enumerate(values):
            cell = value if isinstance(value, Cell) else None
            if cell is not None:
                value = cell.value
            if value is None or value == "":
                if cell is None or not (cell.fill or cell.font):
                    continue
                value = None
            ref = f"{column_letter(i + 1)}{r}"
            style = self.book.style(cell, quote=isinstance(value, str) and value.startswith(_FORMULA_START))
            s = f' s="{style}"' if style else ""
            if value is None:
                out.append(f'<c r="{ref}"{s}/>')
            elif isinstance(value, str):
                out.append(f'<c r="{ref}"{s} t="s"><v>{self.book.text(clean(value))}</v></c>')
            elif isinstance(value, bool):
                out.append(f'<c r="{ref}"{s} t="b"><v>{int(value)}</v></c>')
            else:
                out.append(f'<c r="{ref}"{s}><v>{value!r}</v></c>')
            if cell is not None and cell.link:
                self.links.append((ref, clean(cell.link)))
        self.rows.append(f'<row r="{r}">{"".join(out)}</row>')

    def _last(self) -> str:
        return f"{column_letter(max(len(self.widths), 1))}{max(len(self.rows), 1)}"

    def xml(self) -> str:
        parts = [_HEAD, f'<worksheet xmlns="{_MAIN}" xmlns:r="{_REL}">',
                 f'<dimension ref="A1:{self._last()}"/><sheetViews><sheetView workbookViewId="0">']
        if self.freeze:
            col, row = re.fullmatch(r"([A-Z]+)(\d+)", self.freeze).groups()  # type: ignore[union-attr]
            x, y = sum((ord(c) - 64) * 26 ** i for i, c in enumerate(reversed(col))) - 1, int(row) - 1
            parts.append(f'<pane xSplit="{x}" ySplit="{y}" topLeftCell="{self.freeze}" '
                         'activePane="bottomRight" state="frozen"/>'
                         f'<selection pane="bottomRight" activeCell="{self.freeze}" '
                         f'sqref="{self.freeze}"/>')
        parts.append('</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>')
        if self.widths:
            parts.append("<cols>" + "".join(
                f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                for i, w in enumerate(self.widths, start=1)) + "</cols>")
        parts.append("<sheetData>")
        parts += self.rows
        parts.append("</sheetData>")
        if self.filter:
            parts.append(f'<autoFilter ref="A1:{self._last()}"/>')
        if self.dropdown and len(self.rows) > 1:
            col, choices = self.dropdown
            letter = column_letter(col)
            listed = escape('"' + ",".join(choices) + '"')
            parts.append('<dataValidations count="1"><dataValidation type="list" allowBlank="1" '
                         f'showErrorMessage="1" sqref="{letter}2:{letter}{len(self.rows)}">'
                         f"<formula1>{listed}</formula1></dataValidation></dataValidations>")
        if self.links:
            parts.append("<hyperlinks>" + "".join(
                f'<hyperlink ref="{ref}" r:id="rId{n}"/>'
                for n, (ref, _) in enumerate(self.links, start=1)) + "</hyperlinks>")
        parts.append('<pageMargins left="0.75" right="0.75" top="1" bottom="1" header="0.5" '
                     'footer="0.5"/></worksheet>')
        return "".join(parts)

    def rels(self) -> str:
        return (_HEAD + f'<Relationships xmlns="{_PKG}">' + "".join(
            f'<Relationship Id="rId{n}" Type="{_REL}/hyperlink" Target={quoteattr(url)} '
            'TargetMode="External"/>' for n, (_, url) in enumerate(self.links, start=1))
            + "</Relationships>")


class Book:
    def __init__(self) -> None:
        self.sheets: list[Sheet] = []
        self._texts: dict[str, int] = {}   # shared text -> its number
        self._fills: list[str] = []      # colours, in order (fills 0 and 1 are Excel's own)
        self._fonts: list[str] = []      # FONTS names, in order (font 0 is the default)
        self._styles: dict[tuple[str | None, str | None, bool, bool], int] = {(None, None, False, False): 0}

    def add_sheet(self, name: str, widths: Iterable[float] = (), freeze: str | None = None) -> Sheet:
        sheet = Sheet(self, name, list(widths), freeze)
        self.sheets.append(sheet)
        return sheet

    def text(self, value: str) -> int:
        n = self._texts.get(value)
        if n is None:
            n = self._texts[value] = len(self._texts)
        return n

    def style(self, cell: Cell | None, quote: bool = False) -> int:
        key = ((cell.fill, cell.font, cell.wrap) if cell else (None, None, False)) + (quote,)
        n = self._styles.get(key)
        if n is None:
            fill, font, _, _ = key
            if fill and fill not in self._fills:
                self._fills.append(fill)
            if font and font not in self._fonts:
                self._fonts.append(font)
            n = self._styles[key] = len(self._styles)
        return n

    def _styles_xml(self) -> str:
        fonts = ['<font><sz val="11"/><name val="Calibri"/></font>'] + [FONTS[f] for f in self._fonts]
        fills = ['<fill><patternFill patternType="none"/></fill>',
                 '<fill><patternFill patternType="gray125"/></fill>'] + [
            f'<fill><patternFill patternType="solid"><fgColor rgb="FF{c}"/><bgColor indexed="64"/>'
            "</patternFill></fill>" for c in self._fills]
        xfs = []
        for (fill, font, wrap, quote), _ in sorted(self._styles.items(), key=lambda kv: kv[1]):
            font_id = self._fonts.index(font) + 1 if font else 0
            fill_id = self._fills.index(fill) + 2 if fill else 0
            attrs = f'numFmtId="0" fontId="{font_id}" fillId="{fill_id}" borderId="0" xfId="0"'
            attrs += ' applyFont="1"' if font else ""
            attrs += ' applyFill="1"' if fill else ""
            attrs += ' quotePrefix="1"' if quote else ""
            if wrap:
                xfs.append(f'<xf {attrs} applyAlignment="1"><alignment vertical="center" '
                           'wrapText="1"/></xf>')
            else:
                xfs.append(f"<xf {attrs}/>")
        return (_HEAD + f'<styleSheet xmlns="{_MAIN}">'
                f'<fonts count="{len(fonts)}">{"".join(fonts)}</fonts>'
                f'<fills count="{len(fills)}">{"".join(fills)}</fills>'
                '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                "</styleSheet>")

    def _texts_xml(self) -> str:
        items = "".join(f'<si><t xml:space="preserve">{escape(t)}</t></si>' for t in self._texts)
        return (_HEAD + f'<sst xmlns="{_MAIN}" count="{len(self._texts)}" '
                f'uniqueCount="{len(self._texts)}">{items}</sst>')

    def _workbook_xml(self) -> str:
        sheets = "".join(f'<sheet name={quoteattr(s.name)} sheetId="{n}" r:id="rId{n}"/>'
                         for n, s in enumerate(self.sheets, start=1))
        names = "".join(
            f'<definedName name="_xlnm._FilterDatabase" localSheetId="{n}" hidden="1">'
            f"{escape(_quoted(s.name))}!$A$1:${column_letter(max(len(s.widths), 1))}"
            f"${max(len(s.rows), 1)}</definedName>"
            for n, s in enumerate(self.sheets) if s.filter)
        return (_HEAD + f'<workbook xmlns="{_MAIN}" xmlns:r="{_REL}">'
                '<bookViews><workbookView/></bookViews>'
                f"<sheets>{sheets}</sheets>"
                + (f"<definedNames>{names}</definedNames>" if names else "") + "</workbook>")

    def save(self) -> bytes:
        """The finished file, as bytes."""
        n = len(self.sheets)
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="{_DOC}.worksheet+xml"/>'
            for i in range(1, n + 1))
        types = (_HEAD + f'<Types xmlns="{_TYPES}">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 f'<Override PartName="/xl/workbook.xml" ContentType="{_DOC}.sheet.main+xml"/>'
                 f"{overrides}"
                 f'<Override PartName="/xl/styles.xml" ContentType="{_DOC}.styles+xml"/>'
                 f'<Override PartName="/xl/sharedStrings.xml" ContentType="{_DOC}.sharedStrings+xml"/>'
                 "</Types>")
        root_rels = (_HEAD + f'<Relationships xmlns="{_PKG}"><Relationship Id="rId1" '
                     f'Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        book_rels = (_HEAD + f'<Relationships xmlns="{_PKG}">' + "".join(
            f'<Relationship Id="rId{i}" Type="{_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, n + 1))
            + f'<Relationship Id="rId{n + 1}" Type="{_REL}/styles" Target="styles.xml"/>'
            + f'<Relationship Id="rId{n + 2}" Type="{_REL}/sharedStrings" Target="sharedStrings.xml"/>'
            + "</Relationships>")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", types)
            z.writestr("_rels/.rels", root_rels)
            z.writestr("xl/workbook.xml", self._workbook_xml())
            z.writestr("xl/_rels/workbook.xml.rels", book_rels)
            for i, sheet in enumerate(self.sheets, start=1):
                z.writestr(f"xl/worksheets/sheet{i}.xml", sheet.xml())
                if sheet.links:
                    z.writestr(f"xl/worksheets/_rels/sheet{i}.xml.rels", sheet.rels())
            z.writestr("xl/styles.xml", self._styles_xml())
            z.writestr("xl/sharedStrings.xml", self._texts_xml())
        return buf.getvalue()


def _quoted(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"
