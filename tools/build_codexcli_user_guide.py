#!/usr/bin/env python3
"""Build the CodexCLI user guide DOCX using only the Python standard library."""

from __future__ import annotations

import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
DCTERMS_NS = "http://purl.org/dc/terms/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"

for prefix, namespace in (
    ("w", W_NS),
    ("r", R_NS),
    ("cp", CP_NS),
    ("dc", DC_NS),
    ("dcterms", DCTERMS_NS),
    ("xsi", XSI_NS),
    ("vt", VT_NS),
):
    ET.register_namespace(prefix, namespace)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "codexcli-user-guide.md"
OUTPUT = ROOT / "docs" / "CodexCLI-User-Guide.docx"

NAVY = "17365D"
TEAL = "0B7285"
TEXT = "243447"
MUTED = "5B6573"
LIGHT = "EEF4F6"
CODE_BG = "F4F6F8"
WHITE = "FFFFFF"


def qn(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def w(name: str) -> str:
    return qn(W_NS, name)


def attr(name: str) -> str:
    return qn(W_NS, name)


def sub(parent: ET.Element, name: str, **attributes: str) -> ET.Element:
    return ET.SubElement(parent, w(name), {attr(key): value for key, value in attributes.items()})


def serialize(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def add_run(
    paragraph: ET.Element,
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
    color: str | None = None,
    size: int | None = None,
) -> None:
    run = sub(paragraph, "r")
    if bold or italic or code or color or size:
        properties = sub(run, "rPr")
        if bold:
            sub(properties, "b")
        if italic:
            sub(properties, "i")
        if code:
            ET.SubElement(
                properties,
                w("rFonts"),
                {
                    attr("ascii"): "Cascadia Mono",
                    attr("hAnsi"): "Cascadia Mono",
                    attr("eastAsia"): "Consolas",
                },
            )
            sub(properties, "color", val=NAVY)
            sub(properties, "shd", val="clear", color="auto", fill="E8EEF3")
        if color:
            sub(properties, "color", val=color)
        if size:
            sub(properties, "sz", val=str(size))
            sub(properties, "szCs", val=str(size))
    node = sub(run, "t")
    if text.startswith(" ") or text.endswith(" ") or "  " in text:
        node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    node.text = text


INLINE = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)")


def add_inline(paragraph: ET.Element, text: str) -> None:
    position = 0
    for match in INLINE.finditer(text):
        if match.start() > position:
            add_run(paragraph, text[position : match.start()])
        token = match.group(0)
        if token.startswith("`"):
            add_run(paragraph, token[1:-1], code=True)
        elif token.startswith("**"):
            add_run(paragraph, token[2:-2], bold=True)
        else:
            add_run(paragraph, token[1:-1], italic=True)
        position = match.end()
    if position < len(text):
        add_run(paragraph, text[position:])


def paragraph(
    body: ET.Element,
    text: str = "",
    *,
    style: str = "Normal",
    numbering: int | None = None,
    keep_next: bool = False,
) -> ET.Element:
    node = sub(body, "p")
    properties = sub(node, "pPr")
    sub(properties, "pStyle", val=style)
    if keep_next:
        sub(properties, "keepNext")
    if numbering is not None:
        number_properties = sub(properties, "numPr")
        sub(number_properties, "ilvl", val="0")
        sub(number_properties, "numId", val=str(numbering))
    if text:
        add_inline(node, text)
    return node


def code_block(body: ET.Element, lines: list[str]) -> None:
    node = paragraph(body, style="CodeBlock")
    for index, line in enumerate(lines):
        if index:
            run = sub(node, "r")
            sub(run, "br")
        add_run(node, line or " ", code=True, color=TEXT, size=17)


def page_break(body: ET.Element) -> None:
    node = paragraph(body)
    run = sub(node, "r")
    sub(run, "br", type="page")


def cell_text(cell: ET.Element, value: str, *, header: bool) -> None:
    properties = sub(cell, "tcPr")
    sub(properties, "tcMar")
    if header:
        sub(properties, "shd", val="clear", color="auto", fill=NAVY)
    node = paragraph(cell, style="TableText")
    if header:
        add_run(node, value, bold=True, color=WHITE)
    else:
        add_inline(node, value)


def table(body: ET.Element, rows: list[list[str]]) -> None:
    node = sub(body, "tbl")
    properties = sub(node, "tblPr")
    sub(properties, "tblStyle", val="CodexTable")
    sub(properties, "tblW", w="0", type="auto")
    sub(properties, "tblLayout", type="autofit")
    borders = sub(properties, "tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        sub(borders, edge, val="single", sz="4", space="0", color="C8D2DC")
    for row_index, values in enumerate(rows):
        row = sub(node, "tr")
        if row_index == 0:
            row_properties = sub(row, "trPr")
            sub(row_properties, "tblHeader")
        for value in values:
            cell = sub(row, "tc")
            cell_text(cell, value, header=row_index == 0)
    paragraph(body, style="TableAfter")


def is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def starts_block(lines: list[str], index: int) -> bool:
    line = lines[index]
    if not line.strip():
        return True
    if line.startswith("#") or line.startswith("```") or line.startswith("> "):
        return True
    if line.strip() in {"<!-- pagebreak -->"}:
        return True
    if re.match(r"^- ", line) or re.match(r"^\d+\. ", line):
        return True
    return (
        line.startswith("|")
        and index + 1 < len(lines)
        and is_table_separator(lines[index + 1])
    )


def document_xml(markdown: str) -> tuple[bytes, set[int]]:
    document = ET.Element(w("document"))
    body = sub(document, "body")
    lines = markdown.splitlines()
    index = 0
    first_heading = True
    after_title = False
    next_number_id = 2
    used_numbers: set[int] = {1}

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped == "<!-- pagebreak -->":
            page_break(body)
            index += 1
            continue
        if line.startswith("```"):
            index += 1
            values: list[str] = []
            while index < len(lines) and not lines[index].startswith("```"):
                values.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            code_block(body, values)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2)
            if first_heading:
                paragraph(body, text, style="Title")
                first_heading = False
                after_title = True
            else:
                style = {2: "Heading1", 3: "Heading2", 4: "Heading3"}.get(
                    level, "Heading3"
                )
                paragraph(body, text, style=style, keep_next=True)
            index += 1
            continue
        if line.startswith("> "):
            values: list[str] = []
            while index < len(lines) and lines[index].startswith("> "):
                values.append(lines[index][2:].strip())
                index += 1
            paragraph(body, " ".join(values), style="Callout")
            after_title = False
            continue
        if (
            line.startswith("|")
            and index + 1 < len(lines)
            and is_table_separator(lines[index + 1])
        ):
            rows = [table_row(line)]
            index += 2
            while index < len(lines) and lines[index].startswith("|"):
                rows.append(table_row(lines[index]))
                index += 1
            table(body, rows)
            after_title = False
            continue
        if re.match(r"^- ", line):
            while index < len(lines) and re.match(r"^- ", lines[index]):
                paragraph(body, lines[index][2:].strip(), style="ListParagraph", numbering=1)
                index += 1
            after_title = False
            continue
        if re.match(r"^\d+\. ", line):
            current_number_id = next_number_id
            next_number_id += 1
            used_numbers.add(current_number_id)
            while index < len(lines) and re.match(r"^\d+\. ", lines[index]):
                text = re.sub(r"^\d+\.\s+", "", lines[index]).strip()
                paragraph(
                    body,
                    text,
                    style="ListParagraph",
                    numbering=current_number_id,
                )
                index += 1
            after_title = False
            continue

        values = [stripped]
        index += 1
        while index < len(lines) and not starts_block(lines, index):
            values.append(lines[index].strip())
            index += 1
        style = "Subtitle" if after_title else "Normal"
        paragraph(body, " ".join(values), style=style)
        after_title = False

    section = sub(body, "sectPr")
    ET.SubElement(section, w("headerReference"), {attr("type"): "default", qn(R_NS, "id"): "rId6"})
    ET.SubElement(section, w("footerReference"), {attr("type"): "default", qn(R_NS, "id"): "rId7"})
    sub(section, "titlePg")
    sub(section, "pgSz", w="11906", h="16838")
    sub(section, "pgMar", top="1080", right="1080", bottom="1080", left="1080", header="540", footer="540", gutter="0")
    sub(section, "cols", space="720")
    sub(section, "docGrid", linePitch="360")
    return serialize(document), used_numbers


def styles_xml() -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W_NS}">
  <w:docDefaults>
    <w:rPrDefault><w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos" w:eastAsia="Arial"/><w:color w:val="{TEXT}"/><w:sz w:val="20"/><w:szCs w:val="20"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/><w:pPr><w:widowControl/><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:qFormat/><w:pPr><w:spacing w:before="1800" w:after="180"/><w:jc w:val="left"/></w:pPr><w:rPr><w:rFonts w:ascii="Aptos Display" w:hAnsi="Aptos Display"/><w:b/><w:color w:val="{NAVY}"/><w:sz w:val="60"/><w:szCs w:val="60"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:qFormat/><w:pPr><w:spacing w:after="480"/></w:pPr><w:rPr><w:color w:val="{MUTED}"/><w:sz w:val="27"/><w:szCs w:val="27"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="360" w:after="140"/><w:outlineLvl w:val="0"/><w:pBdr><w:bottom w:val="single" w:sz="10" w:space="5" w:color="{TEAL}"/></w:pBdr></w:pPr><w:rPr><w:rFonts w:ascii="Aptos Display" w:hAnsi="Aptos Display"/><w:b/><w:color w:val="{NAVY}"/><w:sz w:val="34"/><w:szCs w:val="34"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="260" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:color w:val="{TEAL}"/><w:sz w:val="27"/><w:szCs w:val="27"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="220" w:after="80"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:color w:val="{NAVY}"/><w:sz w:val="23"/><w:szCs w:val="23"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="480" w:hanging="240"/><w:spacing w:after="70"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="CodeBlock"><w:name w:val="Code Block"/><w:basedOn w:val="Normal"/><w:pPr><w:keepLines/><w:spacing w:before="80" w:after="160" w:line="240" w:lineRule="auto"/><w:ind w:left="180" w:right="120"/><w:shd w:val="clear" w:color="auto" w:fill="{CODE_BG}"/><w:pBdr><w:left w:val="single" w:sz="20" w:space="8" w:color="{TEAL}"/></w:pBdr></w:pPr><w:rPr><w:rFonts w:ascii="Cascadia Mono" w:hAnsi="Cascadia Mono"/><w:sz w:val="17"/><w:szCs w:val="17"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Callout"><w:name w:val="Callout"/><w:basedOn w:val="Normal"/><w:pPr><w:keepLines/><w:spacing w:before="100" w:after="180"/><w:ind w:left="240" w:right="180"/><w:shd w:val="clear" w:color="auto" w:fill="{LIGHT}"/><w:pBdr><w:left w:val="single" w:sz="24" w:space="10" w:color="{TEAL}"/></w:pBdr></w:pPr><w:rPr><w:color w:val="{NAVY}"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="30" w:line="240" w:lineRule="auto"/></w:pPr><w:rPr><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="TableAfter"><w:name w:val="Table After"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="80"/><w:keepNext/></w:pPr><w:rPr><w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr></w:style>
  <w:style w:type="table" w:styleId="CodexTable"><w:name w:val="Codex Table"/><w:tblPr><w:tblCellMar><w:top w:w="80" w:type="dxa"/><w:left w:w="100" w:type="dxa"/><w:bottom w:w="80" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar></w:tblPr><w:tblStylePr w:type="band1Horz"><w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="F7F9FB"/></w:tcPr></w:tblStylePr></w:style>
