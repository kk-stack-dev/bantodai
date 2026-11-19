#!/usr/bin/env python3
"""banto 収集エージェント (読み取り専用)

各デバイスで動かし、banto.nvim の保存ファイル (stdpath("data")/banto/*.json) を
読み取って JSON で返す。書き込み・操作の API は持たない。
LAN (プライベート IP) 以外からのアクセスは 403 で拒否する。

  python3 agent.py                 # 0.0.0.0:8765 で待ち受け
  python3 agent.py --port 8765 --bind 0.0.0.0
  BANTO_DATA_DIR=/path/to/banto python3 agent.py

標準ライブラリのみ。macOS / Linux / Windows で動く。
"""
from __future__ import annotations

import argparse
import glob
import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_VERSION = "1.0.0"
STARTED_AT = time.time()


# ---------------------------------------------------------------- banto ----

def banto_data_dirs() -> list[str]:
    """banto の保存先候補 (stdpath("data")/banto)。persist.dir を変えている場合は BANTO_DATA_DIR で指定"""
    env = os.environ.get("BANTO_DATA_DIR")
    if env:
        return [os.path.expanduser(env)]
    home = os.path.expanduser("~")
    cands = []
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        cands.append(os.path.join(xdg, "nvim", "banto"))
    cands.append(os.path.join(home, ".local", "share", "nvim", "banto"))
    lad = os.environ.get("LOCALAPPDATA")
    if lad:
        cands.append(os.path.join(lad, "nvim-data", "banto"))
    seen, out = set(), []
    for c in cands:
        if c not in seen and os.path.isdir(c):
            seen.add(c)
            out.append(c)
    return out


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True, timeout=3)
            return str(pid) in r.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _project_name(file: str) -> str:
    # <ディレクトリ名>-<sha256 先頭16桁>.json
    base = os.path.basename(file)[:-5]
    if len(base) > 17 and base[-17] == "-":
        return base[:-17]
    return base


def _repo_root(task: dict) -> str | None:
    wt = task.get("worktree")
    if isinstance(wt, dict):
        return wt.get("root")
    return None


def read_project(file: str) -> dict | None:
    try:
        with open(file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # 書き込み中などは次回に回す
        return {"file": os.path.basename(file), "name": _project_name(file), "error": str(e), "tasks": []}
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return None

    lock = file + ".lock"
    board_open = False
    if os.path.exists(lock):
        try:
            with open(lock, "r", encoding="utf-8") as f:
                board_open = pid_alive(int((f.readline() or "0").strip() or 0))
        except Exception:
            board_open = False

    tasks, root = [], None
    for t in data["tasks"]:
        if not isinstance(t, dict):
            continue
        root = root or _repo_root(t)
        sessions = []
        for s in t.get("sessions") or []:
            if not isinstance(s, dict):
                continue
            # 保存時点の状態。ボードが閉じていれば動いていないので idle 扱いに寄せる
            status = s.get("status") or "idle"
            if not board_open and status in ("running", "waiting"):
                status = "idle"
            sessions.append({
                "id": s.get("id"),
                "name": s.get("ai_title") or s.get("name") or s.get("agent"),
                "agent": s.get("agent"),
                "model": s.get("model"),
                "effort": s.get("effort"),
                "status": status,
                "unseen": isinstance(s.get("unseen"), (int, float)),
                "request": s.get("last_request"),
            })
        checklist = [c for c in (t.get("checklist") or []) if isinstance(c, dict)]
        check = t.get("check") if isinstance(t.get("check"), dict) else None
        reports = t.get("reports") or []
        last_report = reports[-1] if reports and isinstance(reports[-1], dict) else None
        tasks.append({
            "id": t.get("id"),
            "title": t.get("title") or "(無題)",
            "comment": t.get("comment"),
            "genre": t.get("genre") or "task",
            "lane": t.get("lane") or "backlog",
            "priority": t.get("priority") or "medium",
            "review": bool(t.get("review")),
            "root": bool(t.get("root")),
            "branch": (t.get("worktree") or {}).get("branch") if isinstance(t.get("worktree"), dict) else None,
            "checklist": {"done": sum(1 for c in checklist if c.get("done")), "total": len(checklist)} if checklist else None,
            "check": {"status": check.get("status"), "duration": check.get("duration"), "summary": check.get("summary")} if check else None,
            "last_report": {"at": last_report.get("at"), "session": last_report.get("session"),
                            "text": (last_report.get("text") or "").splitlines()[0][:200] if last_report.get("text") else ""} if last_report else None,
            "after": t.get("after") or [],
            "sessions": sessions,
        })

    st = os.stat(file)
    return {
        "file": os.path.basename(file),
        "name": os.path.basename(root) if root else _project_name(file),
        "root": root,
        "board_open": board_open,
        "updated_at": st.st_mtime,
        "tasks": tasks,
    }


def collect_banto() -> dict:
    dirs = banto_data_dirs()
    projects = []
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "*.json"))):
            name = os.path.basename(f)
            if name.endswith(".archive.json") or name == "agent_choices.json" or name.endswith(".tmp"):
                continue
            p = read_project(f)
            if p:
                projects.append(p)
    projects.sort(key=lambda p: p.get("updated_at") or 0, reverse=True)
    return {"data_dirs": dirs, "projects": projects}


