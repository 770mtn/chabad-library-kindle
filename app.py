from flask import Flask, render_template, request, send_file
from urllib.request import Request, urlopen
from urllib.parse import quote
from html.parser import HTMLParser
from html import escape
from io import BytesIO
from datetime import datetime, timezone
import json, re, zipfile, uuid, mimetypes

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024

CATALOG = [
    {"group":"הספרייה הרשמית", "title":"כל ספרי ספריית חב״ד ליובאוויטש", "description":"הקטלוג המקורי לפי סדרי הספרייה והקטגוריות", "url":"https://www.chabadlibrary.org/books/1100000000", "kind":"official"},
    {"group":"הספרייה הרשמית", "title":"קטלוג כתבי יד וספרים", "description":"קטלוג נוסף של הספרייה", "url":"https://chabadlibrary.org/catalog/index.php?catalog=mscatalog&frame=content&search_mode=simple", "kind":"official"},
    {"group":"ספרי הרבי", "title":"תורת מנחם", "description":"התוועדויות ושיחות הרבי; דוגמה לכרך באתר הרשמי", "url":"https://www.chabadlibrary.org/books/1400260001", "kind":"series"},
    {"group":"ספרי הרבי", "title":"לקוטי שיחות", "description":"שיחות לפי כרכים ופרשות; קטלוג טקסטים בפנימי", "url":"https://pnimi.org/likutei-sichot-10", "kind":"series"},
    {"group":"ספרי הרבי", "title":"אגרות קודש, מאמרים ורשימות", "description":"היכנסו לקטלוג הרשמי כדי לבחור סדרה וכרך", "url":"https://www.chabadlibrary.org/books/1100000000", "kind":"series"},
    {"group":"ספריית טקסטים", "title":"ספריית פנימי — תוכן הספרייה", "description":"אינדקס טקסטים וחיפוש; הטקסטים מספריית חב״ד כפופים לזכויות בעליהם", "url":"https://pnimi.org/toc", "kind":"text"},
    {"group":"ספריית טקסטים", "title":"ספריית פנימי — חיפוש ומקורות", "description":"חיפוש במקורות והפניות בין טקסטים", "url":"https://pnimi.org/sources", "kind":"text"},
    {"group":"ספריית טקסטים", "title":"ספריא — ספרייה דיגיטלית", "description":"טקסטים רבים עם רישיונות פתוחים; יש לבדוק רישיון של כל מהדורה", "url":"https://www.sefaria.org.il/texts", "kind":"sefaria"},
]

