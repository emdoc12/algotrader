import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";
import * as schema from "@shared/schema";
import path from "path";

// In Docker, DATABASE_URL points to the persistent volume (/app/data/data.db).
// In development it falls back to data.db in the project root.
const dbPath = process.env.DATABASE_URL ?? path.resolve("data.db");
const sqlite = new Database(dbPath);
sqlite.pragma("journal_mode = WAL");

export const db = drizzle(sqlite, { schema });