</w:styles>'''.encode("utf-8")


def numbering_xml(number_ids: set[int]) -> bytes:
    nums = "\n".join(
        f'<w:num w:numId="{number_id}"><w:abstractNumId w:val="1"/></w:num>'
        for number_id in sorted(number_ids)
        if number_id != 1
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="{W_NS}">
  <w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:tabs><w:tab w:val="num" w:pos="480"/></w:tabs><w:ind w:left="480" w:hanging="240"/></w:pPr><w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/></w:rPr></w:lvl></w:abstractNum>
  <w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr><w:tabs><w:tab w:val="num" w:pos="480"/></w:tabs><w:ind w:left="480" w:hanging="240"/></w:pPr></w:lvl></w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
  {nums}
</w:numbering>'''.encode("utf-8")


def header_xml() -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:hdr xmlns:w="{W_NS}"><w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="8" w:space="4" w:color="{TEAL}"/></w:pBdr><w:spacing w:after="60"/></w:pPr><w:r><w:rPr><w:b/><w:color w:val="{NAVY}"/><w:sz w:val="17"/></w:rPr><w:t>CODEXCLI  ·  USER GUIDE</w:t></w:r></w:p></w:hdr>'''.encode("utf-8")


def footer_xml() -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="{W_NS}"><w:p><w:pPr><w:pBdr><w:top w:val="single" w:sz="4" w:space="4" w:color="C8D2DC"/></w:pBdr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:color w:val="{MUTED}"/><w:sz w:val="16"/></w:rPr><w:t xml:space="preserve">Dev Loop v0.1.0  |  Page </w:t></w:r><w:fldSimple w:instr=" PAGE "><w:r><w:rPr><w:color w:val="{MUTED}"/><w:sz w:val="16"/></w:rPr><w:t>1</w:t></w:r></w:fldSimple><w:r><w:rPr><w:color w:val="{MUTED}"/><w:sz w:val="16"/></w:rPr><w:t xml:space="preserve"> of </w:t></w:r><w:fldSimple w:instr=" NUMPAGES "><w:r><w:rPr><w:color w:val="{MUTED}"/><w:sz w:val="16"/></w:rPr><w:t>1</w:t></w:r></w:fldSimple></w:p></w:ftr>'''.encode("utf-8")


