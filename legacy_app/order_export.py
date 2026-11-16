"""Small dependency-free XLSX export for runtime order management.

All cells are literal strings, so customer input cannot become Excel formulas.
"""
import io
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape


def item_text(groups,items):
    lines=[group['text'] for group in groups]
    from product_units import order_total_label
    return '\n'.join(lines+[order_total_label(items)])


def sales_text(sales, ordinary_groups, ordinary_items):
    """Display recorded sale identities; never infer a bundle from components."""
    lines = [str(sale['name'])+' ×'+str(sale['quantity'])+'份' for sale in sales]
    if ordinary_items:
        lines.append('普通商品：'+item_text(ordinary_groups, ordinary_items))
    return '\n'.join(lines)


def xlsx(rows):
    def cell(value,col,row):
        # Strip only characters forbidden by XML 1.0. Emoji and newlines stay.
        text=''.join(c for c in str(value if value is not None else '')
                     if c in '\t\n\r' or 0x20<=ord(c)<=0xD7FF
                     or 0xE000<=ord(c)<=0xFFFD or 0x10000<=ord(c)<=0x10FFFF)
        return '<c r="'+chr(65+col)+str(row)+'" t="inlineStr" s="1"><is><t xml:space="preserve">'+escape(text)+'</t></is></c>'
    contents=['<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cols>']
    for index,width in enumerate((18,14,20,50,55,16,16,24),1):
        contents.append(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>')
    contents.append('</cols><sheetData>')
    for index,row in enumerate(rows,1):
        height=max(24,18*max(str(value or '').count('\n')+1 for value in row))
        contents.append(f'<row r="{index}" ht="{height}" customHeight="1">'+''.join(cell(value,col,index) for col,value in enumerate(row))+'</row>')
    contents.append('</sheetData></worksheet>')
    files={
      '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
      '_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
      'xl/workbook.xml':'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="订单记录" sheetId="1" r:id="rId1"/></sheets></workbook>',
      'xl/_rels/workbook.xml.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
      'xl/styles.xml':'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font><sz val="11"/><name val="Arial"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf/><xf fontId="0" fillId="0" borderId="0" numFmtId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf></cellXfs></styleSheet>',
      'xl/worksheets/sheet1.xml':''.join(contents),
    }
    output=io.BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        for name,xml in files.items():archive.writestr(name,'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'+xml)
    return output.getvalue()
