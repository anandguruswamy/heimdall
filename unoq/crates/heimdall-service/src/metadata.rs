use std::{path::Path, sync::Arc};

use anyhow::Result;
use parking_lot::Mutex;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};

pub struct Metadata {
    connection: Mutex<Connection>,
}

#[derive(Debug, Clone)]
pub struct SegmentRow {
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
    pub closed_ns: i64,
}

impl Metadata {
    pub fn open(path: impl AsRef<Path>) -> Result<Arc<Self>> {
        let connection = Connection::open(path)?;
        connection.execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
             CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started_ns INTEGER NOT NULL, stopped_ns INTEGER);
             CREATE TABLE IF NOT EXISTS connections(id INTEGER PRIMARY KEY, run_id INTEGER, opened_ns INTEGER NOT NULL, closed_ns INTEGER, device TEXT NOT NULL, status TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS segments(id INTEGER PRIMARY KEY, connection_id INTEGER, path TEXT UNIQUE NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, closed_ns INTEGER NOT NULL, status TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS settings(epoch INTEGER PRIMARY KEY, changed_ns INTEGER NOT NULL, value TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS calibration(id INTEGER PRIMARY KEY, created_ns INTEGER NOT NULL, value TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS clips(id INTEGER PRIMARY KEY, created_ns INTEGER NOT NULL, value TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, created_ns INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL);",
        )?;
        Ok(Arc::new(Self {
            connection: Mutex::new(connection),
        }))
    }

    pub fn start_run(&self) -> Result<i64> {
        let db = self.connection.lock();
        db.execute("INSERT INTO runs(started_ns) VALUES(?1)", [now_ns()])?;
        Ok(db.last_insert_rowid())
    }

    pub fn stop_run(&self, id: i64) -> Result<()> {
        self.connection.lock().execute(
            "UPDATE runs SET stopped_ns=?1 WHERE id=?2",
            params![now_ns(), id],
        )?;
        Ok(())
    }

    pub fn open_connection(&self, run_id: i64, device: &str) -> Result<i64> {
        let db = self.connection.lock();
        db.execute(
            "INSERT INTO connections(run_id,opened_ns,device,status) VALUES(?1,?2,?3,'open')",
            params![run_id, now_ns(), device],
        )?;
        Ok(db.last_insert_rowid())
    }

    pub fn close_connection(&self, id: i64, status: &str) -> Result<()> {
        self.connection.lock().execute(
            "UPDATE connections SET closed_ns=?1,status=?2 WHERE id=?3",
            params![now_ns(), status, id],
        )?;
        Ok(())
    }

    pub fn catalog_segment(&self, connection_id: Option<i64>, row: &SegmentRow) -> Result<()> {
        self.connection.lock().execute(
            "INSERT INTO segments(connection_id,path,bytes,sha256,closed_ns,status)
             VALUES(?1,?2,?3,?4,?5,'verified')
             ON CONFLICT(path) DO UPDATE SET bytes=excluded.bytes,sha256=excluded.sha256,status='verified'",
            params![connection_id, row.path, row.bytes as i64, row.sha256, row.closed_ns],
        )?;
        Ok(())
    }

    pub fn verified_segments(&self) -> Result<Vec<SegmentRow>> {
        let db = self.connection.lock();
        let mut query = db.prepare(
            "SELECT path,bytes,sha256,closed_ns FROM segments WHERE status='verified' ORDER BY closed_ns,id",
        )?;
        Ok(query
            .query_map([], |row| {
                Ok(SegmentRow {
                    path: row.get(0)?,
                    bytes: row.get::<_, i64>(1)? as u64,
                    sha256: row.get(2)?,
                    closed_ns: row.get(3)?,
                })
            })?
            .collect::<rusqlite::Result<_>>()?)
    }

    pub fn mark_segment_deleted(&self, path: &str) -> Result<()> {
        self.connection.lock().execute(
            "UPDATE segments SET status='deleted' WHERE path=?1 AND status='verified'",
            [path],
        )?;
        Ok(())
    }

    pub fn settings(&self) -> Result<Value> {
        Ok(self
            .connection
            .lock()
            .query_row(
                "SELECT epoch,value FROM settings ORDER BY epoch DESC LIMIT 1",
                [],
                |row| Ok((row.get::<_, i64>(0)? as u64, row.get::<_, String>(1)?)),
            )
            .optional()?
            .map(|(epoch, value)| json!({"epoch": epoch, "value": serde_json::from_str::<Value>(&value).unwrap_or(Value::Null)}))
            .unwrap_or_else(|| json!({"epoch": 0, "value": {}})))
    }

    pub fn put_settings(&self, value: &Value) -> Result<Value> {
        let db = self.connection.lock();
        let epoch: i64 =
            db.query_row("SELECT COALESCE(MAX(epoch),0)+1 FROM settings", [], |r| {
                r.get(0)
            })?;
        db.execute(
            "INSERT INTO settings(epoch,changed_ns,value) VALUES(?1,?2,?3)",
            params![epoch, now_ns(), serde_json::to_string(value)?],
        )?;
        Ok(json!({"epoch": epoch, "value": value}))
    }

    pub fn clips(&self) -> Result<Vec<Value>> {
        let db = self.connection.lock();
        let mut query = db.prepare("SELECT id,created_ns,value FROM clips ORDER BY id DESC")?;
        Ok(query
            .query_map([], |row| {
                let value: String = row.get(2)?;
                Ok(json!({"id": row.get::<_, i64>(0)?, "created_ns": row.get::<_, i64>(1)?, "value": serde_json::from_str::<Value>(&value).unwrap_or(Value::Null)}))
            })?
            .collect::<rusqlite::Result<_>>()?)
    }

    pub fn add_clip(&self, value: &Value) -> Result<Value> {
        let db = self.connection.lock();
        let created = now_ns();
        db.execute(
            "INSERT INTO clips(created_ns,value) VALUES(?1,?2)",
            params![created, serde_json::to_string(value)?],
        )?;
        Ok(json!({"id": db.last_insert_rowid(), "created_ns": created, "value": value}))
    }

    pub fn clip(&self, id: i64) -> Result<Option<Value>> {
        let db = self.connection.lock();
        Ok(db
            .query_row(
                "SELECT id,created_ns,value FROM clips WHERE id=?1",
                [id],
                |row| {
                    let value: String = row.get(2)?;
                    Ok(json!({"id": row.get::<_, i64>(0)?, "created_ns": row.get::<_, i64>(1)?, "value": serde_json::from_str::<Value>(&value).unwrap_or(Value::Null)}))
                },
            )
            .optional()?)
    }

    pub fn update_clip(&self, id: i64, value: &Value) -> Result<()> {
        self.connection.lock().execute(
            "UPDATE clips SET value=?1 WHERE id=?2",
            params![serde_json::to_string(value)?, id],
        )?;
        Ok(())
    }

    pub fn delete_clip(&self, id: i64) -> Result<bool> {
        Ok(self
            .connection
            .lock()
            .execute("DELETE FROM clips WHERE id=?1", [id])?
            != 0)
    }

    pub fn calibration_snapshot(&self) -> Result<Value> {
        let db = self.connection.lock();
        Ok(db
            .query_row(
                "SELECT id,created_ns,value FROM calibration ORDER BY id DESC LIMIT 1",
                [],
                |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?, row.get::<_, String>(2)?)),
            )
            .optional()?
            .map(|(id, created, value)| json!({"id": id, "created_ns": created, "value": serde_json::from_str::<Value>(&value).unwrap_or(Value::Null)}))
            .unwrap_or_else(|| json!({"id": null, "value": null})))
    }

