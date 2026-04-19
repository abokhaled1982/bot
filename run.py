import subprocess
import time
import os
import signal
import sys

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
    print_header("MarketPulse & AlphaEngine Launcher")
    print(f"{GREEN}Starte News Engine (main.py) im Hintergrund...{RESET}")
    print(f"  → Logs findest du in: engine.log")
    
    # Start main.py in the background
    news_process = subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=subprocess.DEVNULL, # Suppress normal stdout
        stderr=subprocess.DEVNULL  # Suppress normal stderr (main.py logs to engine.log anyway)
    )
    
    time.sleep(2) # Give it a moment to start
    
    print(f"{GREEN}Starte Trading Bot (algo_trader.py)...{RESET}")
    time.sleep(1)
    
    # Start algo_trader.py in the foreground so it can take over the terminal
    bot_process = subprocess.Popen([sys.executable, "algo_trader.py"])
    
    def cleanup(sig, frame):
        print(f"\n{YELLOW}Beende alle Prozesse...{RESET}")
        bot_process.terminate()
        news_process.terminate()
        bot_process.wait()
        news_process.wait()
        print(f"{GREEN}Alle Prozesse sauber beendet.{RESET}")
        sys.exit(0)

    # Catch Ctrl+C to cleanly exit both
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        # Wait for the bot process to finish (which is never, unless it crashes)
        bot_process.wait()
    except KeyboardInterrupt:
        cleanup(None, None)

if __name__ == "__main__":
    main()
