from flask import Flask, render_template, request, jsonify, send_file
from urllib.parse import urljoin, urlparse, urldefrag
from pathlib import Path
from html.parser import HTMLParser
from html import escape
from uuid import uuid4
import threading, requests, zipfile, tempfile, os, time, re, json, mimetypes
from datetime import datetime, timezone

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
# Start with known, public HTML text pages rather than relying on the books index
# to expose all links. These are examples of real text pages indexed by search engines.
BASES = [
    'https://www.chabadlibrary.org/books/1400250017',
    'https://www.chabadlibrary.org/books/1400170004',
    'https://www.chabadlibrary.org/books/1402780104',
    'https://www.chabadlibrary.org/books/1400310274',
    'https://www.chabadlibrary.org/books/1400260001',
    'https://www.chabadlibrary.org/books/1400340001',
    'https://www.chabadlibrary.org/books/1400340015',
    'https://www.chabadlibrary.org/books/',
    'https://www.chabadlibrary.org/catalog/index.php?catalog=mscatalog&frame=content&search_mode=simple',
]
ALLOWED_HOSTS = {'chabadlibrary.org', 'www.chabadlibrary.org'}
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; ChabadLibraryTextLibrary/1.4; public-page text indexing)', 'Accept-Language': 'he,en;q=0.8'}
state = {
    'running': False, 'done': False, 'error': '', 'pages': 0, 'found': 0,
    'message': 'מוכן לסריקת טקסטים — כולל דפי טקסט ידועים', 'items': [], 'zip_path': '', 'zip_name': '',
    'kind': 'text', 'skipped': 0
}
lock = threading.Lock()

def allowed_url(url):
    p = urlparse(url)
    return p.scheme in ('http', 'https') and p.hostname in ALLOWED_HOSTS

def canonical(url):
    url = urldefrag(url)[0]
    p = urlparse(url)
    # Drop tracking parameters but preserve catalog parameters needed for the source page.
    return url

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.title_parts = []
        self.text_parts = []
        self.skip_depth = 0
        self.title_depth = 0
        self.in_body = False
        self.skip_tags = {'script', 'style', 'noscript', 'svg', 'iframe', 'form', 'button', 'select', 'option'}
    def handle_starttag(self, tag, attrs):
        tag = tag.lower(); d = dict(attrs)
        if tag == 'body': self.in_body = True
        if tag == 'a' and d.get('href'):
            self.links.append((d['href'].strip(), (d.get('title') or '').strip()))
        if tag == 'title': self.title_depth += 1
        if tag in self.skip_tags: self.skip_depth += 1
        if tag in ('br', 'p', 'div', 'li', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'section', 'article') and self.skip_depth == 0:
            self.text_parts.append('\n')
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == 'title' and self.title_depth: self.title_depth -= 1
        if tag in self.skip_tags and self.skip_depth: self.skip_depth -= 1
        if tag == 'body': self.in_body = False
        if tag in ('p', 'div', 'li', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'section', 'article') and self.skip_depth == 0:
            self.text_parts.append('\n')
    def handle_data(self, data):
        if self.title_depth: self.title_parts.append(data)
        if self.skip_depth == 0 and data.strip(): self.text_parts.append(data.strip())

def clean_text(raw):
    raw = raw.replace('\xa0', ' ').replace('\u200f', '').replace('\u200e', '')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in raw.splitlines()]
    lines = [line for line in lines if line]
    # Collapse repeated adjacent lines and excessive menus/blank lines.
    out=[]
    for line in lines:
        if not out or line != out[-1]: out.append(line)
    return '\n'.join(out)

def parse_page(html):
    parser = PageParser()
    try: parser.feed(html)
    except Exception: pass
    title = clean_text(' '.join(parser.title_parts)) or ''
    text = clean_text('\n'.join(parser.text_parts))
    # Generic HTML pages can contain navigation; keep only substantial text pages.
    return title[:200], text, parser.links

def is_crawlable(url):
    if not allowed_url(url): return False
    p = urlparse(url)
    path = p.path.lower()
    if path.endswith(('.pdf', '.jpg', '.jpeg', '.png', '.gif', '.zip', '.epub', '.mp3', '.mp4', '.css', '.js')): return False
    return '/books/' in path or '/catalog/' in path or path.rstrip('/').endswith('books')

