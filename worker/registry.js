// The convoys on this relay, for its owner (GET /api/admin/convoys): one Registry object holds a row for each
// convoy from its start until it ends or expires. Only what is needed to find and describe a convoy is kept:
// its token, name, when it started, the country it was started from, and whether it has a password. The rest
// (members, activity) is asked of the convoy itself when the owner looks.
import { DurableObject } from "cloudflare:workers";

export class Registry extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`CREATE TABLE IF NOT EXISTS convoys (token TEXT PRIMARY KEY, name TEXT NOT NULL, started INTEGER NOT NULL,
                     country TEXT NOT NULL, locked INTEGER NOT NULL)`);
  }

  add({ token, name, started, country, locked }) {
    this.sql.exec("INSERT OR REPLACE INTO convoys (token, name, started, country, locked) VALUES (?, ?, ?, ?, ?)",
      token, name, started, country || "", locked ? 1 : 0);
  }

  remove(token) {
    this.sql.exec("DELETE FROM convoys WHERE token = ?", token);
  }

  list() {
    return this.sql.exec("SELECT token, name, started, country, locked FROM convoys ORDER BY started DESC").toArray()
      .map((r) => ({ ...r, locked: Boolean(r.locked) }));
  }
}