ALLOWED_LICENSES = {"public domain", "public-domain", "public_domain", "pd", "cc0", "cc-by", "cc-by-sa", "cc by", "cc by-sa", "creativecommons attribution", "creativecommons attribution-sharealike"}

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]; self.skip=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style','noscript','svg'): self.skip += 1
        if self.skip == 0 and tag in ('p','div','br','li','h1','h2','h3','section'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script','style','noscript','svg') and self.skip: self.skip -= 1
        if self.skip == 0 and tag in ('p','div','li','h1','h2','h3','section'): self.parts.append('\n')
    def handle_data(self, data):
        if not self.skip and data.strip(): self.parts.append(data.strip())

def html_to_text(value):
    if isinstance(value, list):
        return '\n'.join(html_to_text(v) for v in value if v is not None)
    if not isinstance(value, str): return ''
    p=TextExtractor()
    try: p.feed(value)
    except Exception: pass
    text=''.join(p.parts).replace('\xa0',' ').replace('\u200f','').replace('\u200e','')
    lines=[re.sub(r'\s+',' ',x).strip() for x in text.splitlines()]
    return '\n'.join(x for x in lines if x)

def license_allowed(license_value):
    lic=(license_value or '').strip().lower().replace('_','-')
    if not lic: return False
    # Explicitly exclude non-commercial and unknown/copyrighted editions.
    if 'nc' in lic or 'copyright' in lic or 'all rights reserved' in lic or 'unknown' in lic: return False
    return lic in ALLOWED_LICENSES or lic.startswith('cc-by-') or lic.startswith('cc by-')

def make_epub(title, text, source, license_value):
    safe_title=escape(title or 'טקסט מספריא')
    ident='urn:uuid:'+str(uuid.uuid4())
    stamp=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    xhtml='''<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="he" lang="he" dir="rtl"><head><title>'''+safe_title+'''</title><meta charset="utf-8"/><style>body{direction:rtl;text-align:right;font-family:serif;line-height:1.8}h1{text-align:center}p{white-space:pre-wrap}</style></head><body><h1>'''+safe_title+'''</h1><p>מקור: '''+escape(source)+'''\nרישיון: '''+escape(license_value)+'''</p><hr/><p>'''+escape(text).replace('\n','</p><p>')+'''</p></body></html>'''
    container='<?xml version="1.0" encoding="UTF-8"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    opf='''<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="he" dir="rtl"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">'''+ident+'''</dc:identifier><dc:title>'''+safe_title+'''</dc:title><dc:language>he</dc:language><dc:creator>ספריית חב״ד לקינדל — כלי ייצוא</dc:creator><dc:description>מקור: '''+escape(source)+'''; רישיון: '''+escape(license_value)+'''</dc:description><meta property="dcterms:modified">'''+stamp+'''</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="content" href="content.xhtml" media-type="application/xhtml+xml"/></manifest><spine page-progression-direction="rtl"><itemref idref="content"/></spine></package>'''
    nav='<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="he" xml:lang="he" dir="rtl"><head><title>תוכן עניינים</title></head><body><nav epub:type="toc"><h1>תוכן עניינים</h1><ol><li><a href="content.xhtml">'+safe_title+'</a></li></ol></nav></body></html>'
    b=BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('mimetype','application/epub+zip',compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml',container,compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('OEBPS/content.opf',opf,compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('OEBPS/content.xhtml',xhtml,compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('OEBPS/nav.xhtml',nav,compress_type=zipfile.ZIP_DEFLATED)
    b.seek(0); return b

@app.get('/')
def index():
    return render_template('index.html', catalog=CATALOG)

@app.post('/export')
def export_text():
    ref=(request.form.get('ref') or '').strip()
    fmt=(request.form.get('format') or 'epub').strip().lower()
    if not ref or len(ref)>180 or not re.fullmatch(r'[\w .,:;()\-״׳]+', ref, flags=re.UNICODE):
        return render_template('index.html', catalog=CATALOG, error='יש להזין מראה מקום תקין מספריא, לדוגמה Berakhot 2a או Genesis 1.'), 400
    if fmt not in ('epub','txt'): fmt='epub'
    api_ref=quote(ref.replace(' ', '_'), safe='._-,:;()')
    url='https://www.sefaria.org/api/v3/texts/'+api_ref+'?version=hebrew'
    try:
        req=Request(url,headers={'User-Agent':'ChabadLibraryKindle/1.5 (personal text export)','Accept':'application/json'})
        with urlopen(req,timeout=20) as response:
            if response.status != 200: raise ValueError('ספריא החזירה תשובת שגיאה')
            data=json.loads(response.read(4_000_000).decode('utf-8'))
    except Exception as exc:
        return render_template('index.html', catalog=CATALOG, error='לא הצלחתי לקבל טקסט מספריא כרגע. בדוק את המראה מקום או נסה שוב מאוחר יותר. פרטים: '+str(exc)[:220]), 502
    versions=data.get('versions') or []
    if isinstance(versions,dict): versions=[versions]
    chosen=None
    for v in versions:
        if (v.get('language') or '').lower().startswith('he') and license_allowed(v.get('license')):
            chosen=v; break
    if not chosen:
        return render_template('index.html', catalog=CATALOG, error='לא נמצאה מהדורה עברית עם רישיון ייצוא מאושר (נחלת הכלל, CC0, CC-BY או CC-BY-SA). לא ייצאנו טקסט שרישיונו לא ברור או מוגבל.'), 403
    text=html_to_text(chosen.get('text'))
    if len(text.strip())<30:
        return render_template('index.html', catalog=CATALOG, error='ספריא לא החזירה מספיק טקסט למראה המקום הזה. נסה פרק או עמוד ספציפי.'), 422
    title=chosen.get('versionTitleInHebrew') or chosen.get('versionTitle') or data.get('ref') or ref
    source_url='https://www.sefaria.org/'+quote(ref.replace(' ', '_'),safe='._-,:;()')
    lic=chosen.get('license') or 'Public Domain'
    if fmt=='txt':
        content=(title+'\nמקור: '+source_url+'\nרישיון: '+lic+'\n\n'+text+'\n').encode('utf-8-sig')
        return send_file(BytesIO(content),as_attachment=True,download_name='sefaria_text.txt',mimetype='text/plain; charset=utf-8')
    return send_file(make_epub(title,text,source_url,lic),as_attachment=True,download_name='sefaria_text.epub',mimetype='application/epub+zip')

@app.get('/health')
def health(): return {'status':'ok','version':'1.5'}

if __name__=='__main__':
    app.run(host='0.0.0.0',port=5000,debug=False)
