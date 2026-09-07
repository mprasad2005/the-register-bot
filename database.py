import json
import sqlite3
from config import DB_PATH


def connect():
    con=sqlite3.connect(DB_PATH); con.row_factory=sqlite3.Row; return con


def init_db(initial_admin_ids=()):
    con=connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY,title TEXT NOT NULL,username TEXT,active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL, question TEXT NOT NULL, options TEXT NOT NULL,
        slots INTEGER NOT NULL DEFAULT 0, registration_required INTEGER NOT NULL DEFAULT 1,
        registration_open INTEGER NOT NULL DEFAULT 1, registration_deadline TEXT,
        status TEXT NOT NULL DEFAULT 'registration', telegram_message_id INTEGER, telegram_poll_id TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(group_id) REFERENCES groups(id)
    );
    CREATE TABLE IF NOT EXISTS registrations (id INTEGER PRIMARY KEY AUTOINCREMENT,poll_id INTEGER NOT NULL,user_id INTEGER NOT NULL,name TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(poll_id,user_id),FOREIGN KEY(poll_id) REFERENCES polls(id));
    CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY,first_name TEXT,username TEXT,started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY,added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS poll_votes (poll_id INTEGER NOT NULL,user_id INTEGER NOT NULL,option_ids TEXT NOT NULL,answered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(poll_id,user_id),FOREIGN KEY(poll_id) REFERENCES polls(id));
    CREATE TABLE IF NOT EXISTS poll_results (poll_id INTEGER PRIMARY KEY,valid_counts TEXT NOT NULL,invalid_total INTEGER NOT NULL DEFAULT 0,checked_total INTEGER NOT NULL DEFAULT 0,telegram_total INTEGER NOT NULL DEFAULT 0,verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(poll_id) REFERENCES polls(id));
    """)
    cols={r[1] for r in con.execute("PRAGMA table_info(polls)").fetchall()}
    group_cols={r[1] for r in con.execute("PRAGMA table_info(groups)").fetchall()}
    if "username" not in group_cols: con.execute("ALTER TABLE groups ADD COLUMN username TEXT")
    if "telegram_poll_id" not in cols: con.execute("ALTER TABLE polls ADD COLUMN telegram_poll_id TEXT")
    if "slots" not in cols: con.execute("ALTER TABLE polls ADD COLUMN slots INTEGER NOT NULL DEFAULT 0")
    if "registration_required" not in cols: con.execute("ALTER TABLE polls ADD COLUMN registration_required INTEGER NOT NULL DEFAULT 1")
    con.executemany("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", ((user_id,) for user_id in initial_admin_ids))
    con.commit(); con.close()


def get_groups():
    con=connect(); rows=con.execute("SELECT id,title FROM groups WHERE active=1 ORDER BY title").fetchall(); con.close(); return rows

def get_group(group_id):
    con=connect(); r=con.execute("SELECT * FROM groups WHERE id=?",(group_id,)).fetchone(); con.close(); return r

def add_group(group_id,title,username=None):
    con=connect(); con.execute("INSERT INTO groups(id,title,username,active) VALUES(?,?,?,1) ON CONFLICT(id) DO UPDATE SET title=excluded.title,username=excluded.username,active=1",(group_id,title,username)); con.commit(); con.close()

def remove_group(group_id):
    con=connect(); con.execute("UPDATE groups SET active=0 WHERE id=?",(group_id,)); con.commit(); con.close()

def create_poll(group_id,question,options,slots=0,registration_required=1):
    con=connect(); cur=con.execute("INSERT INTO polls(group_id,question,options,slots,registration_required) VALUES(?,?,?,?,?)",(group_id,question,json.dumps(options,ensure_ascii=False),slots,registration_required)); con.commit(); pid=cur.lastrowid; con.close(); return pid

def get_poll(poll_id):
    con=connect(); r=con.execute("SELECT * FROM polls WHERE id=?",(poll_id,)).fetchone(); con.close(); return r

def active_poll(group_id):
    con=connect(); r=con.execute("SELECT * FROM polls WHERE group_id=? AND status='registration' AND registration_open=1 ORDER BY id DESC LIMIT 1",(group_id,)).fetchone(); con.close(); return r

def registration_polls():
    con=connect(); r=con.execute("SELECT * FROM polls WHERE status='registration' AND registration_open=1 ORDER BY id DESC").fetchall(); con.close(); return r

def ready_polls():
    con=connect(); r=con.execute("SELECT * FROM polls WHERE status='ready' ORDER BY id DESC").fetchall(); con.close(); return r

def live_polls(group_id=None):
    con=connect()
    r=con.execute("SELECT * FROM polls WHERE status='live' ORDER BY id DESC").fetchall() if group_id is None else con.execute("SELECT * FROM polls WHERE group_id=? AND status='live' ORDER BY id DESC",(group_id,)).fetchall()
    con.close(); return r

def all_polls(group_id):
    con=connect(); r=con.execute("SELECT * FROM polls WHERE group_id=? ORDER BY id DESC",(group_id,)).fetchall(); con.close(); return r

def completed_polls(group_id):
    con=connect(); r=con.execute("SELECT * FROM polls WHERE group_id=? AND status='completed' ORDER BY id DESC",(group_id,)).fetchall(); con.close(); return r

def completed_polls_all():
    con=connect(); r=con.execute("SELECT * FROM polls WHERE status='completed' ORDER BY id DESC").fetchall(); con.close(); return r

def register(poll_id,user_id,name):
    con=connect(); poll=con.execute("SELECT * FROM polls WHERE id=?",(poll_id,)).fetchone()
    if not poll or poll["status"]!='registration' or not poll["registration_open"]: con.close(); return False,"closed",0,0
    count=con.execute("SELECT COUNT(*) FROM registrations WHERE poll_id=?",(poll_id,)).fetchone()[0]
    if con.execute("SELECT 1 FROM registrations WHERE poll_id=? AND user_id=?",(poll_id,user_id)).fetchone(): con.close(); return False,"duplicate",count,poll["slots"]
    slots=poll["slots"] or 0
    if slots and count>=slots: con.execute("UPDATE polls SET registration_open=0,status='ready' WHERE id=?",(poll_id,)); con.commit(); con.close(); return False,"full",count,slots
    con.execute("INSERT INTO registrations(poll_id,user_id,name) VALUES(?,?,?)",(poll_id,user_id,name))
    count+=1
    if slots and count>=slots: con.execute("UPDATE polls SET registration_open=0,status='ready' WHERE id=?",(poll_id,))
    con.commit(); con.close(); return True,"ok",count,slots

def registrations(poll_id):
    con=connect(); r=con.execute("SELECT r.*,u.username FROM registrations r LEFT JOIN users u ON u.user_id=r.user_id WHERE r.poll_id=? ORDER BY r.id",(poll_id,)).fetchall(); con.close(); return r

def registration_count(poll_id):
    con=connect(); n=con.execute("SELECT COUNT(*) FROM registrations WHERE poll_id=?",(poll_id,)).fetchone()[0]; con.close(); return n

def close_registration(poll_id):
    con=connect(); con.execute("UPDATE polls SET registration_open=0,status='ready' WHERE id=?",(poll_id,)); con.commit(); con.close()

def set_poll_message(poll_id,message_id,telegram_poll_id=None):
    con=connect(); con.execute("UPDATE polls SET telegram_message_id=?,telegram_poll_id=? WHERE id=?",(message_id,telegram_poll_id,poll_id)); con.commit(); con.close()

def mark_live(poll_id):
    con=connect(); con.execute("UPDATE polls SET status='live' WHERE id=?",(poll_id,)); con.commit(); con.close()

def mark_completed(poll_id):
    con=connect(); con.execute("UPDATE polls SET status='completed',registration_open=0 WHERE id=?",(poll_id,)); con.commit(); con.close()

def get_poll_by_telegram_poll(telegram_poll_id):
    con=connect(); r=con.execute("SELECT * FROM polls WHERE telegram_poll_id=?",(telegram_poll_id,)).fetchone(); con.close(); return r

def save_vote(poll_id,user_id,option_ids):
    con=connect()
    if not option_ids: con.execute("DELETE FROM poll_votes WHERE poll_id=? AND user_id=?",(poll_id,user_id))
    else: con.execute("INSERT INTO poll_votes(poll_id,user_id,option_ids) VALUES(?,?,?) ON CONFLICT(poll_id,user_id) DO UPDATE SET option_ids=excluded.option_ids,answered_at=CURRENT_TIMESTAMP",(poll_id,user_id,json.dumps(list(option_ids))))
    con.commit(); con.close()

def get_votes(poll_id):
    con=connect(); r=con.execute("SELECT * FROM poll_votes WHERE poll_id=?",(poll_id,)).fetchall(); con.close(); return r

def vote_count(poll_id):
    con=connect(); n=con.execute("SELECT COUNT(*) FROM poll_votes WHERE poll_id=?",(poll_id,)).fetchone()[0]; con.close(); return n

def save_poll_results(poll_id,valid_counts,invalid_total,checked_total,telegram_total=0):
    con=connect(); con.execute("INSERT INTO poll_results(poll_id,valid_counts,invalid_total,checked_total,telegram_total) VALUES(?,?,?,?,?) ON CONFLICT(poll_id) DO UPDATE SET valid_counts=excluded.valid_counts,invalid_total=excluded.invalid_total,checked_total=excluded.checked_total,telegram_total=excluded.telegram_total,verified_at=CURRENT_TIMESTAMP",(poll_id,json.dumps(list(valid_counts)),invalid_total,checked_total,telegram_total)); con.commit(); con.close()

def get_poll_results(poll_id):
    con=connect(); r=con.execute("SELECT *,json_array_length(valid_counts) AS _n FROM poll_results WHERE poll_id=?",(poll_id,)).fetchone(); con.close(); return r

def save_user(user_id,first_name=None,username=None):
    con=connect(); con.execute("INSERT INTO users(user_id,first_name,username) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name,username=excluded.username",(user_id,first_name,username)); con.commit(); con.close()

def get_users():
    con=connect(); r=con.execute("SELECT * FROM users ORDER BY started_at").fetchall(); con.close(); return r

def stats():
    con=connect(); r={"groups":con.execute("SELECT COUNT(*) FROM groups WHERE active=1").fetchone()[0],"polls":con.execute("SELECT COUNT(*) FROM polls").fetchone()[0],"registrations":con.execute("SELECT COUNT(*) FROM registrations").fetchone()[0],"votes":con.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0]}; con.close(); return r

def is_admin(user_id):
    con=connect(); row=con.execute("SELECT 1 FROM admins WHERE user_id=?",(user_id,)).fetchone(); con.close(); return row is not None

def get_admins():
    con=connect(); rows=con.execute("SELECT user_id,added_at FROM admins ORDER BY added_at,user_id").fetchall(); con.close(); return rows

def add_admin(user_id):
    con=connect(); con.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)",(user_id,)); con.commit(); con.close()

def remove_admin(user_id):
    con=connect(); con.execute("DELETE FROM admins WHERE user_id=?",(user_id,)); con.commit(); con.close()
