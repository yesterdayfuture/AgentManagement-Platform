"""读写只有一张表的 xlsx。样例和导入都不依赖第三方表格库。"""

import zipfile
from io import BytesIO
from xml.etree import ElementTree as ET

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _col(index: int) -> str:
    name = ""
    number = index + 1
    while number:
        number, rest = divmod(number - 1, 26)
        name = chr(65 + rest) + name
    return name


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def workbook(rows: list[list[str]]) -> bytes:
    strings = []
    index = {}
    cells = []
    for row_no, row in enumerate(rows, start=1):
        refs = []
        for col_no, value in enumerate(row):
            text = "" if value is None else str(value)
            if text not in index:
                index[text] = len(strings)
                strings.append(text)
            refs.append(f'<c r="{_col(col_no)}{row_no}" t="s"><v>{index[text]}</v></c>')
        cells.append(f'<row r="{row_no}">{"".join(refs)}</row>')
    shared = "".join(f"<si><t>{_escape(item)}</t></si>" for item in strings)
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{_NS}"><sheetData>{"".join(cells)}</sheetData></worksheet>'
    )
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="{_NS}" count="{len(strings)}" uniqueCount="{len(strings)}">{shared}</sst>'
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{_PKG}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<workbook xmlns="{_NS}" xmlns:r="{_REL}">'
            '<sheets><sheet name="用例" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{_PKG}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": sheet,
        "xl/sharedStrings.xml": shared_xml,
    }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _text(node) -> str:
    if node is None:
        return ""
    return "".join(node.itertext())


def read_rows(data: bytes) -> list[list[str]]:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
        sheet_name = next((name for name in names if name.startswith("xl/worksheets/sheet")), None)
        if sheet_name is None:
            raise ValueError("表格里没有工作表")
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [_text(item) for item in root]
        root = ET.fromstring(archive.read(sheet_name))
    rows = []
    for row in root.findall(f".//{{{_NS}}}row"):
        values = []
        for cell in row.findall(f"{{{_NS}}}c"):
            kind = cell.attrib.get("t")
            if kind == "inlineStr":
                values.append(_text(cell.find(f"{{{_NS}}}is")))
            elif kind == "s":
                raw = _text(cell.find(f"{{{_NS}}}v"))
                values.append(shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else "")
            else:
                values.append(_text(cell.find(f"{{{_NS}}}v")))
        rows.append(values)
    return rows


HEADERS = {"输入": "input_text", "期望": "expected", "期望工具": "expected_tool"}


def cases_of(data: bytes) -> list[dict]:
    rows = read_rows(data)
    if not rows:
        raise ValueError("表格是空的")
    header = [item.strip() for item in rows[0]]
    missing = [name for name in ("输入", "期望") if name not in header]
    if missing:
        raise ValueError("表头需要包含：输入、期望。期望工具可以留空。")
    found = []
    for row in rows[1:]:
        padded = row + [""] * (len(header) - len(row))
        item = {HEADERS[name]: padded[index].strip() for index, name in enumerate(header) if name in HEADERS}
        if not item.get("input_text") and not item.get("expected"):
            continue
        if not item.get("input_text") or not item.get("expected"):
            raise ValueError("每一行都要填写输入和期望")
        found.append(
            {
                "input_text": item["input_text"],
                "expected": item["expected"],
                "expected_tool": item.get("expected_tool") or "",
            }
        )
    if not found:
        raise ValueError("没有读到用例")
    return found
