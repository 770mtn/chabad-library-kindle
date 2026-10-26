from flask import Flask, render_template, request, jsonify, send_file
from urllib.parse import urljoin, urlparse, urldefrag
from html.parser import HTMLParser
from pathlib import Path
import threading, requests, zipfile, tempfile, os, time, re, base64, json

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024

# Start with known, public pages. The www host is important for the books section.
BASES = [
    'https://www.chabadlibrary.org/books/1100000000',
    'https://www.chabadlibrary.org/books/pdf/',
    'https://www.chabadlibrary.org/books/900000000',
    'https://www.chabadlibrary.org/books/zz/index.htm',
    'https://chabadlibrary.org/catalog/index1.php',
    'https://chabadlibrary.org/catalog/index.php?catalog=mscatalog',
]
ALLOWED_HOSTS = {'chabadlibrary.org', 'www.chabadlibrary.org', 's3.wasabisys.com'}
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ChabadLibraryPersonalCatalog/1.2; public pages)',
    'Accept': 'text/html,application/xhtml+xml,application/pdf,*/*;q=0.8',
    'Accept-Language': 'he,en-US;q=0.8,en;q=0.7',
}
state = {'running': False, 'done': False, 'error': '', 'pages': 0, 'found': 0,
         'message': 'מוכן לסריקה', 'items': [], 'zip_path': '', 'zip_name': ''}
lock = threading.Lock()

class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag.lower() in ('a', 'iframe', 'frame', 'embed', 'object', 'source'):
            href = d.get('href') or d.get('src') or d.get('data')
            if href: self.links.append((href, d.get('title') or d.get('aria-label') or d.get('alt') or ''))

def allowed_url(url):
    p = urlparse(url)
    return p.scheme in ('http','https') and (p.hostname or '').lower() in ALLOWED_HOSTS

def canonical(url):
    return urldefrag(url)[0]

def is_pdf_url(url):
    p = urlparse(url)
    path = p.path.lower()
    return path.endswith('.pdf') or path.endswith('.pdf.php')

def scan_worker(max_pages):
    global state
    errors=[]
    try:
        queue=list(BASES); seen=set(); found={}; session=requests.Session(); session.headers.update(HEADERS)
        with lock:
            state.update(running=True, done=False, error='', pages=0, found=0,
                         message='סורק דפים ציבוריים באתר הרשמי…', items=[], zip_path='', zip_name='')
        while queue and len(seen) < max_pages:
            url=canonical(queue.pop(0))
            if url in seen or not allowed_url(url): continue
            seen.add(url)
            try:
                r=session.get(url, timeout=18, allow_redirects=True)
                final_url=canonical(r.url)
                ctype=r.headers.get('content-type','').lower()
                if r.status_code != 200:
                    errors.append(f'{urlparse(url).netloc}: HTTP {r.status_code} בדף {urlparse(url).path[:70]}')
                    r.close(); continue
                if 'pdf' in ctype or is_pdf_url(final_url):
                    found[final_url]={'url':final_url,'title':Path(urlparse(final_url).path).name or 'PDF','kind':'PDF'}
                    r.close(); continue
                if 'html' not in ctype and 'text/plain' not in ctype:
                    errors.append(f'תוכן לא מזוהה ({ctype or "ללא content-type"}) בדף {urlparse(url).path[:70]}')
                    r.close(); continue
                content=r.text[:3_000_000]; r.close()
                parser=Links(); parser.feed(content)
                # Some pages store URLs in script/config text rather than normal anchors.
                hrefs=list(parser.links)
                for match in re.findall(r'''(?:href|src|data)\s*=\s*["']([^"']+)["']''', content, re.I):
                    hrefs.append((match, ''))
                for href,title in hrefs:
                    href=href.strip().replace('&amp;','&')
                    nxt=canonical(urljoin(final_url,href))
                    if not allowed_url(nxt): continue
                    path=urlparse(nxt).path.lower()
                    if is_pdf_url(nxt):
                        found[nxt]={'url':nxt,'title':(title.strip() or Path(urlparse(nxt).path).name or 'PDF')[:180],'kind':'PDF'}
                    elif (('/books/' in path or '/catalog/' in path) and nxt not in seen and len(queue)<max_pages*8):
                        if nxt not in queue: queue.append(nxt)
                # Decode public catalog PDF configuration links where the site uses a config parameter.
                for cfg in re.findall(r'''[?&]config=([A-Za-z0-9+/=_-]+)''', content):
                    try:
                        raw=base64.b64decode(cfg + '=' * (-len(cfg) % 4)).decode('utf-8','ignore')
                        obj=json.loads(raw)
                        template=obj.get('href','')
                        # Only add literal PDF links; never generate or enumerate protected page sequences.
                        if isinstance(template,str) and '.pdf' in template.lower():
                            for m in re.findall(r'https?://[^\s"\\]+?\.pdf(?:\?[^\s"\\]*)?', template, re.I):
                                if allowed_url(m): found[m]={'url':m,'title':Path(urlparse(m).path).name or 'PDF','kind':'PDF'}
                    except Exception:
                        pass
            except Exception as e:
                errors.append(f'לא ניתן לקרוא {urlparse(url).netloc}{urlparse(url).path[:55]} ({type(e).__name__})')
            with lock:
                state['pages']=len(seen); state['found']=len(found)
                state['items']=sorted(found.values(),key=lambda x:x['title'].casefold())
                state['message']=f'נסרקו {len(seen)} דפים; נמצאו {len(found)} קובצי PDF'
            time.sleep(0.25)
        summary=f'הסריקה הסתיימה: {len(seen)} דפים, {len(found)} קובצי PDF שנמצאו'
        if not found and errors:
            summary += '. לא התקבל תוכן מחלק מהדפים: ' + ' | '.join(errors[:4])
        with lock:
            state.update(running=False, done=True, pages=len(seen), found=len(found), items=sorted(found.values(),key=lambda x:x['title'].casefold()), message=summary, error='')
    except Exception as e:
        with lock: state.update(running=False, done=True, error=f'{type(e).__name__}: {e}', message='הסריקה נכשלה')

