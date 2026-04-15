import { sqliteTable, text, integer, real } from "drizzle-orm/sqlite-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod";

// Tastytrade account credentials
export const accounts = sqliteTable("accounts", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  name: text("name").notNull(),
  platform: text("platform").notNull(), // 'tastytrade' | 'tasty_crypto'
  username: text("username").notNull(),
  accountNumber: text("account_number").notNull(),
  sessionToken: text("session_token"),
  rememberToken: text("remember_token"),
  isActive: integer("is_active", { mode: "boolean" }).notNull().default(true),
  isSandbox: integer("is_sandbox", { mode: "boolean" }).notNull().default(false),
  createdAt: text("created_at").notNull().default("now"),
});

// Trading strategies
export const strategies = sqliteTable("strategies", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  name: text("name").notNull(),
  type: text("type").notNull(), // 'short_put' | 'credit_spread' | 'covered_call' | 'iron_condor' | 'crypto_momentum' | 'crypto_mean_reversion' | 'custom'
  platform: text("platform").notNull(), // 'tastytrade' | 'tasty_crypto'
  isEnabled: integer("is_enabled", { mode: "boolean" }).notNull().default(false),
  accountId: integer("account_id").notNull(),
  // Strategy parameters as JSON
  parameters: text("parameters").notNull().default("{}"),
  // Scheduling
  scanInterval: integer("scan_interval").notNull().default(300), // seconds
  lastScanAt: text("last_scan_at"),
  // Risk limits
  maxPositionSize: real("max_position_size").notNull().default(1),
  maxDailyTrades: integer("max_daily_trades").notNull().default(5),
  maxBuyingPowerUsage: real("max_buying_power_usage").notNull().default(50), // percentage
  createdAt: text("created_at").notNull().default("now"),
});

// Trade log
export const trades = sqliteTable("trades", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  strategyId: integer("strategy_id").notNull(),
  accountId: integer("account_id").notNull(),
  platform: text("platform").notNull(),
  symbol: text("symbol").notNull(),
  action: text("action").notNull(), // 'BUY_TO_OPEN' | 'SELL_TO_OPEN' | 'BUY_TO_CLOSE' | 'SELL_TO_CLOSE'
  instrumentType: text("instrument_type").notNull(), // 'equity' | 'option' | 'crypto'
  quantity: real("quantity").notNull(),
  price: real("price"),
  status: text("status").notNull().default("pending"), // 'pending' | 'filled' | 'cancelled' | 'rejected'
  orderId: text("order_id"),
  optionDetails: text("option_details"), // JSON for strike, exp, etc.
  pnl: real("pnl"),
  notes: text("notes"),
  executedAt: text("executed_at"),
  createdAt: text("created_at").notNull().default("now"),
});

// Positions snapshot
export const positions = sqliteTable("positions", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  accountId: integer("account_id").notNull(),
  symbol: text("symbol").notNull(),
  instrumentType: text("instrument_type").notNull(),
  quantity: real("quantity").notNull(),
  averagePrice: real("average_price").notNull(),
  currentPrice: real("current_price"),
  marketValue: real("market_value"),
  unrealizedPnl: real("unrealized_pnl"),
  optionDetails: text("option_details"), // JSON
  updatedAt: text("updated_at").notNull(),
});

// Bot activity log
export const botLogs = sqliteTable("bot_logs", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  level: text("level").notNull().default("info"), // 'info' | 'warn' | 'error' | 'trade'
  strategyId: integer("strategy_id"),
  message: text("message").notNull(),
  details: text("details"), // JSON
  createdAt: text("created_at").notNull().default("now"),
});

// Watchlist for scan targets
export const watchlistItems = sqliteTable("watchlist_items", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  strategyId: integer("strategy_id").notNull(),
  symbol: text("symbol").notNull(),
  notes: text("notes"),
});

// Insert schemas
export const insertAccountSchema = createInsertSchema(accounts).omit({ id: true, createdAt: true, sessionToken: true, rememberToken: true });
export const insertStrategySchema = createInsertSchema(strategies).omit({ id: true, createdAt: true, lastScanAt: true });
export const insertTradeSchema = createInsertSchema(trades).omit({ id: true, createdAt: true });
export const insertBotLogSchema = createInsertSchema(botLogs).omit({ id: true, createdAt: true });
export const insertWatchlistItemSchema = createInsertSchema(watchlistItems).omit({ id: true });

// Types
export type Account = typeof accounts.$inferSelect;
export type InsertAccount = z.infer<typeof insertAccountSchema>;
export type Strategy = typeof strategies.$inferSelect;
export type InsertStrategy = z.infer<typeof insertStrategySchema>;
export type Trade = typeof trades.$inferSelect;
export type InsertTrade = z.infer<typeof insertTradeSchema>;
export type Position = typeof positions.$inferSelect;
export type BotLog = typeof botLogs.$inferSelect;
export type InsertBotLog = z.infer<typeof insertBotLogSchema>;
export type WatchlistItem = typeof watchlistItems.$inferSelect;
export type InsertWatchlistItem = z.infer<typeof insertWatchlistItemSchema>;
