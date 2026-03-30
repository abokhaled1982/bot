import sqlite3
import os

DB_PATH = os.path.join(os.getcwd(), '.temp/memecoin_bot.db')

def inspect_db():
    if not os.path.exists(DB_PATH):
        print(f"Datenbank nicht gefunden unter: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    print("--- Datenbank-Inhalt (letzte 100 Einträge) ---")
    try:
        c.execute("SELECT * FROM news_items ORDER BY rowid DESC LIMIT 100")
        rows = c.fetchall()
        for row in rows:
            print(row)
    except sqlite3.OperationalError as e:
        print(f"Fehler: {e}. (Eventuell existiert die Tabelle 'news_items' noch nicht?)")
    
    conn.close()

if __name__ == '__main__':
    inspect_db()