def scan_worker(max_pages):
    queue = list(BASES); seen = set(); found = {}; skipped = 0
    with lock:
        state.update(running=True, done=False, error='', pages=0, found=0, message='מתחיל סריקת דפי טקסט ציבוריים…', items=[], zip_path='', zip_name='', kind='text', skipped=0)
    try:
        session = requests.Session(); session.headers.update(HEADERS)
        while queue and len(seen) < max_pages:
            url = canonical(queue.pop(0))
            if url in seen or not is_crawlable(url): continue
            seen.add(url)
            try:
                resp = session.get(url, timeout=15, allow_redirects=True)
                ctype = resp.headers.get('content-type', '').lower()
                if resp.status_code != 200 or ('html' not in ctype and not resp.text[:100].lower().find('<html') >= 0):
                    skipped += 1; resp.close(); continue
                html = resp.text[:2_500_000]; resp.close()
                title, text, links = parse_page(html)
                path = urlparse(url).path.lower()
                # Avoid indexing search/index pages unless they contain substantial book text.
                if len(text) >= 500 and ('/books/' in path) and not any(x in path for x in ('/search', '/index.php')):
                    key = url
                    found[key] = {'url': url, 'title': (title or text.splitlines()[0] or 'טקסט ללא כותרת')[:180], 'kind': 'TEXT', 'chars': len(text), 'preview': text[:220].replace('\n', ' ')}
                for href, link_title in links:
                    nxt = canonical(urljoin(url, href))
                    if is_crawlable(nxt) and nxt not in seen and nxt not in queue and len(queue) < max_pages * 4:
                        queue.append(nxt)
            except Exception:
                skipped += 1
            with lock:
                state.update(pages=len(seen), found=len(found), items=list(found.values()), skipped=skipped,
                             message=f'נסרקו {len(seen)} דפים · נמצאו {len(found)} דפי טקסט · דולגו {skipped}')
            time.sleep(0.35)
        with lock:
            state.update(running=False, done=True, items=sorted(found.values(), key=lambda x: x['title'].casefold()),
                         message=f'הסריקה הסתיימה: {len(seen)} דפים, {len(found)} דפי טקסט מתאימים; דולגו {skipped}. אם נמצאו 0, פתחו את פרטי השגיאות/הלוגים ב-Render ובדקו שהשרת יכול לגשת לאתר המקור.')
    except Exception as e:
        with lock: state.update(running=False, done=True, error=str(e)[:300], message='הסריקה נכשלה')

def fetch_text(url, session):
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    if 'html' not in resp.headers.get('content-type', '').lower():
        raise ValueError('העמוד אינו HTML')
    title, text, _ = parse_page(resp.text[:3_000_000])
    if len(text) < 100: raise ValueError('לא נמצא טקסט קריא בעמוד')
    return title or 'ללא כותרת', text

def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip(' ._')
    return (name or 'text')[:100]

