#!/usr/bin/env python3
"""Skynet Sync backend. Standard library only: http.server + sqlite3 + scrypt."""
import hashlib, hmac, json, os, re, secrets, sqlite3, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ.get("SKYNET_DB", "/var/lib/skynet-sync/skynet.db")
HOST = os.environ.get("SKYNET_HOST", "127.0.0.1")
PORT = int(os.environ.get("SKYNET_PORT", "8080"))
MAX_NODES = 5
SESSION_TTL = 30 * 86400
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
SID_RE = re.compile(r"(?:^|;\s*)sid=([0-9a-f]{64})")
fails = {}  # email -> (failed attempts, time of last failure)


def run(sql, args=(), fetch=None):
    c = sqlite3.connect(DB, timeout=10)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        cur = c.execute(sql, args)
        r = cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else cur.rowcount
        c.commit()
        return r
    finally:
        c.close()


def init():
    c = sqlite3.connect(DB)
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw TEXT NOT NULL,
        key_hash TEXT UNIQUE, created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, expires INTEGER NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS nodes(
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL, seen INTEGER NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(user_id, name));
    """)
    c.close()


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def hash_pw(pw, salt=None):
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex() + ":" + h.hex()


def check_pw(pw, stored):
    salt, h = stored.split(":")
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(salt)).split(":")[1], h)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None, cookie=None):
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        # Requiring JSON content type also blocks cross-site form posts (CSRF).
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ValueError("Content-Type must be application/json.")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 16384:
            raise ValueError("Payload too large.")
        d = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(d, dict):
            raise ValueError("Body must be a JSON object.")
        return d

    def sid(self):
        m = SID_RE.search(self.headers.get("Cookie") or "")
        return m.group(1) if m else None

    def user(self):
        t = self.sid()
        if not t:
            return None
        return run("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                   "WHERE s.token=? AND s.expires>?", (sha(t), int(time.time())), "one")

    def route(self, method):
        try:
            fn = ROUTES.get((method, self.path.split("?")[0]))
            if fn:
                fn(self)
            else:
                self.send(404, {"error": "Not found."})
        except ValueError as e:
            self.send(400, {"error": str(e)})
        except Exception:
            self.send(500, {"error": "Internal error."})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")


def creds(h):
    d = h.body()
    email, pw = str(d.get("email", "")).strip().lower(), str(d.get("password", ""))
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    if not 8 <= len(pw) <= 128:
        raise ValueError("Password must be 8 to 128 characters.")
    return email, pw


def start_session(h, uid, code=200):
    now = int(time.time())
    run("DELETE FROM sessions WHERE expires<?", (now,))
    tok = secrets.token_hex(32)
    run("INSERT INTO sessions VALUES(?,?,?)", (sha(tok), now + SESSION_TTL, uid))
    h.send(code, {"redirect": "/dashboard"},
           f"sid={tok}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; Secure; SameSite=Lax")


def register(h):
    email, pw = creds(h)
    try:
        run("INSERT INTO users(email, pw, created) VALUES(?,?,?)", (email, hash_pw(pw), int(time.time())))
    except sqlite3.IntegrityError:
        return h.send(409, {"error": "This email is already registered."})
    start_session(h, run("SELECT id FROM users WHERE email=?", (email,), "one")["id"], 201)


def login(h):
    email, pw = creds(h)
    n, t = fails.get(email, (0, 0))
    if time.time() - t > 600:
        n = 0
    if n >= 5:
        return h.send(429, {"error": "Too many attempts. Try again in a few minutes."})
    u = run("SELECT * FROM users WHERE email=?", (email,), "one")
    if not (check_pw(pw, u["pw"]) if u else hash_pw(pw) and False):
        if len(fails) > 10000:
            fails.clear()
        fails[email] = (n + 1, time.time())
        return h.send(401, {"error": "Invalid email or password."})
    fails.pop(email, None)
    start_session(h, u["id"])


def logout(h):
    h.body()
    t = h.sid()
    if t:
        run("DELETE FROM sessions WHERE token=?", (sha(t),))
    h.send(200, {}, "sid=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax")


def me(h):
    u = h.user()
    if not u:
        return h.send(401, {"error": "Not signed in."})
    rows = run("SELECT name, seen, data FROM nodes WHERE user_id=? ORDER BY name", (u["id"],), "all")
    h.send(200, {"email": u["email"], "has_key": bool(u["key_hash"]), "max_nodes": MAX_NODES,
                 "nodes": [{"name": r["name"], "seen": r["seen"], "data": json.loads(r["data"])} for r in rows]})


def new_key(h):
    h.body()
    u = h.user()
    if not u:
        return h.send(401, {"error": "Not signed in."})
    key = "sk_" + secrets.token_urlsafe(24)
    run("UPDATE users SET key_hash=? WHERE id=?", (sha(key), u["id"]))
    h.send(200, {"api_key": key})  # shown once, only the hash is stored


def telemetry(h):
    key = h.headers.get("X-API-Key", "")
    u = run("SELECT id FROM users WHERE key_hash=?", (sha(key),), "one") if key else None
    if not u:
        return h.send(401, {"error": "Invalid API key."})
    d = h.body()
    name = str(d.pop("node", "node-1"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", name):
        raise ValueError("Node name: 1 to 32 characters, letters, digits, _ . -")
    data = json.dumps(d)
    if len(data) > 2000:
        raise ValueError("Telemetry payload too large.")
    known = run("SELECT 1 FROM nodes WHERE user_id=? AND name=?", (u["id"], name), "one")
    if not known and run("SELECT COUNT(*) c FROM nodes WHERE user_id=?", (u["id"],), "one")["c"] >= MAX_NODES:
        return h.send(403, {"error": "Node limit reached for the free tier."})
    run("INSERT INTO nodes VALUES(?,?,?,?) ON CONFLICT(user_id, name) "
        "DO UPDATE SET seen=excluded.seen, data=excluded.data", (u["id"], name, int(time.time()), data))
    h.send(200, {"ok": True})


ROUTES = {
    ("POST", "/api/auth/register"): register,
    ("POST", "/api/auth/login"): login,
    ("POST", "/api/auth/logout"): logout,
    ("GET", "/api/me"): me,
    ("POST", "/api/key"): new_key,
    ("POST", "/api/telemetry"): telemetry,
}

if __name__ == "__main__":
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    init()
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
