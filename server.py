"""CyberX Lucky Drop v3 test backend. Python 3.11+, FastAPI.
No real LANGAME credits are issued."""
import os, json, hmac, hashlib, secrets, sqlite3, time, random
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from fastapi import FastAPI, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE=Path(__file__).resolve().parent
DB=os.getenv('CYBERX_DB',str(BASE/'cyberx.sqlite3'))
BOT_TOKEN=os.getenv('BOT_TOKEN','')
OWNER_ID=int(os.getenv('OWNER_TELEGRAM_ID','933124893'))
TEST_MODE=os.getenv('CYBERX_TEST_MODE','1')=='1'
VALUES=[25,50,100,150,200,250,300,350,500,1000,2000,5000,10000]
WEIGHTS=[30,25,15,10,7,4,3,2,1.5,1,.8,.5,.2]
app=FastAPI(title='CyberX Lucky Drop API',version='3.0-test')

@contextmanager
def db():
    c=sqlite3.connect(DB,timeout=15,isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=15000')
    try: yield c
    finally: c.close()

with db() as c:
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, coins INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL DEFAULT 'player');
    CREATE TABLE IF NOT EXISTS tickets (code_hash TEXT PRIMARY KEY, amount INTEGER NOT NULL, issued_by INTEGER NOT NULL, redeemed_by INTEGER, redeemed_at INTEGER);
    CREATE TABLE IF NOT EXISTS wins (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, value INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL, delivered_at INTEGER, delivered_by INTEGER);
    CREATE TABLE IF NOT EXISTS operations (id INTEGER PRIMARY KEY AUTOINCREMENT, actor INTEGER, action TEXT NOT NULL, details TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    c.execute("INSERT OR IGNORE INTO settings VALUES ('weights',?)",(json.dumps(WEIGHTS),))
    c.execute("INSERT OR IGNORE INTO users (id,role) VALUES (?,'owner')",(OWNER_ID,))
    c.execute("UPDATE users SET role='owner' WHERE id=?",(OWNER_ID,))

class Auth(BaseModel): init_data:str
class Redeem(Auth): code:str=Field(min_length=8,max_length=128)
class Issue(Auth): amount:int; count:int=Field(ge=1,le=100)
class Staff(Auth): user_id:int; enabled:bool
class Deliver(Auth): win_id:int=Field(ge=1)
class Odds(Auth): weights:list[float]

def user_id(init_data):
    """Validate Telegram WebApp initData as documented by Telegram."""
    if TEST_MODE and init_data.startswith('dev:'):
        try: return int(init_data[4:])
        except ValueError: raise HTTPException(401,'Bad dev identity')
    if not BOT_TOKEN: raise HTTPException(503,'BOT_TOKEN not configured')
    try:
        data=dict(parse_qsl(init_data,keep_blank_values=True))
        provided=data.pop('hash')
        auth_date=int(data['auth_date'])
        if abs(time.time()-auth_date)>3600: raise ValueError('Expired authentication')
        check='\n'.join(f'{k}={v}' for k,v in sorted(data.items()))
        secret=hmac.new(b'WebAppData',BOT_TOKEN.encode(),hashlib.sha256).digest()
        correct=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(correct,provided): raise ValueError('Invalid hash')
        profile=json.loads(data['user']); uid=int(profile['id'])
        with db() as c:
            c.execute('INSERT OR IGNORE INTO users(id) VALUES (?)',(uid,))
            c.execute('UPDATE users SET username=? WHERE id=?',(profile.get('username'),uid))
        return uid
    except (KeyError,ValueError,TypeError,json.JSONDecodeError):
        raise HTTPException(401,'Invalid Telegram login')

def require(uid,roles):
    if uid==OWNER_ID: return 'owner'
    with db() as c: row=c.execute('SELECT role FROM users WHERE id=?',(uid,)).fetchone()
    role=row['role'] if row else 'player'
    if role not in roles: raise HTTPException(403,'Access denied')
    return role

def log(c,uid,action,details):
    c.execute('INSERT INTO operations(actor,action,details,created_at) VALUES(?,?,?,?)',(uid,action,json.dumps(details,ensure_ascii=False),int(time.time())))

@app.post('/api/me')
def me(a:Auth):
    uid=user_id(a.init_data)
    with db() as c:
        c.execute('INSERT OR IGNORE INTO users(id) VALUES (?)',(uid,))
        user=c.execute('SELECT coins,role,username FROM users WHERE id=?',(uid,)).fetchone()
        wins=[dict(x) for x in c.execute('SELECT id,value,status,created_at FROM wins WHERE user_id=? ORDER BY id DESC LIMIT 100',(uid,))]
    return {'id':uid,'coins':user['coins'],'role':'owner' if uid==OWNER_ID else user['role'],'wins':wins,'test_mode':TEST_MODE}

@app.post('/api/ticket/redeem')
def redeem(a:Redeem):
    uid=user_id(a.init_data)
    digest=hashlib.sha256(a.code.strip().upper().encode()).hexdigest()
    with db() as c:
        try:
            c.execute('BEGIN IMMEDIATE')
            t=c.execute('SELECT amount,redeemed_by FROM tickets WHERE code_hash=?',(digest,)).fetchone()
            if not t: raise HTTPException(404,'Билет не найден')
            if t['redeemed_by'] is not None: raise HTTPException(409,'Билет уже активирован')
            c.execute('INSERT OR IGNORE INTO users(id) VALUES (?)',(uid,))
            c.execute('UPDATE tickets SET redeemed_by=?,redeemed_at=? WHERE code_hash=?',(uid,int(time.time()),digest))
            c.execute('UPDATE users SET coins=coins+? WHERE id=?',(t['amount'],uid))
            log(c,uid,'ticket_redeemed',{'amount':t['amount']})
            c.execute('COMMIT')
            return {'added':t['amount']}
        except Exception:
            c.execute('ROLLBACK');raise

@app.post('/api/spin')
def spin(a:Auth):
    uid=user_id(a.init_data)
    with db() as c:
        try:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO users(id) VALUES (?)',(uid,))
            updated=c.execute('UPDATE users SET coins=coins-50 WHERE id=? AND coins>=50',(uid,))
            if updated.rowcount!=1: raise HTTPException(409,'Недостаточно Cyber Coin')
            weights=json.loads(c.execute("SELECT value FROM settings WHERE key='weights'").fetchone()['value'])
            value=random.SystemRandom().choices(VALUES,weights=weights,k=1)[0]
            cur=c.execute('INSERT INTO wins(user_id,value,created_at) VALUES (?,?,?)',(uid,value,int(time.time())))
            coins=c.execute('SELECT coins FROM users WHERE id=?',(uid,)).fetchone()['coins']
            log(c,uid,'spin',{'win_id':cur.lastrowid,'value':value})
            c.execute('COMMIT')
            return {'win_id':cur.lastrowid,'value':value,'coins':coins}
        except Exception:
            c.execute('ROLLBACK');raise

@app.post('/api/admin/tickets')
def tickets(a:Issue):
    uid=user_id(a.init_data);require(uid,('owner',))
    if a.amount not in (200,300,500,1000): raise HTTPException(422,'Invalid ticket amount')
    codes=[]
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        for _ in range(a.count):
            code='CX-'+secrets.token_hex(12).upper()
            c.execute('INSERT INTO tickets(code_hash,amount,issued_by) VALUES(?,?,?)',(hashlib.sha256(code.encode()).hexdigest(),a.amount,uid))
            codes.append(code)
        log(c,uid,'tickets_issued',{'amount':a.amount,'count':a.count})
        c.execute('COMMIT')
    return {'codes':codes,'warning':'Сохраните коды сейчас: повторно получить их с сервера нельзя.'}

@app.post('/api/admin/deliver')
def deliver(a:Deliver):
    uid=user_id(a.init_data);require(uid,('owner','staff'))
    with db() as c:
        try:
            c.execute('BEGIN IMMEDIATE')
            win=c.execute('SELECT user_id,value,status FROM wins WHERE id=?',(a.win_id,)).fetchone()
            if not win: raise HTTPException(404,'Приз не найден')
            if win['status']!='pending': raise HTTPException(409,'Уже выдан')
            c.execute("UPDATE wins SET status='delivered',delivered_at=?,delivered_by=? WHERE id=? AND status='pending'",(int(time.time()),uid,a.win_id))
            log(c,uid,'prize_delivered',{'win_id':a.win_id,'value':win['value'],'player':win['user_id']})
            c.execute('COMMIT')
            return {'ok':True,'value':win['value'],'instruction':'Начислите этот приз в LANGAME вручную и убедитесь в фактическом начислении.'}
        except Exception:
            c.execute('ROLLBACK');raise

@app.post('/api/admin/staff')
def staff(a:Staff):
    uid=user_id(a.init_data);require(uid,('owner',))
    if a.user_id==OWNER_ID:raise HTTPException(422,'Cannot change owner')
    with db() as c:
        c.execute('INSERT OR IGNORE INTO users(id) VALUES (?)',(a.user_id,))
        c.execute('UPDATE users SET role=? WHERE id=?',('staff' if a.enabled else 'player',a.user_id))
        log(c,uid,'staff_role',{'user':a.user_id,'enabled':a.enabled})
    return {'ok':True}

@app.post('/api/admin/odds')
def odds(a:Odds):
    uid=user_id(a.init_data);require(uid,('owner',))
    if len(a.weights)!=len(VALUES) or any(x<0 or x>100 for x in a.weights) or abs(sum(a.weights)-100)>0.001:
        raise HTTPException(422,'13 probabilities must be nonnegative and total 100%')
    with db() as c:
        c.execute("UPDATE settings SET value=? WHERE key='weights'",(json.dumps(a.weights),))
        log(c,uid,'weights_changed',{'weights':a.weights})
    return {'ok':True}

@app.post('/api/admin/stats')
def stats(a:Auth):
    uid=user_id(a.init_data);require(uid,('owner',))
    with db() as c:
        return {'users':c.execute('SELECT count(*) FROM users').fetchone()[0], 'wins':c.execute('SELECT count(*) FROM wins').fetchone()[0], 'pending':c.execute("SELECT count(*) FROM wins WHERE status='pending'").fetchone()[0], 'tickets_redeemed':c.execute('SELECT count(*) FROM tickets WHERE redeemed_by IS NOT NULL').fetchone()[0]}

app.mount('/static',StaticFiles(directory=BASE/'public'),name='static')
@app.get('/')
def index():return FileResponse(BASE/'public'/'index.html')