CONTENT_TYPES = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/word/fontTable.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>
  <Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''

ROOT_RELS = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''

DOCUMENT_RELS = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable" Target="fontTable.xml"/>
  <Relationship Id="rId6" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
  <Relationship Id="rId7" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
</Relationships>'''

SETTINGS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="{W_NS}"><w:zoom w:percent="100"/><w:defaultTabStop w:val="720"/><w:updateFields w:val="true"/><w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>'''.encode("utf-8")

FONT_TABLE = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:fonts xmlns:w="{W_NS}"><w:font w:name="Aptos"/><w:font w:name="Aptos Display"/><w:font w:name="Cascadia Mono"/><w:font w:name="Consolas"/><w:font w:name="Arial"/></w:fonts>'''.encode("utf-8")


def core_xml() -> bytes:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="{CP_NS}" xmlns:dc="{DC_NS}" xmlns:dcterms="{DCTERMS_NS}" xmlns:xsi="{XSI_NS}">
  <dc:title>CodexCLI User Guide</dc:title><dc:subject>Installation, commands, parameters, recovery, and operation</dc:subject><dc:creator>Dev Loop</dc:creator><cp:lastModifiedBy>Dev Loop</cp:lastModifiedBy><dc:description>Professional user guide for Dev Loop CodexCLI v0.1.0.</dc:description><cp:keywords>CodexCLI, Dev Loop, user guide, installation, parameters</cp:keywords><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified><cp:revision>1</cp:revision>
</cp:coreProperties>'''.encode("utf-8")


