"""scout 账号系统鉴权原语: 密码哈希(pbkdf2) + 会话 + 用户 CRUD. 纯标准库, 接收 conn."""
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

_ALGO = "pbkdf2_sha256"
_ITERS = 200_000
SESSION_DAYS = 7
_TS = "%Y-%m-%d %H:%M:%S"


def hash_password(pw: str, *, iters: int = _ITERS, salt: bytes = None) -> str:
    """生成 pbkdf2_sha256$<迭代>$<salt_b64>$<hash_b64>. salt 缺省随机 16 字节."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iters)
    return f"{_ALGO}${iters}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    """常数时间比对. stored 非法格式一律 False."""
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        iters = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, AttributeError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


def _now() -> datetime:
    return datetime.now()


def create_session(c, user_id: int, *, days: int = SESSION_DAYS) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    exp = now + timedelta(days=days)
    c.execute(
        "INSERT INTO scout_session(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        (token, user_id, now.strftime(_TS), exp.strftime(_TS)))
    c.commit()
    return token


def resolve_session(c, token: str):
    """token → 用户 dict{id,username,display_name,role}; 过期/停用/不存在 → None."""
    if not token:
        return None
    row = c.execute(
        "SELECT u.id, u.username, u.display_name, u.role, u.status, s.expires_at "
        "FROM scout_session s JOIN scout_user u ON u.id = s.user_id WHERE s.token=?",
        (token,)).fetchone()
    if row is None:
        return None
    if row["expires_at"] < _now().strftime(_TS):   # 定宽时间串可直接字典序比较
        return None
    if row["status"] != "active":
        return None
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"]}


def delete_session(c, token: str) -> None:
    c.execute("DELETE FROM scout_session WHERE token=?", (token,))
    c.commit()


def purge_expired(c) -> None:
    c.execute("DELETE FROM scout_session WHERE expires_at < ?", (_now().strftime(_TS),))
    c.commit()


def create_user(c, username: str, password: str, display_name: str = None,
                role: str = "user") -> int:
    """建号. 用户名重复抛 sqlite3.IntegrityError(UNIQUE). 返回新 id."""
    now = _now().strftime(_TS)
    cur = c.execute(
        "INSERT INTO scout_user(username,password_hash,display_name,role,status,created_at,updated_at) "
        "VALUES(?,?,?,?, 'active', ?, ?)",
        (username, hash_password(password), display_name or username, role, now, now))
    c.commit()
    return cur.lastrowid


def authenticate(c, username: str, password: str):
    """返回 ('ok', row) | ('bad', None) | ('disabled', None)."""
    row = c.execute("SELECT * FROM scout_user WHERE username=?", (username,)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return "bad", None
    if row["status"] != "active":
        return "disabled", None
    return "ok", row


def list_users(c):
    rows = c.execute(
        "SELECT id, username, display_name, role, status, created_at "
        "FROM scout_user ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def count_active_admins(c) -> int:
    return c.execute(
        "SELECT COUNT(*) FROM scout_user WHERE role='admin' AND status='active'").fetchone()[0]


def update_user(c, user_id: int, *, display_name: str = None, role: str = None,
                status: str = None):
    """改昵称/角色/状态. 不能停用或降级最后一个 active admin → 返回 (False, err).
    成功 (True, None)."""
    row = c.execute("SELECT role, status FROM scout_user WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return False, "用户不存在"
    # 护栏: 该用户当前是 active admin, 且本次会让它不再是 active admin, 且它是最后一个
    is_active_admin = row["role"] == "admin" and row["status"] == "active"
    will_lose_admin = (role is not None and role != "admin") or \
                      (status is not None and status != "active")
    if is_active_admin and will_lose_admin and count_active_admins(c) <= 1:
        return False, "不能停用或降级最后一个管理员"
    sets, vals = [], []
    if display_name is not None:
        sets.append("display_name=?"); vals.append(display_name)
    if role is not None:
        sets.append("role=?"); vals.append(role)
    if status is not None:
        sets.append("status=?"); vals.append(status)
    if sets:
        sets.append("updated_at=?"); vals.append(_now().strftime(_TS))
        vals.append(user_id)
        c.execute(f"UPDATE scout_user SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
    return True, None


def set_password(c, user_id: int, password: str) -> None:
    c.execute("UPDATE scout_user SET password_hash=?, updated_at=? WHERE id=?",
              (hash_password(password), _now().strftime(_TS), user_id))
    c.commit()


def reassign_orphan_holdings(c, owner_id: int) -> None:
    """把所有 owner_id IS NULL 的持仓/清仓记录归到 owner_id(老数据迁移用)."""
    c.execute("UPDATE follow_holding SET owner_id=? WHERE owner_id IS NULL", (owner_id,))
    c.execute("UPDATE follow_trade_closed SET owner_id=? WHERE owner_id IS NULL", (owner_id,))
    c.commit()