# --------------------------------------------------------------- system ----

def _run(cmd: list[str], timeout: float = 3) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


class _WinGpu:
    """Windows の GPU 情報を標準 API だけで取る (AMD / Intel など nvidia-smi が無い GPU 向け)

    - アダプタ名・VRAM 総量: DXGI (IDXGIFactory1::EnumAdapters1 → GetDesc1)
    - 使用率: パフォーマンスカウンター「GPU Engine / Utilization Percentage」
      (タスクマネージャーと同じく、エンジンごとに全プロセスを合計し、その最大値)
    - VRAM 使用量: 「GPU Adapter Memory / Dedicated Usage」
    温度と消費電力は標準 API に無いため None を返す。
    カウンターは 2 回の計測の差で出るので、別スレッドで 5 秒ごとに取って結果を持っておく。
    """

    def __init__(self, interval: float = 5):
        self.interval = interval
        self.result: list[dict] = []
        self.adapters = self._adapters()
        if self.adapters:
            import threading
            self._update()  # 初回だけ同期で取る (--once でも値が出るように)
            threading.Thread(target=self._loop, daemon=True).start()

    @staticmethod
    def _adapters() -> list[dict]:
        try:
            import ctypes
            from ctypes import wintypes as w

            class LUID(ctypes.Structure):
                _fields_ = [("Low", w.DWORD), ("High", w.LONG)]

            class DESC1(ctypes.Structure):
                _fields_ = [("Description", w.WCHAR * 128), ("VendorId", w.UINT), ("DeviceId", w.UINT),
                            ("SubSysId", w.UINT), ("Revision", w.UINT),
                            ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                            ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", LUID), ("Flags", w.UINT)]

            class GUID(ctypes.Structure):
                _fields_ = [("a", w.DWORD), ("b", w.WORD), ("c", w.WORD), ("d", ctypes.c_ubyte * 8)]

            def vcall(obj, idx, restype, *argtypes):
                vt = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vt[idx])

            iid = GUID(0x770aae78, 0xf26f, 0x4dba, (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
            fac = ctypes.c_void_p()
            if ctypes.WinDLL("dxgi").CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(fac)) != 0:
                return []
            out = []
            i = 0
            while True:
                ad = ctypes.c_void_p()
                if vcall(fac, 12, ctypes.c_long, w.UINT, ctypes.POINTER(ctypes.c_void_p))(fac, i, ctypes.byref(ad)) != 0:
                    break  # DXGI_ERROR_NOT_FOUND
                d = DESC1()
                vcall(ad, 10, ctypes.c_long, ctypes.POINTER(DESC1))(ad, ctypes.byref(d))
                vcall(ad, 2, w.ULONG)(ad)  # Release
                i += 1
                if d.Flags & 2:  # DXGI_ADAPTER_FLAG_SOFTWARE (Microsoft Basic Render Driver)
                    continue
                out.append({"name": d.Description, "mem_total": d.DedicatedVideoMemory,
                            "luid": f"0x{d.AdapterLuid.High & 0xffffffff:08x}_0x{d.AdapterLuid.Low:08x}"})
            vcall(fac, 2, w.ULONG)(fac)
            # VRAM の多い順 (dGPU を先頭にする。画面は先頭の 1 枚だけを出す)
            out.sort(key=lambda a: a["mem_total"], reverse=True)
            return out
        except Exception:
            return []

    @staticmethod
    def _sample() -> tuple[dict, dict]:
        """(LUID ごとの使用率 %, LUID ごとの VRAM 使用バイト) を返す"""
        import ctypes
        from ctypes import wintypes as w

        class FMT(ctypes.Structure):
            _fields_ = [("CStatus", w.DWORD), ("doubleValue", ctypes.c_double)]

        class ITEM(ctypes.Structure):
            _fields_ = [("szName", w.LPWSTR), ("FmtValue", FMT)]

        pdh = ctypes.WinDLL("pdh")
        PDH_FMT_DOUBLE = 0x200

        def items(counter) -> list[tuple[str, float]]:
            size, n = w.DWORD(0), w.DWORD(0)
            pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(n), None)
            if not size.value:
                return []
            buf = (ctypes.c_byte * size.value)()
            if pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(n), buf) != 0:
                return []
            arr = ctypes.cast(buf, ctypes.POINTER(ITEM))
            return [(arr[k].szName, arr[k].FmtValue.doubleValue) for k in range(n.value)]

        # プロセスの増減でインスタンスが変わるので、毎回クエリを作り直す
        q = ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
            return {}, {}
        try:
            c_eng, c_mem = ctypes.c_void_p(), ctypes.c_void_p()
            pdh.PdhAddEnglishCounterW(q, "\\GPU Engine(*)\\Utilization Percentage", 0, ctypes.byref(c_eng))
            pdh.PdhAddEnglishCounterW(q, "\\GPU Adapter Memory(*)\\Dedicated Usage", 0, ctypes.byref(c_mem))
            pdh.PdhCollectQueryData(q)
            time.sleep(1)
            pdh.PdhCollectQueryData(q)
            # 例: pid_1234_luid_0x00000000_0x37885868_phys_0_eng_0_engtype_3D
            per_engine: dict[tuple[str, str], float] = {}
            for name, v in items(c_eng):
                try:
                    luid = name.lower().split("luid_")[1].split("_phys")[0]
                    eng = name.lower().split("_eng_")[1].split("_")[0]
                except IndexError:
                    continue
                per_engine[(luid, eng)] = per_engine.get((luid, eng), 0.0) + v
            util: dict[str, float] = {}
            for (luid, _), v in per_engine.items():
                util[luid] = max(util.get(luid, 0.0), min(v, 100.0))
            mem = {}
            for name, v in items(c_mem):
                try:
                    mem[name.lower().split("luid_")[1].split("_phys")[0]] = v
                except IndexError:
                    continue
            return util, mem
        finally:
            pdh.PdhCloseQuery(q)

    def _update(self):
        try:
            util, mem = self._sample()
            mib = 1024 * 1024
            self.result = [{
                "name": a["name"],
                "util": round(util[a["luid"]], 1) if a["luid"] in util else None,
                "temp": None,
                "mem_used": round(mem[a["luid"]] / mib) if a["luid"] in mem else None,
                "mem_total": round(a["mem_total"] / mib),
                "power": None,
            } for a in self.adapters]
        except Exception:
            pass

    def _loop(self):
        while True:
            time.sleep(max(self.interval - 1, 1))
            self._update()


