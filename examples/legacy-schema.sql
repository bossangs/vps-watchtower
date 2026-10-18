
        CREATE TABLE IF NOT EXISTS sites (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL,
          interval_seconds INTEGER NOT NULL DEFAULT 300, selector TEXT DEFAULT '',
          keywords TEXT DEFAULT '', price_threshold REAL, stock_keywords TEXT DEFAULT 'in stock,available,有货',
          channels TEXT DEFAULT 'telegram,email,discord,webhook', enabled INTEGER NOT NULL DEFAULT 1,
          last_checked TEXT, last_status TEXT DEFAULT 'Never checked', last_error TEXT DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL, content_hash TEXT NOT NULL,
          content TEXT NOT NULL, checked_at TEXT NOT NULL,
          FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_site ON snapshots(site_id, id DESC);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL, event_type TEXT NOT NULL,
          summary TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, channel TEXT NOT NULL,
          status TEXT NOT NULL, detail TEXT DEFAULT '', created_at TEXT NOT NULL,
          FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
        );
        