def build_epub(path, books, book_title='ספריית חב״ד — טקסטים'):
    uid = 'urn:uuid:' + str(uuid4())
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', compress_type=zipfile.ZIP_DEFLATED)
        css = 'body{direction:rtl;text-align:right;font-family:serif;line-height:1.8;margin:5%;} h1{text-align:center;} p{white-space:pre-wrap;}'
        z.writestr('OEBPS/style.css', css)
        manifest=[]; spine=[]; nav=[]
        for i, (title, text, source) in enumerate(books, 1):
            fn = f'chapter{i:04d}.xhtml'; ident=f'ch{i}'
            body = '\n'.join('<p>'+escape(line)+'</p>' for line in text.splitlines() if line.strip())
            xhtml = f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xml:lang="he" lang="he" dir="rtl"><head><title>{escape(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head><body dir="rtl"><h1>{escape(title)}</h1>{body}<hr/><p>מקור: <a href="{escape(source, quote=True)}">העמוד המקורי</a></p></body></html>'
            z.writestr('OEBPS/'+fn, xhtml)
            manifest.append(f'<item id="{ident}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="{ident}"/>')
            nav.append(f'<li><a href="{fn}">{escape(title)}</a></li>')
        z.writestr('OEBPS/nav.xhtml', '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="he" lang="he" dir="rtl"><head><title>תוכן עניינים</title></head><body dir="rtl"><nav epub:type="toc" id="toc"><h1>תוכן עניינים</h1><ol>'+''.join(nav)+'</ol></nav></body></html>')
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        manifest.append('<item id="css" href="style.css" media-type="text/css"/>')
        opf = f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="he"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">{uid}</dc:identifier><dc:title>{escape(book_title)}</dc:title><dc:language>he</dc:language><meta property="dcterms:modified">{now}</meta></metadata><manifest>{"".join(manifest)}</manifest><spine page-progression-direction="rtl">{"".join(spine)}</spine></package>'
        z.writestr('OEBPS/content.opf', opf)

def export_worker(urls, fmt):
    with lock: state.update(running=True, done=False, error='', zip_path='', zip_name='', kind='export', message='מוריד דפי טקסט ציבוריים ומכין קובץ לקינדל…')
    books=[]; skipped=0
    try:
        session=requests.Session(); session.headers.update(HEADERS)
        for i, url in enumerate(urls, 1):
            try:
                if not allowed_url(url) or not is_crawlable(url): raise ValueError('כתובת לא מאושרת')
                title, txt = fetch_text(url, session)
                books.append((title, txt, url))
            except Exception:
                skipped += 1
            with lock: state['message'] = f'אוסף טקסטים: {i}/{len(urls)} · הצליחו {len(books)} · דולגו {skipped}'
            time.sleep(0.2)
        if not books: raise RuntimeError('לא הצלחנו לחלץ טקסט קריא מהעמודים שנבחרו. ייתכן שהאתר מציג את התוכן בצורה דינמית או חוסם גישה אוטומטית.')
        fd, path = tempfile.mkstemp(prefix='chabad-texts-', suffix='.zip'); os.close(fd)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
            if fmt in ('epub', 'both'):
                tmp = path + '.epub'
                try:
                    build_epub(tmp, books)
                    z.write(tmp, 'ספריית-חבד-טקסטים.epub')
                finally:
                    if os.path.exists(tmp): os.remove(tmp)
            if fmt in ('txt', 'both'):
                for i, (title, txt, source) in enumerate(books, 1):
                    z.writestr(f'TXT/{i:04d}-{safe_filename(title)}.txt', f'{title}\nמקור: {source}\n\n{txt}\n'.encode('utf-8'))
                z.writestr('README.txt', f'נאספו {len(books)} עמודי טקסט.\nהקבצים נוצרו מעמודים ציבוריים; יש לבדוק את תנאי השימוש וזכויות היוצרים של כל יצירה.\n')
        with lock: state.update(running=False, done=True, zip_path=path, zip_name='ספריית-חבד-טקסטים-לקינדל.zip', message=f'החבילה מוכנה: {len(books)} עמודי טקסט; דולגו {skipped}')
    except Exception as e:
        with lock: state.update(running=False, done=True, error=str(e)[:400], message='לא הצלחנו להכין חבילת טקסטים')

@app.get('/')
def home(): return render_template('index.html')

@app.get('/api/status')
def status():
    with lock:
        return jsonify({**{k:v for k,v in state.items() if k != 'zip_path'}, 'zip_ready': bool(state.get('zip_path') and os.path.exists(state.get('zip_path','')))})

@app.post('/api/scan')
def scan():
    with lock:
        if state['running']: return jsonify(ok=False, error='כבר מתבצעת פעולה'), 409
    data = request.get_json(silent=True) or {}
    try: max_pages = max(20, min(500, int(data.get('max_pages', 150))))
    except Exception: max_pages = 150
    threading.Thread(target=scan_worker, args=(max_pages,), daemon=True).start()
    return jsonify(ok=True)

@app.post('/api/export')
def export_texts():
    with lock:
        if state['running']: return jsonify(ok=False, error='כבר מתבצעת פעולה'), 409
    data = request.get_json(silent=True) or {}; urls = data.get('urls', []); fmt = data.get('format', 'epub')
    if fmt not in ('epub', 'txt', 'both'): return jsonify(ok=False, error='פורמט לא תקין'), 400
    urls = list(dict.fromkeys(u for u in urls if isinstance(u, str) and allowed_url(u) and is_crawlable(u)))
    if not urls: return jsonify(ok=False, error='בחר לפחות דף טקסט אחד'), 400
    if len(urls) > 100: return jsonify(ok=False, error='אפשר להכין עד 100 דפי טקסט בכל חבילה'), 400
    threading.Thread(target=export_worker, args=(urls, fmt), daemon=True).start()
    return jsonify(ok=True)

@app.get('/api/file')
def file():
    with lock: path = state.get('zip_path', ''); name = state.get('zip_name') or 'chabad-library-texts.zip'
    if not path or not os.path.exists(path): return 'החבילה עדיין לא מוכנה או נמחקה מהשרת. הפעל את הייצוא מחדש.', 404
    return send_file(path, as_attachment=True, download_name=name)

@app.get('/health')
def health(): return jsonify(status='ok', version='1.3-text')

if __name__ == '__main__': app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)