_win_gpu: _WinGpu | None = None


def collect_gpu() -> list[dict]:
    """GPU の情報。NVIDIA は nvidia-smi、それ以外の Windows は DXGI とパフォーマンスカウンターで取る"""
    global _win_gpu
    if not shutil.which("nvidia-smi"):
        if os.name != "nt":
            return []
        if _win_gpu is None:
            _win_gpu = _WinGpu()
        return _win_gpu.result
    out = _run(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw",
                "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None
        gpus.append({"name": p[0], "util": num(p[1]), "temp": num(p[2]),
                     "mem_used": num(p[3]), "mem_total": num(p[4]), "power": num(p[5])})
    return gpus


def uptime_seconds() -> float | None:
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/uptime") as f:
                return float(f.read().split()[0])
        if sys.platform == "darwin":
            out = _run(["sysctl", "-n", "kern.boottime"])  # { sec = 1700000000, usec = 0 } ...
            sec = int(out.split("sec =")[1].split(",")[0])
            return time.time() - sec
        if os.name == "nt":
            import ctypes
            return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:
        return None
    return None


def collect_system() -> dict:
    disk = None
    try:
        du = shutil.disk_usage(os.path.expanduser("~"))
        disk = {"used": du.used, "total": du.total}
    except Exception:
        pass
    load = None
    if hasattr(os, "getloadavg"):
        try:
            load = list(os.getloadavg())
        except OSError:
            pass
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpus": os.cpu_count(),
        "load": load,
        "uptime": uptime_seconds(),
        "disk": disk,
        "gpus": collect_gpu(),
    }


