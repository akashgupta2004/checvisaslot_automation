import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from src.auth.browser import ensure_chrome_debug_running


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


ACCOUNTS_FILE = Path(os.getenv("CHECKVISA_ACCOUNTS_FILE", Path(__file__).parent.parent / "accounts.json"))
PROFILE_ROOT = Path(os.getenv("CHECKVISA_PROFILE_ROOT", Path(__file__).parent.parent))
STATE_DIR = Path(os.getenv("CHECKVISA_STATE_DIR", Path(__file__).parent.parent / "state"))
PYTHON = sys.executable
BASE_CDP_PORT = int(os.getenv("CHECKVISA_BASE_CDP_PORT", "9222"))
OFC_URL = "https://www.usvisascheduling.com/en-US/ofc-schedule/"


def now_ts() -> float:
    return time.time()


def ts_text(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [SCHEDULER] {message}", flush=True)


class SchedulerBrowserLog:
    def info(self, message: str) -> None:
        log(message)

    def warning(self, message: str) -> None:
        log(f"WARNING {message}")

    def error(self, message: str) -> None:
        log(f"ERROR {message}")


def load_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        raise FileNotFoundError(f"accounts.json not found: {ACCOUNTS_FILE}")
    accounts = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("accounts.json must contain a non-empty list")
    deduped = {}
    order = []
    for account in accounts:
        name = (account.get("customer_name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in deduped:
            order.append(key)
        deduped[key] = account
    return [deduped[key] for key in order]


def account_cities(account: dict) -> str:
    cities = account.get("contributionCities") or account.get("ofcCities") or ["ANY"]
    if isinstance(cities, str):
        return cities
    return ",".join(cities)


def state_file(customer: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in customer)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"scheduler_state_{safe}.json"


def write_account_state(customer: str, payload: dict) -> None:
    data = {
        "customer_name": customer,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    state_file(customer).write_text(json.dumps(data, indent=2), encoding="utf-8")


def relay_output(proc: subprocess.Popen, label: str, ready_event: threading.Event | None = None) -> None:
    if not proc.stdout:
        return
    for line in proc.stdout:
        line = line.rstrip()
        print(f"[{label}] {line}", flush=True)
        if ready_event and "[READY]" in line:
            ready_event.set()


def kill_port_process(port: int) -> None:
    if os.name != "nt":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception:
        return
    pids = set()
    needles = (f"127.0.0.1:{port}", f"0.0.0.0:{port}", f"[::]:{port}", f"[::1]:{port}")
    for line in result.stdout.splitlines():
        if "LISTENING" not in line:
            continue
        if not any(needle in line for needle in needles):
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            pids.add(parts[-1])
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, timeout=10)
            log(f"Closed browser process on port {port} (PID {pid})")
        except Exception as exc:
            log(f"Failed to close PID {pid}: {exc}")

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                time.sleep(0.5)
        except OSError:
            return
    log(f"WARNING port {port} still appears active after cleanup")


def start_process(cmd: list[str], env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        cmd,
        cwd=str(Path(__file__).parent.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


def has_saved_profile(profile_dir: Path) -> bool:
    default_dir = profile_dir / "Default"
    return default_dir.exists() and any(
        (default_dir / name).exists()
        for name in ["Network", "Cookies", "Local Storage", "Session Storage"]
    )


def contribution_cmd(account: dict, port: int, args: argparse.Namespace, saved_session_only: bool = False) -> list[str]:
    cmd = [
        PYTHON,
        "-m",
        "src.core.contribution_runner",
        "--cdp-port",
        str(port),
        "--customer",
        account["customer_name"],
        "--cities",
        account_cities(account),
        "--dwell-seconds",
        str(args.dwell_seconds),
        "--cycles",
        "0",
        "--max-rotations",
        str(args.max_rotations),
        "--min-gap-seconds",
        str(args.min_gap_seconds),
        "--max-gap-seconds",
        str(args.max_gap_seconds),
    ]
    if saved_session_only:
        cmd.append("--saved-session-only")
    return cmd


def run_account(account: dict, args: argparse.Namespace, env: dict) -> None:
    customer = account["customer_name"]
    port = BASE_CDP_PORT
    profile_dir = PROFILE_ROOT / f"chrome_profile_{customer}"

    kill_port_process(port)
    write_account_state(customer, {
        "status": "starting",
        "rotation_count": 0,
        "max_rotations": args.max_rotations,
        "cooldown_until": None,
    })

    if has_saved_profile(profile_dir):
        log(f"Saved profile found for {customer}; trying direct OFC contribution without login.")
        ensure_chrome_debug_running(port, str(profile_dir), SchedulerBrowserLog(), start_url=OFC_URL)
        write_account_state(customer, {
            "status": "checking_saved_session",
            "rotation_count": 0,
            "max_rotations": args.max_rotations,
        })
        contrib_proc = start_process(contribution_cmd(account, port, args, saved_session_only=True), env)
        threading.Thread(target=relay_output, args=(contrib_proc, f"contrib:{customer}"), daemon=True).start()
        code = contrib_proc.wait()
        log(f"Saved-session contribution exited for {customer} with code {code}")
        kill_port_process(port)
        if code == 0:
            return
        time.sleep(2)
        log(f"Saved session was not usable for {customer}; falling back to login.")

    login_cmd = [
        PYTHON,
        "-m",
        "src.core.login_runner",
        "--username",
        account["username"],
        "--password",
        account["password"],
        "--cdp-port",
        str(port),
        "--customer",
        customer,
        "--profile-dir",
        str(profile_dir),
    ]

    log(f"Starting account {customer} on port {port}")
    login_proc = start_process(login_cmd, env)
    ready = threading.Event()
    threading.Thread(target=relay_output, args=(login_proc, f"login:{customer}", ready), daemon=True).start()

    deadline = time.time() + args.login_timeout_seconds
    while time.time() < deadline:
        if ready.is_set():
            break
        if login_proc.poll() is not None:
            break
        time.sleep(1)

    if not ready.is_set():
        code = login_proc.poll()
        write_account_state(customer, {"status": f"login_failed_or_timeout", "rotation_count": 0})
        log(f"Login failed or timeout for {customer} (exit code {code}); moving to next.")
        try:
            login_proc.terminate()
        except Exception:
            pass
        kill_port_process(port)
        return code

    write_account_state(customer, {
        "status": "contributing",
        "rotation_count": 0,
        "max_rotations": args.max_rotations,
    })
    
    contrib_proc = start_process(contribution_cmd(account, port, args), env)
    threading.Thread(target=relay_output, args=(contrib_proc, f"contrib:{customer}"), daemon=True).start()
    code = contrib_proc.wait()
    
    log(f"Contribution runner exited for {customer} with code {code}")

    try:
        login_proc.terminate()
    except Exception:
        pass
    kill_port_process(port)
    return code


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequential contribution scheduler with account cooldown.")
    parser.add_argument("--dwell-seconds", type=int, default=12)
    parser.add_argument("--max-rotations", type=int, default=20)
    parser.add_argument("--cooldown-seconds", type=int, default=3600)
    parser.add_argument("--login-timeout-seconds", type=int, default=900)
    parser.add_argument("--min-gap-seconds", type=int, default=15)
    parser.add_argument("--max-gap-seconds", type=int, default=20)
    parser.add_argument("--session-gap-seconds", type=int, default=300, help="Seconds to wait between consecutive account sessions")
    args = parser.parse_args()

    accounts = load_accounts()
    log(f"Loaded {len(accounts)} account(s). Max rotations/account={args.max_rotations}, cooldown={args.cooldown_seconds}s")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    cooldown_until: dict[str, float] = {}
    stopped = False

    def stop(_signum=None, _frame=None):
        nonlocal stopped
        stopped = True
        log("Stop requested. Finishing current cleanup.")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while not stopped:
        ran_any = False
        for account in accounts:
            if stopped:
                break
            customer = account["customer_name"]
            available_at = cooldown_until.get(customer, 0)
            if now_ts() < available_at:
                continue

            ran_any = True
            code = run_account(account, args, env)
            
            if code == 43:
                log(f"Cloudflare block (Code 43) detected for {customer}. Deleting user session and retrying...")
                profile_dir = PROFILE_ROOT / f"chrome_profile_{customer}"
                import shutil
                try:
                    shutil.rmtree(profile_dir / "Default" / "Sessions", ignore_errors=True)
                    shutil.rmtree(profile_dir / "Default" / "Network", ignore_errors=True)
                except Exception as ex:
                    log.warning(f"Failed to delete session for {customer}: {ex}")
                cooldown_until[customer] = now_ts()  # 0 cooldown
                write_account_state(customer, {
                    "status": "session_wiped_restarting",
                    "cooldown_until": ts_text(cooldown_until[customer]),
                    "max_rotations": args.max_rotations,
                })
            else:
                cooldown_until[customer] = now_ts() + args.cooldown_seconds
                write_account_state(customer, {
                    "status": "cooldown",
                    "cooldown_until": ts_text(cooldown_until[customer]),
                    "cooldown_seconds": args.cooldown_seconds,
                    "max_rotations": args.max_rotations,
                })
                log(f"{customer} is cooling down until {ts_text(cooldown_until[customer])}")

            # Wait gap between sessions (responsive to stops)
            if args.session_gap_seconds > 0 and not stopped:
                log(f"Waiting {args.session_gap_seconds} seconds gap before the next session...")
                gap_remaining = args.session_gap_seconds
                while gap_remaining > 0 and not stopped:
                    sleep_time = min(5, gap_remaining)
                    time.sleep(sleep_time)
                    gap_remaining -= sleep_time

        if stopped:
            break

        if not ran_any:
            next_ready = min(cooldown_until.values()) if cooldown_until else now_ts() + 10
            sleep_for = max(5, min(60, int(next_ready - now_ts())))
            log(f"No eligible accounts. Sleeping {sleep_for}s.")
            time.sleep(sleep_for)


if __name__ == "__main__":
    main()
