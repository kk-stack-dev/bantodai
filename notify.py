"""Web Push 通知 (hub だけが使う)

- pywebpush が入っていれば有効。入っていなければ hub はそのまま動き、通知だけが無効になる
- VAPID 鍵は初回起動時に vapid_private.pem を作る。購読は push_subscriptions.json に保存する (どちらも git に入れない)
- hub の poll ごとに、前回の状態との差分から出来事を拾って全購読先へ送る
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    AVAILABLE = True
except ImportError:  # 依存が無ければ通知だけ無効
    AVAILABLE = False

# 出来事の種類。config.json の notify.events で個別に止められる
EVENTS = {
    "ai_done": "AI の処理が終わった",
    "attention": "人の番が増えた (承認待ち・確認待ち)",
    "gaming_up": "Gaming PC が起動した",
    "link": "Launch のリンクが落ちた・戻った",
    "mac_load": "Mac mini のメモリプレッシャー・熱による制限",
}

DEFAULT_NOTIFY = {
    "enabled": True,
    "events": {k: True for k in EVENTS},
    # Push サービス (Apple など) に伝える連絡先 (VAPID の sub)。"mailto:you@example.com" か、
    # パスの無い "https://ホスト名" だけが通る。届かないときの問い合わせ先なので自分のものに変えてよい
    "contact": "https://github.com",
    "mem_pressure": 85,  # これ以上で通知、10 下がったら解除
    "link_down_polls": 2,  # 何回続けて落ちていたら通知するか (一瞬の失敗で鳴らさない)
    "cooldown": 1800,  # 同じ出来事 (Mac の負荷など) を繰り返し通知しない秒数
}


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# ------------------------------------------------------------- 差分検出 ----

def _sessions(state: dict) -> tuple[dict, set]:
    """(セッションごとの状態, オンラインだったデバイス) を返す。
    キーは (デバイス, プロジェクト, タスク, セッション)。ボードが閉じているプロジェクトは数えない
    (agent が running/waiting を idle に寄せるので、閉じただけで「終わった」と誤検出するため)"""
    out, online = {}, set()
    for d in state.get("devices") or []:
        if not d.get("online"):
            continue
        online.add(d["id"])
        for p in ((d.get("snapshot") or {}).get("banto") or {}).get("projects") or []:
            if not p.get("board_open"):
                continue
            for t in p.get("tasks") or []:
                base = (d["id"], p.get("file"), t.get("id"))
                out[base + ("task",)] = {"review": bool(t.get("review")), "title": t.get("title"),
                                         "device": d.get("name"), "project": p.get("name")}
                for s in t.get("sessions") or []:
                    out[base + (s.get("id"),)] = {"status": s.get("status"), "name": s.get("name"),
                                                  "request": s.get("request"), "title": t.get("title"),
                                                  "device": d.get("name"), "project": p.get("name")}
    return out, online


def detect(prev: dict, cur: dict, cfg: dict, mem: dict) -> list[dict]:
    """前回と今回の状態から通知する出来事を並べる。mem は呼び出しをまたいで持ち越す作業領域"""
    ev: list[dict] = []
    on = cfg.get("events") or {}
    now = time.time()

    def cooled(key: str) -> bool:
        if now - mem.setdefault("last", {}).get(key, 0) < float(cfg.get("cooldown", 1800)):
            return False
        mem["last"][key] = now
        return True

    # --- banto ---
    ps, p_online = _sessions(prev)
    cs, _ = _sessions(cur)
    for k, c in cs.items():
        p = ps.get(k)
        if p is None and k[0] not in p_online:
            continue  # デバイスが戻ってきただけ。前の様子が分からないので鳴らさない
        where = f"{c['device']} · {c['project']}"
        if k[-1] == "task":
            if on.get("attention", True) and c["review"] and not (p or {}).get("review"):
                ev.append({"kind": "attention", "title": f"確認待ち: {c['title']}", "body": where,
                           "tag": "rev:" + ":".join(map(str, k)), "tab": "attn"})
            continue
        was, st = (p or {}).get("status"), c["status"]
        if on.get("attention", True) and st == "waiting" and was != "waiting":
            ev.append({"kind": "attention", "title": f"承認待ち: {c['title']}",
                       "body": (c.get("request") or c.get("name") or "") + f"\n{where}",
                       "tag": "wait:" + ":".join(map(str, k)), "tab": "attn"})
        elif on.get("ai_done", True) and was in ("running", "waiting") and st in ("idle", "done", "failed"):
            head = "AI が失敗しました" if st == "failed" else "AI の処理が終わりました"
            ev.append({"kind": "ai_done", "title": f"{head}: {c['title']}",
                       "body": f"{c.get('name') or ''}\n{where}".strip(),
                       "tag": "done:" + ":".join(map(str, k)), "tab": "attn"})

    # --- Gaming PC ---
    pg, cg = prev.get("gaming") or {}, cur.get("gaming") or {}
    if on.get("gaming_up", True) and cg.get("online") and pg and not pg.get("online"):
        ev.append({"kind": "gaming_up", "title": f"{cg.get('name') or 'Gaming PC'} が起動しました",
                   "body": cg.get("host") or "", "tag": "gaming", "tab": "machines"})

    # --- Launch のリンク (続けて落ちたときだけ。戻ったら通知) ---
    if on.get("link", True):
        need = max(1, int(cfg.get("link_down_polls", 2)))
        streak, down = mem.setdefault("link_streak", {}), mem.setdefault("link_down", set())
        for l in cur.get("links") or []:
            name = l.get("name")
            if l.get("up") is None:
                continue
            if l["up"]:
                streak[name] = 0
                if name in down:
                    down.discard(name)
                    ev.append({"kind": "link", "title": f"{name} が戻りました", "body": "", "tag": "link:" + name, "tab": "attn"})
            else:
                streak[name] = streak.get(name, 0) + 1
                if streak[name] >= need and name not in down:
                    down.add(name)
                    ev.append({"kind": "link", "title": f"{name} が応答しません", "body": l.get("url", "").replace("{host}", ""),
                               "tag": "link:" + name, "tab": "attn"})

    # --- Mac mini の負荷 ---
    m = cur.get("mac") or {}
    if on.get("mac_load", True) and m:
        th = float(cfg.get("mem_pressure", 85))
        mp = m.get("mem_pressure")
        if mp is not None:
            if mp >= th and not mem.get("mem_hot"):
                mem["mem_hot"] = True
                if cooled("mem"):
                    ev.append({"kind": "mac_load", "title": f"Mac mini のメモリプレッシャー {mp}%",
                               "body": "ロード中のモデルを減らすと下がります", "tag": "mac:mem", "tab": "machines"})
            elif mp < th - 10:
                mem["mem_hot"] = False
        lim = m.get("cpu_speed_limit")
        if lim is not None:
            if lim < 100 and not mem.get("therm_hot"):
                mem["therm_hot"] = True
                if cooled("therm"):
                    ev.append({"kind": "mac_load", "title": f"Mac mini が熱で CPU を {lim}% に制限中",
                               "body": "", "tag": "mac:therm", "tab": "machines"})
            elif lim >= 100:
                mem["therm_hot"] = False
    return ev


# ------------------------------------------------------------------ 送信 ----

class Notifier:
    def __init__(self, cfg: dict | None, here: str):
        self.cfg = {**DEFAULT_NOTIFY, **(cfg or {})}
        self.cfg["events"] = {**DEFAULT_NOTIFY["events"], **((cfg or {}).get("events") or {})}
        self.enabled = AVAILABLE and bool(self.cfg.get("enabled", True))
        self.key_path = os.path.join(here, "vapid_private.pem")
        self.subs_path = os.path.join(here, "push_subscriptions.json")
        self.lock = threading.Lock()
        self.mem: dict = {}
        self.prev: dict | None = None
        self.public_key = None
        if not self.enabled:
            if not AVAILABLE and self.cfg.get("enabled", True):
                print("notify: pywebpush が無いので通知は無効です (pip install -r requirements-hub.txt)", file=sys.stderr)
            return
        self.vapid = self._load_or_create_key()
        pub = self.vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = _b64url(pub)

    def _load_or_create_key(self):
        if not os.path.exists(self.key_path):
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
            print(f"notify: VAPID 鍵を作りました ({self.key_path})", file=sys.stderr)
        return Vapid.from_file(self.key_path)

    # --- 購読 ---
    def _load_subs(self) -> list[dict]:
        try:
            with open(self.subs_path, "r", encoding="utf-8") as f:
                return [s for s in json.load(f) if isinstance(s, dict) and s.get("endpoint")]
        except (OSError, ValueError):
            return []

    def _save_subs(self, subs: list[dict]):
        tmp = self.subs_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(subs, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.subs_path)

    def subscribe(self, sub: dict, ua: str = "") -> bool:
        if not (isinstance(sub, dict) and str(sub.get("endpoint", "")).startswith("https://")
                and isinstance(sub.get("keys"), dict) and sub["keys"].get("p256dh") and sub["keys"].get("auth")):
            return False
        with self.lock:
            subs = [s for s in self._load_subs() if s["endpoint"] != sub["endpoint"]]
            subs.append({"endpoint": sub["endpoint"], "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]},
                         "ua": ua[:200], "created": time.time()})
            self._save_subs(subs)
        return True

    def unsubscribe(self, endpoint: str):
        with self.lock:
            subs = self._load_subs()
            left = [s for s in subs if s["endpoint"] != endpoint]
            if len(left) != len(subs):
                self._save_subs(left)

    def count(self) -> int:
        return len(self._load_subs()) if self.enabled else 0

    # --- 送信 ---
    def send(self, msg: dict, only: str | None = None) -> dict:
        """msg = {title, body, tag, tab}。only を渡すとその endpoint だけに送る"""
        if not self.enabled:
            return {"sent": 0, "failed": 0}
        payload = json.dumps({"title": msg.get("title", "番頭台"), "body": msg.get("body", ""),
                              "tag": msg.get("tag"), "url": "/?tab=" + (msg.get("tab") or "attn")}, ensure_ascii=False)
        claims = {"sub": self.cfg.get("contact") or DEFAULT_NOTIFY["contact"]}
        sent = failed = 0
        gone = []
        for s in self._load_subs():
            if only and s["endpoint"] != only:
                continue
            try:
                webpush(subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]}, data=payload,
                        vapid_private_key=self.vapid, vapid_claims=dict(claims), ttl=3600, timeout=10)
                sent += 1
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code in (404, 410):  # 購読が消えた (アプリ削除・通知オフ)
                    gone.append(s["endpoint"])
                else:
                    failed += 1
                    print(f"notify: 送信失敗 {code}: {str(e)[:200]}", file=sys.stderr)
            except Exception as e:
                failed += 1
                print(f"notify: 送信失敗: {type(e).__name__}: {e}", file=sys.stderr)
        for ep in gone:
            self.unsubscribe(ep)
        return {"sent": sent, "failed": failed, "removed": len(gone)}

    def on_state(self, state: dict):
        """hub の poll ごとに呼ぶ。初回は基準にするだけで鳴らさない"""
        if not self.enabled or state.get("demo"):
            return
        prev, self.prev = self.prev, state
        if prev is None or not self.count():
            return
        try:
            events = detect(prev, state, self.cfg, self.mem)
        except Exception as e:
            print(f"notify: 差分の検出に失敗: {e}", file=sys.stderr)
            return
        if len(events) > 4:  # 一度に多いときはまとめる
            events = [{"kind": "many", "title": f"番頭台: {len(events)} 件の出来事",
                       "body": "\n".join(e["title"] for e in events[:6]), "tag": "many", "tab": "attn"}]
        for e in events:
            threading.Thread(target=self.send, args=(e,), daemon=True).start()
