import subprocess
import time
import os
import signal
import sys

VENV_PYTHON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python")
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = sys.executable

# ansi colors
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

def print_header(msg):
    print(f"{CYAN}═" * 70)
    print(f"  {msg}")
    print(f"═" * 70 + f"{RESET}")

def main():
    print_header("MarketPulse, AlphaEngine & Dashboard Launcher")
    
    print(f"{GREEN}Starte News Engine (main.py) im Hintergrund...{RESET}")
    print(f"  → Logs findest du in: engine.log")
    news_process = subprocess.Popen(
        [VENV_PYTHON, "main.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(1)
    
    print(f"{GREEN}Starte Dashboard (Streamlit) im Hintergrund...{RESET}")
    print(f"  → Erreichbar unter: http://localhost:8501")
    dashboard_process = subprocess.Popen(
        [VENV_PYTHON, "-m", "streamlit", "run", "dashboard/app.py", "--server.headless=true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(2)
    
    print(f"{GREEN}Starte Trading Bot (algo_trader.py)...{RESET}")
    time.sleep(1)
    bot_process = subprocess.Popen([VENV_PYTHON, "algo_trader.py"])
    
    def cleanup(sig, frame):
        print(f"\n{YELLOW}Beende alle Prozesse...{RESET}")
        try:
            bot_process.terminate()
            news_process.terminate()
            dashboard_process.terminate()
            bot_process.wait(timeout=2)
            news_process.wait(timeout=2)
            dashboard_process.wait(timeout=2)
        except Exception as e:
            pass
        print(f"{GREEN}Alle Prozesse sauber beendet.{RESET}")
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        bot_process.wait()
    except KeyboardInterrupt:
        cleanup(None, None)

if __name__ == "__main__":
    main()