def snapshot() -> dict:
    return {
        "agent_version": AGENT_VERSION,
        "time": time.time(),
        "agent_uptime": time.time() - STARTED_AT,
        "system": collect_system(),
        "banto": collect_banto(),
    }


# ----------------------------------------------------------------- http ----

def is_lan(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "banto-agent/" + AGENT_VERSION

    def log_message(self, fmt, *args):  # 静かに
        pass

    def _send(self, code: int, body: dict):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not is_lan(self.client_address[0]):
            return self._send(403, {"error": "LAN only"})
        path = self.path.split("?")[0]
        if path in ("/", "/api/snapshot"):
            return self._send(200, snapshot())
        if path == "/api/health":
            return self._send(200, {"ok": True, "version": AGENT_VERSION})
        return self._send(404, {"error": "not found"})

    def _deny(self):
        self._send(405, {"error": "read-only"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _deny


BEACON_PORT = 8766


def own_mac() -> str | None:
    import uuid
    n = uuid.getnode()
    if (n >> 40) & 1:  # 取れずに乱数になったとき
        return None
    return ":".join(f"{(n >> s) & 0xff:02X}" for s in range(40, -1, -8))


def beacon_loop(name: str, role: str, port: int, beacon_port: int, interval: float = 10):
    """LAN にブロードキャストで名乗る (hub が IP と MAC を覚える)。中身は名前と待ち受けポートだけ"""
    import threading

    def run_():
        msg = json.dumps({"type": "banto-agent", "v": 1, "id": socket.gethostname(), "name": name,
                          "role": role, "port": port, "mac": own_mac()}).encode("utf-8")
        while True:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.sendto(msg, ("255.255.255.255", beacon_port))
            except OSError:
                pass
            time.sleep(interval)
    threading.Thread(target=run_, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="banto 収集エージェント (読み取り専用)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--name", default=None, help="画面に出す名前 (既定はホスト名)")
    ap.add_argument("--role", default="", help="gaming を指定すると Gaming PC 欄 (WoL) の対象になる")
    ap.add_argument("--no-beacon", action="store_true", help="自動検出用のブロードキャストを止める")
    ap.add_argument("--beacon-port", type=int, default=BEACON_PORT)
    ap.add_argument("--once", action="store_true", help="1 回だけ収集して標準出力に出す")
    a = ap.parse_args()
    if a.once:
        print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
        return
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    if not a.no_beacon:
        beacon_loop(a.name or socket.gethostname(), a.role, a.port, a.beacon_port)
    print(f"banto-agent {AGENT_VERSION} listening on {a.bind}:{a.port} (data: {banto_data_dirs() or 'not found'})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
