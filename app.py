from flask import Flask, render_template, request, jsonify, send_file
from urllib.parse import urljoin, urlparse, urldefrag
from html.parser import HTMLParser
from pathlib import Path
import threading, requests, zipfile, tempfile, os, time, re

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
BASES = [
 'https://chabadlibrary.org/books/',
 'https://www.chabadlibrary.org/books/',
 'https://chabadlibrary.org/catalog/index.php?catalog=mscatalog&frame=content&search_mode=simple'
]
ALLOWED_HOSTS = {'chabadlibrary.org', 'www.chabadlibrary.org', 's3.wasabisys.com'}
HEADERS = {'User-Agent': 'ChabadLibraryPersonalDownloader/1.0 (public-page links only)'}
state = {'running': False, 'done': False, 'error': '', 'pages': 0, 'found': 0, 'message': 'מוכן לסריקה', 'items': [], 'zip_path': '', 'zip_name': ''}
lock = threading.Lock()

class Links(HTMLParser):
    def __init__(self): super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a':
            d=dict(attrs); href=d.get('href')
            if href: self.links.append((href, ''.join(d.get('title',''))))

def allowed_url(url):
    p=urlparse(url)
    return p.scheme in ('http','https') and p.hostname in ALLOWED_HOSTS

def canonical(url): return urldefrag(url)[0]

def scan_worker(max_pages):
    global state
    try:
        queue=list(BASES); seen=set(); found={}
        with lock:
            state.update(running=True, done=False, error='', pages=0, found=0, message='סורק דפים ציבוריים באתר הרשמי…', items=[], zip_path='', zip_name='')
        session=requests.Session(); session.headers.update(HEADERS)
        while queue and len(seen)<max_pages:
            url=canonical(queue.pop(0))
            if url in seen or not allowed_url(url): continue
            seen.add(url)
            try:
                r=session.get(url, timeout=12, stream=True)
                ctype=r.headers.get('content-type','').lower()
                if r.status_code != 200 or 'text/html' not in ctype:
                    r.close()
                    if url.lower().split('?')[0].endswith('.pdf'):
                        found[url]={'url':url,'title':Path(urlparse(url).path).name or 'PDF','kind':'PDF'}
                    continue
                content=r.text[:2_000_000]; r.close()
                parser=Links(); parser.feed(content)
                for href,title in parser.links:
                    nxt=canonical(urljoin(url,href.strip()))
                    if not allowed_url(nxt): continue
                    path=urlparse(nxt).path.lower()
                    if path.endswith('.pdf'):
                        found[nxt]={'url':nxt,'title':(title.strip() or Path(urlparse(nxt).path).name or 'PDF')[:180],'kind':'PDF'}
                    elif (('/books/' in path or '/catalog/' in path) and nxt not in seen and len(queue)<max_pages*5):
                        queue.append(nxt)
            except Exception:
                continue
            with lock:
                state['pages']=len(seen); state['found']=len(found); state['items']=list(found.values()); state['message']=f"נסרקו {len(seen)} דפים; נמצאו {len(found)} קישורי PDF"
            time.sleep(0.25)
        with lock:
            state.update(running=False, done=True, items=sorted(found.values(),key=lambda x:x['title'].casefold()), message=f"הסריקה הסתיימה: {len(seen)} דפים, {len(found)} קובצי PDF שנמצאו")
    except Exception as e:
        with lock: state.update(running=False, done=True, error=str(e), message='הסריקה נכשלה')

def zip_worker(urls):
    with lock: state.update(running=True, done=False, error='', zip_path='', zip_name='', message='מוריד קבצים ומכין חבילת ZIP…')
    try:
        fd, path=tempfile.mkstemp(prefix='chabad-library-',suffix='.zip'); os.close(fd)
        session=requests.Session(); session.headers.update(HEADERS); ok=0; skipped=0
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as zf:
            for i,url in enumerate(urls,1):
                if not allowed_url(url) or not urlparse(url).path.lower().endswith('.pdf'):
                    skipped+=1; continue
                try:
                    r=session.get(url,timeout=30,stream=True)
                    if r.status_code != 200 or 'pdf' not in r.headers.get('content-type','').lower(): r.close(); skipped+=1; continue
                    name=Path(urlparse(url).path).name or f'book_{i}.pdf'
                    # Avoid duplicate filenames in ZIP.
                    base=Path(name).stem; ext=Path(name).suffix; n=2
                    while name in zf.namelist(): name=f'{base}_{n}{ext}'; n+=1
                    with zf.open(name,'w') as dest:
                        total=0
                        for chunk in r.iter_content(128*1024):
                            if chunk: dest.write(chunk); total+=len(chunk)
                    r.close()
                    if total < 100: skipped+=1
                    else: ok+=1
                except Exception: skipped+=1
                with lock: state['message']=f'מכין חבילה: {i}/{len(urls)} | הצליחו {ok}, דולגו {skipped}'
                time.sleep(0.15)
        with lock:
            state.update(running=False, done=True, zip_path=path, zip_name='ספריית-חבד-PDF.zip', message=f'החבילה מוכנה: {ok} קבצים; דולגו {skipped}')
    except Exception as e:
        with lock: state.update(running=False, done=True, error=str(e), message='יצירת החבילה נכשלה')

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
    except: max_pages=250
    threading.Thread(target=scan_worker,args=(max_pages,),daemon=True).start()
    return jsonify(ok=True)

@app.post('/api/download')
def download():
    with lock:
        if state['running']: return jsonify(ok=False,error='כבר מתבצעת פעולה'),409
    data=request.get_json(silent=True) or {}; urls=data.get('urls',[])
    urls=list(dict.fromkeys(u for u in urls if isinstance(u,str) and allowed_url(u) and urlparse(u).path.lower().endswith('.pdf')))
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