    pub fn add_calibration(&self, value: &Value) -> Result<Value> {
        let db = self.connection.lock();
        let created = now_ns();
        db.execute(
            "INSERT INTO calibration(created_ns,value) VALUES(?1,?2)",
            params![created, serde_json::to_string(value)?],
        )?;
        Ok(json!({"id": db.last_insert_rowid(), "created_ns": created, "value": value}))
    }

    pub fn calibration_stack(&self) -> Result<Vec<Vec<(u32, f64)>>> {
        let db = self.connection.lock();
        let mut query = db.prepare("SELECT value FROM calibration ORDER BY id")?;
        let values = query
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut stack = Vec::new();
        for encoded in values {
            let value: Value = serde_json::from_str(&encoded)?;
            match value["action"].as_str() {
                Some("apply") => {
                    if let Ok(offsets) = serde_json::from_value::<Vec<(u32, f64)>>(
                        value["solution"]["board_offsets"].clone(),
                    ) {
                        stack.push(offsets);
                    }
                }
                Some("rollback") => {
                    stack.pop();
                }
                _ => {}
            }
        }
        Ok(stack)
    }

    pub fn integrity_check(&self) -> Result<String> {
        Ok(self
            .connection
            .lock()
            .query_row("PRAGMA integrity_check", [], |r| r.get(0))?)
    }
}

pub fn now_ns() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .min(i64::MAX as u128) as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn metadata_and_settings_epochs() {
        let dir = tempfile::tempdir().unwrap();
        let db = Metadata::open(dir.path().join("test.db")).unwrap();
        assert_eq!(db.settings().unwrap()["epoch"], 0);
        assert_eq!(db.put_settings(&json!({"rate": 30})).unwrap()["epoch"], 1);
        assert_eq!(db.put_settings(&json!({"rate": 15})).unwrap()["epoch"], 2);
        assert_eq!(db.settings().unwrap()["value"]["rate"], 15);
        let clip = db.add_clip(&json!({"name": "a"})).unwrap();
        assert_eq!(db.clips().unwrap()[0]["id"], clip["id"]);
        let calibration = db
            .add_calibration(&json!({
                "action": "apply", "solution": {"board_offsets": [[1, 0.1], [2, -0.1]]}
            }))
            .unwrap();
        assert_eq!(db.calibration_snapshot().unwrap()["id"], calibration["id"]);
        assert_eq!(db.calibration_stack().unwrap()[0], [(1, 0.1), (2, -0.1)]);
        db.add_calibration(&json!({"action": "rollback"})).unwrap();
        assert!(db.calibration_stack().unwrap().is_empty());
        assert_eq!(db.integrity_check().unwrap(), "ok");
    }
}