APP_XML = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="{VT_NS}"><Application>Dev Loop Documentation Builder</Application><AppVersion>1.0</AppVersion><Company>Dev Loop</Company><DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop><LinksUpToDate>false</LinksUpToDate><SharedDoc>false</SharedDoc><HyperlinksChanged>false</HyperlinksChanged></Properties>'''.encode("utf-8")


def build() -> None:
    markdown = SOURCE.read_text(encoding="utf-8")
    document, used_numbers = document_xml(markdown)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, content in (
            ("[Content_Types].xml", CONTENT_TYPES),
            ("_rels/.rels", ROOT_RELS),
            ("docProps/core.xml", core_xml()),
            ("docProps/app.xml", APP_XML),
            ("word/document.xml", document),
            ("word/styles.xml", styles_xml()),
            ("word/numbering.xml", numbering_xml(used_numbers)),
            ("word/settings.xml", SETTINGS),
            ("word/fontTable.xml", FONT_TABLE),
            ("word/header1.xml", header_xml()),
            ("word/footer1.xml", footer_xml()),
            ("word/_rels/document.xml.rels", DOCUMENT_RELS),
        ):
            package.writestr(name, content)
    print(OUTPUT)


if __name__ == "__main__":
    try:
        build()
    except Exception as error:
        print(f"Unable to build CodexCLI user guide: {error}", file=sys.stderr)
        raise
