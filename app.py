import csv, hashlib, io, json, os, re, sqlite3, threading, unicodedata
from email.parser import BytesParser
from email import policy
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT=Path(__file__).resolve().parent
DB=ROOT/'portal_comex.sqlite3'
PORT=int(os.getenv('PORT','8765'))
MAX_UPLOAD=350*1024*1024
LOCK=threading.Lock()

def db():
 c=sqlite3.connect(DB,timeout=60); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=60000'); c.execute('PRAGMA journal_mode=WAL'); return c

def setup():
 with db() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, filename TEXT, sha256 TEXT UNIQUE, rows INTEGER DEFAULT 0, imported_at TEXT DEFAULT CURRENT_TIMESTAMP);
 CREATE TABLE IF NOT EXISTS companies(rut TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
 CREATE TABLE IF NOT EXISTS operations(id INTEGER PRIMARY KEY, file_id INTEGER, rut TEXT, source_name TEXT, country TEXT, product TEXT, hs TEXT, date TEXT, value REAL, currency TEXT, declaration TEXT, raw_json TEXT,
 FOREIGN KEY(file_id) REFERENCES files(id));
 CREATE INDEX IF NOT EXISTS idx_ops_rut ON operations(rut); CREATE INDEX IF NOT EXISTS idx_ops_country ON operations(country); CREATE INDEX IF NOT EXISTS idx_ops_hs ON operations(hs); CREATE INDEX IF NOT EXISTS idx_ops_date ON operations(date);''')

def norm(s):
 return ''.join(x for x in unicodedata.normalize('NFKD',str(s or '').lower()) if not unicodedata.combining(x)).replace('_',' ').replace('-',' ').strip()
def rut(s):
 s=re.sub('[^0-9kK]','',str(s or '')).upper()
 if not (8<=len(s)<=10): return ''
 body,dv=s[:-1],s[-1]; total=sum(int(a)*b for a,b in zip(body[::-1],(2,3,4,5,6,7)*2)); rem=11-total%11; check='0' if rem==11 else 'K' if rem==10 else str(rem)
 return (body+'-'+dv) if check==dv else ''

def detect(data):
 # Supports utf8/latin1, excel UTF16 and delimiter auto detection.
 text=None
 for enc in ('utf-8-sig','utf-16','cp1252'):
  try: text=data.decode(enc); break
  except UnicodeError: pass
 if text is None: raise ValueError('No se pudo decodificar el CSV')
 try: delim=csv.Sniffer().sniff(text[:12000], delimiters=',;\t|').delimiter
 except csv.Error: delim=max((';',',','\t','|'),key=lambda d:text[:12000].count(d))
 return text,delim
ALIASES={
 'rut':['rut importador','rut consignatario','rut cliente','rut','identificador importador','id importador','rut impo'],
 'name':['razon social importador','nombre importador','razon social','nombre cliente','importador','consignatario','empresa'],
 'country':['pais origen','pais de origen','pais procedencia','pais adquisicion','pais','origen'],
 'product':['descripcion mercancia','descripcion producto','descripcion','mercancia','producto','glosa'],
 'hs':['partida arancelaria','codigo arancelario','codigo hs','arancel','partida','hs','ncm'],
 'date':['fecha aceptacion','fecha declaracion','fecha ingreso','fecha','fec aceptacion'],
 'value':['valor cif us','valor cif','cif us','cif','valor fob','fob us','fob','valor usd','valor'],
 'currency':['moneda','divisa'],
 'declaration':['numero din','nro din','num din','din','numero declaracion','numero documento','declaracion']}
def guess(headers):
 names={norm(h):h for h in headers if h}
 result={}
 for dest,possibles in ALIASES.items():
  for option in possibles:
   if option in names:result[dest]=names[option];break
  if dest not in result:
   for option in possibles:
    if len(option)>=6:
     matches=[v for k,v in names.items() if option in k]
     if len(matches)==1:result[dest]=matches[0];break
 return result

def amount(s):
 s=str(s or '').strip().replace(' ','').replace('USD','').replace('$','')
 if not s:return None
 if ',' in s and '.' in s:s=s.replace('.','').replace(',','.') if s.rfind(',')>s.rfind('.') else s.replace(',','')
 elif ',' in s:s=s.replace(',','.')
 try:return float(s)
 except ValueError:return None

def import_csv(data,filename,kind='operations',mapping=None):
 sha=hashlib.sha256(data).hexdigest(); text,delim=detect(data)
 reader=csv.DictReader(io.StringIO(text,newline=''),delimiter=delim)
 if not reader.fieldnames:raise ValueError('No se encontraron encabezados')
 fields=[h.strip().lstrip('\ufeff') if h else '' for h in reader.fieldnames]
 reader.fieldnames=fields
 m={**guess(fields),**(mapping or {})}
 if kind=='operations' and not (m.get('rut') or m.get('name')):raise ValueError('No se detectó RUT o nombre del importador. Ajusta las columnas y reintenta.')
 if kind=='companies' and not (m.get('rut') and m.get('name')):raise ValueError('El padrón de empresas necesita RUT y razón social.')
 with LOCK:
  with db() as c:
   if kind=='operations':
    found=c.execute('SELECT id,rows FROM files WHERE sha256=?',(sha,)).fetchone()
    if found:return {'ok':True,'duplicate':True,'rows':found['rows'],'mapping':m}
    cur=c.execute('INSERT INTO files(filename,sha256) VALUES (?,?)',(filename,sha)); fid=cur.lastrowid
   total=0; valid=0; batch=[]; named=0
   for row in reader:
    if None in row:continue
    total+=1
    r=rut(row.get(m.get('rut',''),'')) if m.get('rut') else ''
    name=str(row.get(m.get('name',''),'') or '').strip()
    if r and name and norm(name) not in ('sin informacion','s/i','no informado','reservado','confidencial','no disponible'):
     c.execute('INSERT INTO companies(rut,name,source) VALUES(?,?,?) ON CONFLICT(rut) DO UPDATE SET name=excluded.name,source=excluded.source,updated_at=CURRENT_TIMESTAMP WHERE companies.source != \'padron\'',(r,name,'padron' if kind=='companies' else 'archivo aduanas'));named+=1
    if kind=='companies':
     if r and name:valid+=1
     continue
    vals=[row.get(m.get(k,''),'') if m.get(k) else '' for k in ('country','product','hs','date','value','currency','declaration')]
    batch.append((fid,r,name,*vals[:4],amount(vals[4]),vals[5],vals[6],json.dumps(row,ensure_ascii=False)))
    if len(batch)>=1000:
     c.executemany('INSERT INTO operations(file_id,rut,source_name,country,product,hs,date,value,currency,declaration,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)',batch);valid+=len(batch);batch=[]
   if batch:c.executemany('INSERT INTO operations(file_id,rut,source_name,country,product,hs,date,value,currency,declaration,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)',batch);valid+=len(batch)
   if kind=='operations':c.execute('UPDATE files SET rows=? WHERE id=?',(valid,fid))
 return {'ok':True,'rows':valid,'read':total,'names_saved':named,'mapping':m}

HTML=r'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Portal COMEX | Clientes</title><style>
:root{font-family:Inter,system-ui,Arial;background:#0b1220;color:#eaf1ff}*{box-sizing:border-box}body{margin:0}header{padding:25px 5%;background:#101c30;border-bottom:1px solid #26354b}h1{margin:0;font-size:25px}small,.muted{color:#93a6c0}main{max-width:1250px;margin:26px auto;padding:0 20px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:15px}.card,.panel{background:#142239;border:1px solid #2c3c55;border-radius:15px;padding:20px}.card strong{display:block;font-size:30px;margin-top:8px;color:#69e4c5}.panel{margin-top:16px}label{display:block;margin-bottom:8px}input,select,button{background:#0d1728;color:#fff;border:1px solid #47607b;padding:12px;border-radius:9px;font-size:14px}button{background:#0cae91;color:#07241e;border:0;font-weight:700;cursor:pointer}button:hover{filter:brightness(1.1)}input[type=file]{max-width:100%}.flex{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.flex input{flex:1;min-width:180px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:12px;border-bottom:1px solid #304057}th{color:#83b6cf;white-space:nowrap}td{max-width:270px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}a{color:#7be9ce}#msg{min-height:24px;padding-top:12px;color:#87e5cf}.warning{color:#ffc67c}.mini{font-size:12px}.right{margin-left:auto}@media(max-width:700px){.cards{grid-template-columns:1fr}.card strong{font-size:22px}}</style></head><body><header><h1>◈ Portal COMEX <span style="color:#43d7b5">/ Clientes</span></h1><small>Importaciones · Identificación por RUT · Inteligencia comercial</small></header><main>
<div class="cards"><div class="card"><small>Operaciones cargadas</small><strong id="ops">—</strong></div><div class="card"><small>RUT únicos registrados</small><strong id="ruts">—</strong></div><div class="card"><small>Empresas con razón social</small><strong id="named">—</strong></div></div>
<section class="panel"><h2>1. Cargar datos</h2><p class="muted">CSV de Aduanas (DIN) o padrón RUT → razón social. La carga evita duplicar archivos idénticos y guarda los datos localmente.</p><div class="flex"><select id="kind"><option value="operations">Operaciones aduaneras</option><option value="companies">Padrón de empresas</option></select><input type="file" id="file" accept=".csv,.txt"><button onclick="upload()">Importar CSV</button></div><div id="msg"></div><div class="mini muted">Formato UTF-8 o Windows-1252; separador coma, punto y coma o tabulación. Para ZIP/RAR, extrae primero el CSV.</div></section>
<section class="panel"><h2>2. Buscar clientes</h2><div class="flex"><input id="q" placeholder="RUT, razón social o nombre en registro" onkeydown="if(event.key==='Enter')search()"><input id="country" placeholder="País de origen" onkeydown="if(event.key==='Enter')search()"><button onclick="search()">Buscar</button><button onclick="location.href='/api/export?'+filters()">Exportar CSV</button></div><p class="mini warning">Los nombres se muestran solo si constan en el archivo o en un padrón importado. Un RUT sin razón social queda pendiente.</p><div class="scroll"><table><thead><tr><th>RUT</th><th>Empresa / identificación</th><th>Operaciones (ítems)</th><th>Países</th><th>Total valores*</th><th>Estado</th></tr></thead><tbody id="results"></tbody></table></div><p class="mini muted">*Suma indicativa de valores del campo seleccionado. No sumar distintas monedas ni interpretar ítems DIN como embarques únicos.</p></section>
<section class="panel"><h2>3. Operaciones de una empresa</h2><p class="muted" id="sel">Selecciona un RUT de la tabla.</p><div class="scroll"><table><thead><tr><th>Fecha</th><th>DIN</th><th>Origen</th><th>Producto</th><th>HS</th><th>Valor</th><th>Moneda</th></tr></thead><tbody id="detail"></tbody></table></div></section>
</main><script>
const $=id=>document.getElementById(id);const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function filters(){return new URLSearchParams({q:$('q').value,country:$('country').value}).toString()}
async function refresh(){let s=await(await fetch('/api/stats')).json();$('ops').textContent=s.operations.toLocaleString('es-CL');$('ruts').textContent=s.ruts.toLocaleString('es-CL');$('named').textContent=s.named.toLocaleString('es-CL')}
async function upload(){let f=$('file').files[0];if(!f){$('msg').textContent='Selecciona primero un CSV';return}let fd=new FormData();fd.append('file',f);fd.append('kind',$('kind').value);$('msg').textContent='Importando y guardando registros…';try{let r=await fetch('/api/upload',{method:'POST',body:fd});let j=await r.json();if(!r.ok)throw Error(j.error);$('msg').textContent=j.duplicate?'Archivo ya importado: se evitó el duplicado.':`Importados ${j.rows} registros. Columnas detectadas: ${JSON.stringify(j.mapping)}`;await refresh();await search()}catch(e){$('msg').textContent='Error: '+e.message}}
async function search(){let r=await(await fetch('/api/companies?'+filters())).json();$('results').innerHTML=r.map(x=>`<tr><td><a href="#" onclick="detail('${esc(x.rut)}');return false">${esc(x.rut||'Sin RUT')}</a></td><td>${esc(x.name||x.source_name||'Sin identificar')}</td><td>${x.count}</td><td>${esc(x.countries)}</td><td>${x.value==null?'—':Number(x.value).toLocaleString('es-CL',{maximumFractionDigits:2})}</td><td>${x.name?'Identificado':'Pendiente'}</td></tr>`).join('')||'<tr><td colspan="6">Sin resultados. Importa archivos para comenzar.</td></tr>'}
async function detail(rut){$('sel').textContent='RUT: '+rut;let a=await(await fetch('/api/operations?rut='+encodeURIComponent(rut))).json();$('detail').innerHTML=a.map(x=>`<tr><td>${esc(x.date)}</td><td>${esc(x.declaration)}</td><td>${esc(x.country)}</td><td>${esc(x.product)}</td><td>${esc(x.hs)}</td><td>${esc(x.value)}</td><td>${esc(x.currency)}</td></tr>`).join('')||'<tr><td colspan="7">Sin operaciones</td></tr>'}
refresh();search();</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
 def send(self,body,status=200,ctype='application/json; charset=utf-8',filename=None):
  if isinstance(body,str):body=body.encode('utf-8')
  self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');
  if filename:self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
  self.end_headers();self.wfile.write(body)
 def j(self,data,status=200):self.send(json.dumps(data,ensure_ascii=False,default=str),status)
 def do_GET(self):
  p=urlparse(self.path);args=parse_qs(p.query);q=args.get('q',[''])[0].strip();country=args.get('country',[''])[0].strip()
  if p.path=='/':return self.send(HTML,'200' if False else 200,'text/html; charset=utf-8')
  with db() as c:
   if p.path=='/api/stats':
    return self.j({'operations':c.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 'ruts':c.execute("SELECT COUNT(DISTINCT rut) FROM operations WHERE rut != ''").fetchone()[0], 'named':c.execute('SELECT COUNT(*) FROM companies').fetchone()[0]})
   if p.path in ('/api/companies','/api/export'):
    rows=c.execute('''SELECT o.rut,MAX(co.name) AS name,MAX(o.source_name) AS source_name,COUNT(*) AS count, GROUP_CONCAT(DISTINCT o.country) AS countries,ROUND(SUM(o.value),2) AS value FROM operations o LEFT JOIN companies co ON co.rut=o.rut WHERE (?='' OR o.rut LIKE ? OR co.name LIKE ? OR o.source_name LIKE ?) AND (?='' OR o.country LIKE ?) GROUP BY o.rut ORDER BY count DESC LIMIT 5000''',(q,'%'+q+'%','%'+q+'%','%'+q+'%',country,'%'+country+'%')).fetchall()
    items=[dict(x) for x in rows]
    if p.path=='/api/companies':return self.j(items[:300])
    out=io.StringIO();w=csv.DictWriter(out,fieldnames=['rut','name','source_name','count','countries','value'],delimiter=';');w.writeheader();w.writerows(items);return self.send('\ufeff'+out.getvalue(),200,'text/csv; charset=utf-8','clientes_comex.csv')
   if p.path=='/api/operations':
    r=args.get('rut',[''])[0];return self.j([dict(x) for x in c.execute('SELECT date,declaration,country,product,hs,value,currency FROM operations WHERE rut=? ORDER BY id DESC LIMIT 300',(r,))])
  self.j({'error':'Ruta inexistente'},404)
 def do_POST(self):
  if urlparse(self.path).path!='/api/upload':return self.j({'error':'Ruta inexistente'},404)
  try:
   size=int(self.headers.get('Content-Length','0'))
   if size>MAX_UPLOAD: return self.j({'error':'Archivo excede 350 MB'},413)
   content_type=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in content_type: return self.j({'error':'Se requiere formulario multipart'},400)
   raw=self.rfile.read(size)
   msg=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
   parts={p.get_param('name',header='content-disposition'):p for p in msg.iter_parts()}
   if 'file' not in parts: return self.j({'error':'Falta archivo'},400)
   part=parts['file'];kind=parts.get('kind').get_content().strip() if 'kind' in parts else 'operations'
   filename=os.path.basename(part.get_filename() or 'archivo.csv')
   if not filename.lower().endswith(('.csv','.txt')):return self.j({'error':'Extrae el archivo y selecciona CSV/TXT'},400)
   if kind not in ('operations','companies'):return self.j({'error':'Tipo inválido'},400)
   result=import_csv(part.get_payload(decode=True),filename,kind);return self.j(result)
  except Exception as e:return self.j({'error':str(e)},400)
 def log_message(self,fmt,*args):pass
if __name__=='__main__':
 setup(); print(f'Portal COMEX listo en http://127.0.0.1:{PORT}'); ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
