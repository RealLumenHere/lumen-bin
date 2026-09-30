// handle-store.js — on-disk persistence for DC process output (Lumen patch 2026-07-10)
// Zero-dep (node:sqlite). FAIL-OPEN: any error degrades to no-op; DC never worse than stock.
import os from 'os';
import fs from 'fs'; // Lumen detach patch 2026-07-19
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const DB_PATH = process.env.DC_HANDLE_DB || `${os.homedir()}/.dc-handles.db`;
const HOST = os.hostname();
const OUTPUT_CAP = 5 * 1024 * 1024;      // 5MB tail per proc
const PRUNE_AGE_MS = 48 * 3600 * 1000;   // done rows older than 48h
const DB_CAP_BYTES = 200 * 1024 * 1024;  // 200MB total
const FLUSH_MS = 1000;

class HandleStore {
    constructor() {
        this.ok = false;
        this.pending = new Map(); // pid -> string[] chunks awaiting flush
        try {
            const { DatabaseSync } = require('node:sqlite');
            this.db = new DatabaseSync(DB_PATH);
            this.db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;');
            this.db.exec(`CREATE TABLE IF NOT EXISTS proc(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pid INTEGER NOT NULL, host TEXT NOT NULL, cmd TEXT,
                started_at TEXT NOT NULL, output TEXT NOT NULL DEFAULT '',
                exit_code INTEGER, done_at TEXT);
                CREATE INDEX IF NOT EXISTS idx_proc_pid ON proc(pid);`);
            try { this.db.exec('ALTER TABLE proc ADD COLUMN out_file TEXT'); } catch (_e) { /* column exists */ }
            this.stIns = this.db.prepare('INSERT INTO proc(pid,host,cmd,started_at,out_file) VALUES(?,?,?,?,?)');
            this.stApp = this.db.prepare('UPDATE proc SET output = substr(output || ?, -?) WHERE id=?');
            this.stFin = this.db.prepare('UPDATE proc SET exit_code=?, done_at=? WHERE id=?');
            this.stGet = this.db.prepare('SELECT * FROM proc WHERE pid=? ORDER BY id DESC LIMIT 1');
            this.rowIds = new Map(); // pid -> row id (this DC lifetime)
            this.ok = true;
            this._prune();
            this.timer = setInterval(() => this._flushAll(), FLUSH_MS);
            if (this.timer.unref) this.timer.unref();
        } catch (e) {
            console.error('[handle-store] disabled:', e && e.message);
        }
    }
    onSpawn(pid, cmd, startTime, outFile) { // Lumen detach patch: + outFile
        if (!this.ok) return;
        try {
            const r = this.stIns.run(pid, HOST, String(cmd).slice(0, 4000),
                (startTime || new Date()).toISOString(), outFile || null);
            this.rowIds.set(pid, Number(r.lastInsertRowid));
        } catch (e) { this._trip(e); }
    }
    onOutput(pid, text) {
        if (!this.ok || !text) return;
        const buf = this.pending.get(pid) || [];
        buf.push(text);
        this.pending.set(pid, buf);
    }
    onExit(pid, code) {
        if (!this.ok) return;
        try {
            this._flush(pid);
            const id = this.rowIds.get(pid);
            if (id != null) {
                this.stFin.run(code == null ? null : code, new Date().toISOString(), id);
                this.rowIds.delete(pid);
            }
        } catch (e) { this._trip(e); }
    }
    // Read-back for PIDs missing from both in-memory Maps.
    // Returns {lines, exitCode, done, marker} or null. Wrong-host rows -> loud error object.
    recover(pid) {
        if (!this.ok) return null;
        try {
            this._flush(pid);
            const row = this.stGet.get(pid);
            if (!row) return null;
            if (row.host !== HOST) {
                return { wrongHost: true,
                    marker: `[handle-store ERROR: PID ${pid} handle belongs to host '${row.host}', you are on '${HOST}' — output not returned]` };
            }
            const done = row.done_at != null;
            // Lumen detach patch: prefer complete on-disk job file; detect still-running detached jobs
            let outText = String(row.output);
            let fileComplete = false;
            try {
                if (row.out_file && fs.existsSync(row.out_file)) {
                    const st = fs.statSync(row.out_file);
                    const start = Math.max(0, st.size - OUTPUT_CAP);
                    const buf = Buffer.alloc(st.size - start);
                    const fd = fs.openSync(row.out_file, 'r');
                    fs.readSync(fd, buf, 0, buf.length, start);
                    fs.closeSync(fd);
                    outText = buf.toString('utf8');
                    fileComplete = true;
                }
            } catch (_e) { /* fall back to flushed db output */ }
            let alive = false;
            if (!done) { try { process.kill(row.pid, 0); alive = true; } catch (_e) { alive = false; } }
            return {
                lines: outText.split('\n'),
                exitCode: row.exit_code,
                done,
                startedAt: row.started_at,
                doneAt: row.done_at,
                marker: done
                    ? `[from handle-store: process dead, exit=${row.exit_code}; output recovered from disk${fileComplete ? ' (complete)' : ''}]`
                    : alive
                        ? `[from handle-store: DC child recycled but PID ${row.pid} STILL RUNNING (detached); output so far${fileComplete ? ' — complete-to-now, re-read for more' : ' up to last flush'}]`
                        : `[from handle-store: process ended while DC recycled; output recovered from disk${fileComplete ? ' (complete)' : ' (up to last flush)'}]`
            };
        } catch (e) { this._trip(e); return null; }
    }
    _flush(pid) {
        const buf = this.pending.get(pid);
        if (!buf || !buf.length) return;
        const id = this.rowIds.get(pid);
        this.pending.delete(pid);
        if (id == null) return;
        this.stApp.run(buf.join(''), OUTPUT_CAP, id);
    }
    _flushAll() {
        if (!this.ok) return;
        try { for (const pid of Array.from(this.pending.keys())) this._flush(pid); }
        catch (e) { this._trip(e); }
    }
    _prune() {
        // Lumen detach patch: prune old .dc-jobs output files alongside db rows
        try {
            const dir = `${os.homedir()}/.dc-jobs`;
            for (const f of fs.readdirSync(dir)) {
                const p = `${dir}/${f}`;
                try { if (Date.now() - fs.statSync(p).mtimeMs > PRUNE_AGE_MS) fs.unlinkSync(p); } catch (_e) { }
            }
        } catch (_e) { /* dir may not exist yet */ }
        try {
            const cutoff = new Date(Date.now() - PRUNE_AGE_MS).toISOString();
            this.db.exec(`DELETE FROM proc WHERE done_at IS NOT NULL AND done_at < '${cutoff}'`);
            const pc = this.db.prepare('PRAGMA page_count').get();
            const ps = this.db.prepare('PRAGMA page_size').get();
            if (pc && ps && pc.page_count * ps.page_size > DB_CAP_BYTES) {
                this.db.exec(`DELETE FROM proc WHERE done_at IS NOT NULL AND id IN
                    (SELECT id FROM proc WHERE done_at IS NOT NULL ORDER BY id ASC
                     LIMIT (SELECT COUNT(*)/2 FROM proc WHERE done_at IS NOT NULL))`);
                this.db.exec('VACUUM');
            }
        } catch (e) { /* prune best-effort */ }
    }
    _trip(e) {
        console.error('[handle-store] write error (store disabled this run):', e && e.message);
        this.ok = false;
        if (this.timer) clearInterval(this.timer);
    }
}
export const handleStore = new HandleStore();
