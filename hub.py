#!/usr/bin/env python3
"""開発ダッシュボード (ハブ)

Mac mini で動かし、LAN 内のブラウザから http://<mac-mini>:8080 で開く。
  - 各デバイスの agent.py から banto のタスク状態を集める (読み取りのみ)
  - Ollama (ローカル LLM) の稼働モデル・インストール済みモデル
  - Mac mini 自身の CPU / メモリ / GPU / ディスク
  - ゲーミング PC の死活と Wake-on-LAN
  - Open WebUI などのリンクと死活

  python3 hub.py                     # config.json を読む
  python3 hub.py --demo              # 架空データで画面だけ確認

標準ライブラリのみ。LAN (プライベート IP) 以外からのアクセスは拒否する。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent  # 同じフォルダの agent.py (自機の banto 収集に使う)
import notify  # Web Push 通知 (pywebpush が無ければ通知だけ無効)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

DEFAULT_CONFIG = {
    "listen": {"bind": "0.0.0.0", "port": 8080},
    "poll_interval": 5,
    "local_name": "Mac mini",
    # 手で足すデバイス (自動検出できないとき用。ふつうは空でよい)
    "devices": [],
    "discovery": {"enabled": True, "port": 8766, "forget_after_days": 14},
    "ollama": {"url": "http://127.0.0.1:11434"},
    # null なら、--role gaming で名乗った agent を自動で使う
    "gaming_pc": None,
    # url の {host} は、ブラウザで開いているダッシュボードのホスト名に置き換わる
    "links": [
        {"name": "Open WebUI", "url": "http://{host}:3000", "health": "http://127.0.0.1:3000/health", "icon": "openwebui"},
        {"name": "Ollama API", "url": "http://{host}:11434", "health": "http://127.0.0.1:11434/api/version", "icon": "ollama"},
    ],
    # スマホへの Web Push 通知。項目は notify.py の DEFAULT_NOTIFY を参照
    "notify": {},
}


# ------------------------------------------------------------ discovery ----

def arp_mac(ip: str) -> str | None:
    """ARP テーブルから、その IP の実際の送信元 NIC の MAC を引く (WoL に使う)"""
    out = run(["arp", "-n", ip]) if os.name != "nt" else run(["arp", "-a", ip])
    m = re.search(r"([0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}", out)
    if not m:
        return None
    parts = re.split(r"[:-]", m.group(0))
    return ":".join(p.zfill(2).upper() for p in parts)


class Discovery:
    """agent のブロードキャストを受けて、IP・MAC を覚える (known_devices.json に保存)"""

    def __init__(self, cfg: dict):
        self.port = int(cfg.get("port", 8766))
        self.forget = float(cfg.get("forget_after_days", 14)) * 86400
        self.path = os.path.join(HERE, "known_devices.json")
        self.lock = threading.Lock()
        self.known: dict[str, dict] = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.known = json.load(f)
        except Exception:
            pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.known, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def start(self):
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", self.port))
        except OSError as e:
            print(f"discovery: UDP {self.port} を開けません: {e}", file=sys.stderr)
            return
        while True:
            try:
                data, (ip, _) = s.recvfrom(2048)
                if not agent.is_lan(ip):
                    continue
                msg = json.loads(data.decode("utf-8"))
                if msg.get("type") != "banto-agent" or not msg.get("id"):
                    continue
                self._seen(msg, ip)
            except Exception:
                continue

    def _seen(self, msg: dict, ip: str):
        did = str(msg["id"])
        with self.lock:
            prev = self.known.get(did, {})
            changed = prev.get("ip") != ip or prev.get("name") != msg.get("name") or prev.get("role") != msg.get("role")
            mac = prev.get("mac") if not changed else None
            mac = mac or arp_mac(ip) or msg.get("mac") or prev.get("mac")
            self.known[did] = {"id": did, "name": msg.get("name") or did, "role": msg.get("role") or "",
                               "ip": ip, "port": int(msg.get("port") or 8765), "mac": mac, "last_seen": time.time()}
            if changed or mac != prev.get("mac") or time.time() - prev.get("last_saved", 0) > 600:
                self.known[did]["last_saved"] = time.time()
                try:
                    self.save()
                except OSError:
                    pass

    def devices(self) -> list[dict]:
        now = time.time()
        with self.lock:
            return [dict(d) for d in self.known.values()
                    if d.get("role") == "gaming" or now - d.get("last_seen", 0) < self.forget]


# ---------------------------------------------------------------- utils ----

def http_json(url: str, timeout: float = 2.5):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    # LAN 内宛てなのでプロキシを通さない
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_ok(url: str, timeout: float = 2.0) -> tuple[bool, float | None]:
    t0 = time.time()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(url), timeout=timeout) as r:
            return (200 <= r.status < 400), (time.time() - t0) * 1000
    except Exception:
        return False, None


def tcp_ok(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ping_ok(host: str) -> bool:
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", "800", host]
    elif sys.platform == "darwin":
        cmd = ["ping", "-c", "1", "-t", "1", host]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", host]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=3).returncode == 0
    except Exception:
        return False


def run(cmd: list[str], timeout: float = 3) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


# ------------------------------------------------------------ collectors ----

def collect_device(dev: dict) -> dict:
    out = {"id": dev["id"], "name": dev.get("name", dev["id"]), "online": False}
    try:
        if dev.get("agent") == "local":
            snap = agent.snapshot()
        else:
            snap = http_json(dev["agent"].rstrip("/") + "/api/snapshot")
        out.update(online=True, snapshot=snap)
    except Exception as e:
        out["error"] = type(e).__name__
    return out


def collect_mac() -> dict | None:
    """Mac mini 自身のスペックと負荷 (macOS のみ。sudo 不要の範囲)"""
    if sys.platform != "darwin":
        return None
    info: dict = {}
    sc = lambda k: run(["sysctl", "-n", k]).strip()
    info["model"] = sc("hw.model")
    info["chip"] = sc("machdep.cpu.brand_string")
    try:
        info["cores"] = {"total": int(sc("hw.ncpu")),
                         "perf": int(sc("hw.perflevel0.physicalcpu") or 0),
                         "eff": int(sc("hw.perflevel1.physicalcpu") or 0)}
    except ValueError:
        pass
    try:
        info["mem_total"] = int(sc("hw.memsize"))
    except ValueError:
        pass
    info["os"] = run(["sw_vers", "-productVersion"]).strip()

    # メモリ: vm_stat (アクティブ + wired + 圧縮 を使用中とみなす。アクティビティモニタの「使用済み」に近い)
    vm = run(["vm_stat"])
    m = re.search(r"page size of (\d+)", vm)
    if m and info.get("mem_total"):
        ps = int(m.group(1))

        def pages(label):
            mm = re.search(label + r":\s+(\d+)", vm)
            return int(mm.group(1)) if mm else 0
        used = (pages("Anonymous pages") - pages("Pages purgeable") + pages("Pages wired down")
                + pages("Pages occupied by compressor")) * ps
        if used <= 0:
            used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * ps
        info["mem_used"] = max(0, min(used, info["mem_total"]))
    lvl = sc("kern.memorystatus_level")  # 空きの割合 (%)
    if lvl.isdigit():
        info["mem_pressure"] = 100 - int(lvl)

    # CPU: 全プロセスの %CPU 合計 / コア数
    try:
        tot = sum(float(x) for x in run(["ps", "-A", "-o", "%cpu="]).split())
        info["cpu"] = min(100.0, tot / max(1, info.get("cores", {}).get("total", 1)))
    except ValueError:
        pass
    info["load"] = list(os.getloadavg())

    # GPU: IOAccelerator の PerformanceStatistics (Apple Silicon、sudo 不要)
    io = run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"])
    m = re.search(r'"Device Utilization %"=(\d+)', io)
    if m:
        info["gpu"] = int(m.group(1))
    m = re.search(r'"In use system memory"=(\d+)', io)
    if m:
        info["gpu_mem"] = int(m.group(1))

    # 温度は sudo なしでは取れないので省略。熱の状態だけ見る
    therm = run(["pmset", "-g", "therm"])
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", therm)
    if m:
        info["cpu_speed_limit"] = int(m.group(1))

    info["uptime"] = agent.uptime_seconds()
    try:
        import shutil
        du = shutil.disk_usage("/")
        info["disk"] = {"used": du.used, "total": du.total}
    except Exception:
        pass
    return info


def collect_ollama(cfg: dict | None) -> dict | None:
    if not cfg:
        return None
    base = cfg.get("url", "http://127.0.0.1:11434").rstrip("/")
    out = {"url": base, "online": False}
    try:
        out["version"] = http_json(base + "/api/version").get("version")
        out["online"] = True
        out["running"] = [{
            "name": m.get("name"),
            "size": m.get("size"),
            "size_vram": m.get("size_vram"),
            "expires_at": m.get("expires_at"),
            "params": (m.get("details") or {}).get("parameter_size"),
            "quant": (m.get("details") or {}).get("quantization_level"),
            "context": m.get("context_length"),
        } for m in http_json(base + "/api/ps").get("models", [])]
        out["installed"] = sorted([{
            "name": m.get("name"),
            "size": m.get("size"),
            "params": (m.get("details") or {}).get("parameter_size"),
            "quant": (m.get("details") or {}).get("quantization_level"),
            "modified_at": m.get("modified_at"),
        } for m in http_json(base + "/api/tags").get("models", [])], key=lambda m: m.get("modified_at") or "", reverse=True)
    except Exception as e:
        out["error"] = type(e).__name__
    return out


class Gaming:
    """ゲーミング PC の死活と WoL"""

    def __init__(self):
        self.wol_sent_at = 0.0
        self.last_online_change = time.time()
        self.prev_online = None

    def status(self, cfg: dict | None, devices: list[dict]) -> dict | None:
        if not cfg:
            return None
        host = cfg.get("host")
        out = {"name": cfg.get("name", "Gaming PC"), "host": host, "device_id": cfg.get("device_id"), "online": False, "via": None,
               "wol": bool(cfg.get("mac"))}
        dev = next((d for d in devices if d["id"] == cfg.get("device_id")), None)
        if dev and dev.get("online"):
            out.update(online=True, via="agent", system=dev["snapshot"].get("system"))
        elif host:
            for p in cfg.get("probe_ports", [3389, 22, 445]):
                if tcp_ok(host, int(p)):
                    out.update(online=True, via=f"tcp:{p}")
                    break
            if not out["online"] and cfg.get("ping", True) and ping_ok(host):
                out.update(online=True, via="ping")
        if self.prev_online is not None and out["online"] != self.prev_online:
            self.last_online_change = time.time()
        self.prev_online = out["online"]
        out["since"] = self.last_online_change
        if out["online"]:
            self.wol_sent_at = 0.0
        out["waking"] = (not out["online"]) and (time.time() - self.wol_sent_at < 180)
        out["wol_sent_at"] = self.wol_sent_at or None
        return out

    def wake(self, cfg: dict) -> dict:
        if time.time() - self.wol_sent_at < 10:
            return {"ok": False, "error": "送信したばかりです"}
        mac = re.sub(r"[^0-9A-Fa-f]", "", cfg.get("mac", ""))
        if len(mac) != 12:
            return {"ok": False, "error": "MAC アドレスがまだ分かりません"}
        packet = b"\xff" * 6 + bytes.fromhex(mac) * 16
        targets = {cfg.get("broadcast") or "255.255.255.255", "255.255.255.255"}
        # macOS のローカルネットワーク許可: 初回の送信は許可ダイアログを出すだけで
        # EHOSTUNREACH (Errno 65) になる。許可されるまで少し待って送り直す
        deadline = time.time() + 15
        while True:
            sent, last_err = 0, None
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                for t in targets:
                    for port in (9, 7):
                        try:
                            s.sendto(packet, (t, port))
                            sent += 1
                        except OSError as e:
                            last_err = e
            if sent or time.time() >= deadline:
                break
            time.sleep(0.5)
        if not sent:
            print(f"wol: 送信できません: {last_err}", file=sys.stderr)
            return {"ok": False, "error": "送信できません。macOS の「ローカルネットワーク」で Python を許可してください"}
        self.wol_sent_at = time.time()
        return {"ok": True}


def collect_links(links: list[dict]) -> list[dict]:
    out = []
    for l in links:
        ok, ms = (http_ok(l["health"]) if l.get("health") else (None, None))
        out.append({**l, "up": ok, "latency_ms": ms})
    return out


# ------------------------------------------------------------------ demo ----

def demo_state() -> dict:
    now = time.time()
    r = random.Random(int(now // 5))

    def sess(i, agent_, status, name, unseen=False, req=None, model=None):
        return {"id": f"s{i}", "name": name, "agent": agent_, "model": model, "effort": None,
                "status": status, "unseen": unseen, "request": req}

    def task(i, title, lane, prio, genre="task", sessions=(), review=False, cl=None, check=None, comment=None):
        return {"id": str(i), "title": title, "comment": comment, "genre": genre, "lane": lane, "priority": prio,
                "review": review, "root": False, "branch": f"banto/task-{i}", "after": [],
                "checklist": cl, "check": check, "last_report": None, "sessions": list(sessions)}

    mac_proj = {"name": "banto.nvim", "root": "~/Development/Repositry/banto.nvim", "board_open": True, "updated_at": now - 40,
                "tasks": [
                    task(12, "Attention Bar の経過時間表示を秒単位に", "active", "high",
                         sessions=[sess(1, "claude", "waiting", "elapsed-seconds", req="tests/broadcast.lua を書き換えてよいですか?", model="opus")],
                         cl={"done": 2, "total": 5}, comment="表示幅に注意"),
                    task(14, "MCP report の全文表示", "active", "medium",
                         sessions=[sess(2, "codex", "running", "codex-2")], cl={"done": 4, "total": 6}),
                    task(15, "worktree 衝突の検出を強化", "active", "critical", genre="spike",
                         sessions=[sess(3, "claude", "idle", "conflict-scan", unseen=True)], review=True,
                         check={"status": "failed", "duration": 3.2, "summary": "exit 2"}),
                    task(16, "README の英訳", "backlog", "low"),
                    task(17, "PR #41 のレビュー", "backlog", "medium", genre="review"),
                    task(9, "opencode の権限確認パターン", "hold", "medium"),
                    task(8, "Muse Code 連携", "done", "high", check={"status": "ok", "duration": 12, "summary": None}),
                ]}
    mb_proj = {"name": "kk-stack-dev-site", "root": "~/src/kk-stack-dev-site", "board_open": True, "updated_at": now - 300,
               "tasks": [
                   task(3, "料金ページのレスポンシブ修正", "active", "high",
                        sessions=[sess(4, "claude", "running", "pricing-responsive", model="sonnet")], cl={"done": 1, "total": 3}),
                   task(4, "OGP 画像の自動生成", "active", "medium", sessions=[sess(5, "cursor", "waiting", "cursor-1", req="Run this command? npm i -D satori")]),
                   task(5, "問い合わせフォームのスパム対策", "backlog", "high", genre="spike"),
               ]}
    gpc_proj = {"name": "aaos-sample-launcher", "root": "C:/src/aaos-sample-launcher", "board_open": False, "updated_at": now - 86400,
                "tasks": [task(1, "CarLauncher のフォーカス制御", "hold", "medium", sessions=[sess(6, "claude", "idle", "focus-ctrl")])]}

    def snap(host, osname, projects, gpus=None):
        return {"agent_version": agent.AGENT_VERSION, "time": now, "agent_uptime": 3600,
                "system": {"hostname": host, "os": osname, "arch": "arm64", "cpus": 10, "load": [1.2, 1.1, 0.9],
                           "uptime": 86400 * 3.2, "disk": {"used": 310e9, "total": 494e9}, "gpus": gpus or []},
                "banto": {"data_dirs": ["~/.local/share/nvim/banto"], "projects": projects}}

    gaming_on = int(now // 30) % 2 == 0
    devices = [
        {"id": "macmini", "name": "Mac mini", "online": True, "snapshot": snap("kazusanomac-mini", "Darwin 25.1", [mac_proj])},
        {"id": "macbook", "name": "MacBook Pro", "online": True, "snapshot": snap("kk-mbp", "Darwin 25.1", [mb_proj])},
        {"id": "gaming", "name": "Gaming PC", "online": gaming_on,
         **({"snapshot": snap("GAMING-PC", "Windows 11", [gpc_proj],
                              [{"name": "NVIDIA GeForce RTX 4080", "util": r.randint(2, 30), "temp": r.randint(38, 55),
                                "mem_used": 2100, "mem_total": 16376, "power": 42.0}])} if gaming_on else {"error": "URLError"})},
    ]
    gb = 1024 ** 3
    return {
        "demo": True,
        "time": now,
        "devices": devices,
        "mac": {"model": "Mac16,11", "chip": "Apple M4 Pro", "cores": {"total": 14, "perf": 10, "eff": 4},
                "mem_total": 64 * gb, "mem_used": int((38 + r.random() * 4) * gb), "mem_pressure": r.randint(28, 40),
                "cpu": r.uniform(8, 35), "gpu": r.randint(0, 70), "gpu_mem": int(19.5 * gb), "load": [3.1, 2.8, 2.5],
                "os": "26.1", "uptime": 86400 * 6.4, "disk": {"used": 612e9, "total": 994e9}, "cpu_speed_limit": 100},
        "ollama": {"url": "http://127.0.0.1:11434", "online": True, "version": "0.13.2",
                   "running": [
                       {"name": "qwen3-coder:30b", "size": 19.8 * gb, "size_vram": 19.8 * gb,
                        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now + 240)),
                        "params": "30.5B", "quant": "Q4_K_M", "context": 32768},
                       {"name": "nomic-embed-text:latest", "size": 0.6 * gb, "size_vram": 0.6 * gb,
                        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now + 50)),
                        "params": "137M", "quant": "F16", "context": 2048}],
                   "installed": [{"name": n, "size": s * gb, "params": p, "quant": q, "modified_at": ""} for n, s, p, q in [
                       ("qwen3-coder:30b", 18.6, "30.5B", "Q4_K_M"), ("gpt-oss:20b", 13.8, "20.9B", "MXFP4"),
                       ("gemma3:27b", 17.4, "27.4B", "Q4_K_M"), ("llama3.2:3b", 2.0, "3.2B", "Q4_K_M"),
                       ("nomic-embed-text:latest", 0.27, "137M", "F16")]]},
        "gaming": {"name": "Gaming PC", "host": "192.168.1.30", "device_id": "gaming", "online": gaming_on, "via": "agent" if gaming_on else None,
                   "wol": True, "since": now - (now % 30), "waking": False, "wol_sent_at": None,
                   "system": devices[2].get("snapshot", {}).get("system")},
        "links": [
            {"name": "Open WebUI", "url": "http://macmini.local:3000", "icon": "openwebui", "up": True, "latency_ms": 12.4},
            {"name": "Ollama API", "url": "http://macmini.local:11434", "icon": "ollama", "up": True, "latency_ms": 3.1},
            {"name": "Router", "url": "http://192.168.1.1", "icon": "router", "up": True, "latency_ms": 5.0},
        ],
    }


# ------------------------------------------------------------------- hub ----

class Hub:
    def __init__(self, cfg: dict, demo: bool):
        self.cfg = cfg
        self.demo = demo
        self.gaming = Gaming()
        self.state: dict = {}
        self.lock = threading.Lock()
        self.pool = cf.ThreadPoolExecutor(max_workers=8)
        self.discovery = None
        self.notifier = notify.Notifier(cfg.get("notify"), HERE)
        if not demo and cfg.get("discovery", {}).get("enabled", True):
            self.discovery = Discovery(cfg.get("discovery", {}))
            self.discovery.start()

    def device_list(self) -> list[dict]:
        """自機 + 手書きの devices + 自動検出したもの (同じ ID は手書きを優先)"""
        me = socket.gethostname()
        out = [{"id": me, "name": self.cfg.get("local_name") or me, "agent": "local"}]
        ids = {me, me.split(".")[0]}
        for d in self.cfg.get("devices", []):
            if d.get("agent") == "local":
                out[0].update(name=d.get("name", out[0]["name"]))
                continue
            out.append(d)
            ids.add(d["id"])
        for d in self.discovery.devices() if self.discovery else []:
            if d["id"] in ids or d["id"].split(".")[0] in ids:
                continue
            out.append({"id": d["id"], "name": d["name"], "role": d.get("role"),
                        "agent": f"http://{d['ip']}:{d['port']}", "ip": d["ip"], "mac": d.get("mac")})
        return out

    def gaming_cfg(self, devices: list[dict]) -> dict | None:
        """自動検出した --role gaming の agent に、config.json の gaming_pc を上書きで重ねる
        (例: {"gaming_pc": {"mac": "34:5A:60:EA:E8:D2"}} だけ書けば、IP は自動・MAC は固定)"""
        g = self.cfg.get("gaming_pc") or {}
        d = next((d for d in devices if d.get("role") == "gaming"), None)
        auto = {}
        if d:
            ip = d.get("ip", "")
            auto = {"name": d["name"], "host": ip, "mac": d.get("mac"), "device_id": d["id"],
                    "broadcast": ".".join(ip.split(".")[:3] + ["255"]) if ip.count(".") == 3 else None,
                    "probe_ports": [3389, 445], "ping": True}
        merged = {**auto, **{k: v for k, v in g.items() if v not in (None, "")}}
        if not merged:
            return None
        merged.setdefault("name", "Gaming PC")
        return merged

    def poll_once(self):
        if self.demo:
            st = demo_state()
            if self.gaming.wol_sent_at and not st["gaming"]["online"]:
                st["gaming"]["waking"] = time.time() - self.gaming.wol_sent_at < 180
            with self.lock:
                self.state = st
            return
        devs = self.device_list()
        f_devs = [self.pool.submit(collect_device, d) for d in devs]
        f_mac = self.pool.submit(collect_mac)
        f_oll = self.pool.submit(collect_ollama, self.cfg.get("ollama"))
        f_links = self.pool.submit(collect_links, self.cfg.get("links", []))
        devices = [f.result() for f in f_devs]
        self.current_gaming = self.gaming_cfg(devs)
        gaming = self.gaming.status(self.current_gaming, devices)
        st = {"demo": False, "time": time.time(), "devices": devices, "mac": f_mac.result(),
              "ollama": f_oll.result(), "gaming": gaming, "links": f_links.result()}
        with self.lock:
            self.state = st
        self.notifier.on_state(st)

    def loop(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:  # 収集の失敗で止めない
                print("poll error:", e, file=sys.stderr)
            time.sleep(max(2, int(self.cfg.get("poll_interval", 5))))

    def get(self) -> dict:
        with self.lock:
            return self.state


def make_handler(hub: Hub):
    class H(BaseHTTPRequestHandler):
        server_version = "dev-dashboard/1.0"

        def log_message(self, fmt, *args):
            pass

        def _json(self, code, body):
            raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _guard(self) -> bool:
            if not agent.is_lan(self.client_address[0]):
                self._json(403, {"error": "LAN only"})
                return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            path = self.path.split("?")[0]
            if path == "/api/state":
                return self._json(200, hub.get())
            if path == "/api/push/key":
                n = hub.notifier
                return self._json(200, {"enabled": n.enabled, "publicKey": n.public_key, "subscribers": n.count()})
            if path == "/sw.js":  # Service Worker はルートから配信しないとスコープが / にならない
                with open(os.path.join(STATIC, "sw.js"), "rb") as f:
                    raw = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    raw = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path.startswith("/static/"):
                name = os.path.basename(path)
                fp = os.path.join(STATIC, name)
                types = {".json": "application/manifest+json", ".svg": "image/svg+xml", ".png": "image/png"}
                ext = os.path.splitext(name)[1]
                if ext in types and os.path.isfile(fp):
                    with open(fp, "rb") as f:
                        raw = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", types[ext])
                    self.send_header("Cache-Control", "max-age=86400")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard():
                return
            # 独自ヘッダー必須 (他サイトからのフォーム送信・CSRF を防ぐ。ブラウザはプリフライトなしに付けられない)
            if self.headers.get("X-Dashboard") != "1":
                return self._json(403, {"error": "missing header"})
            if self.path == "/api/wol":
                if hub.demo:
                    hub.gaming.wol_sent_at = time.time()
                    return self._json(200, {"ok": True, "demo": True})
                g = getattr(hub, "current_gaming", None)
                if not g:
                    return self._json(400, {"ok": False, "error": "Gaming PC が見つかりません (agent を --role gaming で一度起動してください)"})
                res = hub.gaming.wake(g)
                threading.Thread(target=hub.poll_once, daemon=True).start()
                return self._json(200 if res["ok"] else 429, res)
            if self.path.startswith("/api/push/"):
                n = hub.notifier
                if not n.enabled:
                    return self._json(503, {"ok": False, "error": "通知は無効です (hub に pywebpush を入れてください)"})
                body = self._body()
                if body is None:
                    return self._json(400, {"ok": False, "error": "bad json"})
                sub = body.get("subscription") or {}
                if self.path == "/api/push/subscribe":
                    ok = n.subscribe(sub, self.headers.get("User-Agent", ""))
                    return self._json(200 if ok else 400, {"ok": ok})
                if self.path == "/api/push/unsubscribe":
                    n.unsubscribe(str(sub.get("endpoint", "")))
                    return self._json(200, {"ok": True})
                if self.path == "/api/push/test":
                    res = n.send({"title": "番頭台", "body": "テスト通知です。これが見えていれば設定は完了です", "tag": "test"},
                                 only=str(sub.get("endpoint", "")) or None)
                    return self._json(200, {"ok": res["sent"] > 0, **res})
            self._json(404, {"error": "not found"})

        def _body(self) -> dict | None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 64 * 1024:
                    return None
                data = json.loads(self.rfile.read(n) or b"{}")
                return data if isinstance(data, dict) else None
            except ValueError:
                return None

    return H


def load_config(path: str) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def main():
    ap = argparse.ArgumentParser(description="開発ダッシュボード (ハブ)")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--demo", action="store_true", help="架空データで表示する")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    cfg = load_config(a.config)
    hub = Hub(cfg, a.demo)
    hub.poll_once()
    threading.Thread(target=hub.loop, daemon=True).start()
    bind = cfg["listen"].get("bind", "0.0.0.0")
    port = a.port or int(cfg["listen"].get("port", 8080))
    srv = ThreadingHTTPServer((bind, port), make_handler(hub))
    print(f"dev-dashboard on http://{bind}:{port}  ({'DEMO' if a.demo else a.config})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