def zip_worker(urls):
    with lock: state.update(running=True, done=False, error='', zip_path='', zip_name='', message='מוריד קבצים ומכין חבילת ZIP…')
    try:
        fd,path=tempfile.mkstemp(prefix='chabad-library-',suffix='.zip'); os.close(fd)
        session=requests.Session(); session.headers.update(HEADERS); ok=0; skipped=0
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as zf:
            for i,url in enumerate(urls,1):
                if not allowed_url(url) or not is_pdf_url(url): skipped+=1; continue
                try:
                    r=session.get(url,timeout=45,stream=True)
                    if r.status_code != 200 or ('pdf' not in r.headers.get('content-type','').lower() and not is_pdf_url(r.url)):
                        r.close(); skipped+=1; continue
                    name=Path(urlparse(r.url).path).name or f'book_{i}.pdf'
                    if not name.lower().endswith('.pdf'): name += '.pdf'
                    base=Path(name).stem; ext=Path(name).suffix; n=2
                    while name in zf.namelist(): name=f'{base}_{n}{ext}'; n+=1
                    total=0
                    with zf.open(name,'w') as dest:
                        for chunk in r.iter_content(128*1024):
                            if chunk: dest.write(chunk); total+=len(chunk)
                    r.close()
                    if total < 100: skipped+=1
                    else: ok+=1
                except Exception: skipped+=1
                with lock: state['message']=f'מכין חבילה: {i}/{len(urls)} | הצליחו {ok}, דולגו {skipped}'
                time.sleep(0.15)
        with lock: state.update(running=False, done=True, zip_path=path, zip_name='ספריית-חבד-PDF.zip', message=f'החבילה מוכנה: {ok} קבצים; דולגו {skipped}')
    except Exception as e:
        with lock: state.update(running=False, done=True, error=f'{type(e).__name__}: {e}', message='יצירת החבילה נכשלה')

@app.get('/')
def home(): return render_template('index.html')

@app.get('/api/status')
def status():
    with lock: return jsonify({**{k:v for k,v in state.items() if k not in ('zip_path',)}, 'zip_ready': bool(state.get('zip_path') and os.path.exists(state.get('zip_path','')))})

@app.post('/api/scan')
def scan():
    with lock:
        if state['running']: return jsonify(ok=False,error='כבר מתבצעת פעולה'),409
        max_pages=request.json.get('max_pages',250) if request.is_json else 250
        try: max_pages=max(20,min(1500,int(max_pages)))
        except Exception: max_pages=250
        threading.Thread(target=scan_worker,args=(max_pages,),daemon=True).start()
    return jsonify(ok=True)

@app.post('/api/download')
def download():
    with lock:
        if state['running']: return jsonify(ok=False,error='כבר מתבצעת פעולה'),409
        data=request.get_json(silent=True) or {}; urls=data.get('urls',[])
        urls=list(dict.fromkeys(u for u in urls if isinstance(u,str) and allowed_url(u) and is_pdf_url(u)))
        if not urls: return jsonify(ok=False,error='לא נבחרו קובצי PDF תקינים'),400
        if len(urls)>500: return jsonify(ok=False,error='אפשר להוריד עד 500 קבצים בכל חבילה'),400
        threading.Thread(target=zip_worker,args=(urls,),daemon=True).start()
    return jsonify(ok=True)

@app.get('/api/file')
def file():
    with lock: path=state.get('zip_path','')
    if not path or not os.path.exists(path): return 'החבילה עדיין לא מוכנה',404
    return send_file(path,as_attachment=True,download_name=state.get('zip_name') or 'chabad-library.zip')

if __name__ == '__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')),debug=False)
