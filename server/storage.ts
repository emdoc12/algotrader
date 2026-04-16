import { accounts, strategies, trades, positions, botLogs, watchlistItems, backtests } from "@shared/schema";
import type { Account, InsertAccount, Strategy, InsertStrategy, Trade, InsertTrade, Position, BotLog, InsertBotLog, WatchlistItem, InsertWatchlistItem, Backtest, InsertBacktest } from "@shared/schema";
import { db } from "./db";
import { eq, desc } from "drizzle-orm";

export interface IStorage {
  // Accounts
  getAccounts(): Account[];
  getAccount(id: number): Account | undefined;
  createAccount(data: InsertAccount): Account;
  updateAccount(id: number, data: Partial<Account>): Account | undefined;
  deleteAccount(id: number): void;

  // Strategies
  getStrategies(): Strategy[];
  getStrategy(id: number): Strategy | undefined;
  getStrategiesByAccount(accountId: number): Strategy[];
  createStrategy(data: InsertStrategy): Strategy;
  updateStrategy(id: number, data: Partial<Strategy>): Strategy | undefined;
  deleteStrategy(id: number): void;

  // Trades
  getTrades(limit?: number): Trade[];
  getTradesByStrategy(strategyId: number): Trade[];
  createTrade(data: InsertTrade): Trade;
  updateTrade(id: number, data: Partial<Trade>): Trade | undefined;

  // Positions
  getPositions(accountId?: number): Position[];
  upsertPositions(accountId: number, positionList: Omit<Position, "id">[]): void;

  // Bot Logs
  getLogs(limit?: number): BotLog[];
  createLog(data: InsertBotLog): BotLog;

  // Watchlist
  getWatchlistItems(strategyId: number): WatchlistItem[];
  createWatchlistItem(data: InsertWatchlistItem): WatchlistItem;
  deleteWatchlistItem(id: number): void;

  // Backtests
  getBacktests(): Backtest[];
  getBacktest(id: number): Backtest | undefined;
  getBacktestsByStrategy(strategyId: number): Backtest[];
  createBacktest(data: InsertBacktest): Backtest;
  updateBacktest(id: number, data: Partial<Backtest>): Backtest | undefined;
  deleteBacktest(id: number): void;
}

export class DatabaseStorage implements IStorage {
  // Accounts
  getAccounts(): Account[] {
    return db.select().from(accounts).all();
  }
  getAccount(id: number): Account | undefined {
    return db.select().from(accounts).where(eq(accounts.id, id)).get();
  }
  createAccount(data: InsertAccount): Account {
    return db.insert(accounts).values({ ...data, createdAt: new Date().toISOString() }).returning().get();
  }
  updateAccount(id: number, data: Partial<Account>): Account | undefined {
    return db.update(accounts).set(data).where(eq(accounts.id, id)).returning().get();
  }
  deleteAccount(id: number): void {
    db.delete(accounts).where(eq(accounts.id, id)).run();
  }

  // Strategies
  getStrategies(): Strategy[] {
    return db.select().from(strategies).all();
  }
  getStrategy(id: number): Strategy | undefined {
    return db.select().from(strategies).where(eq(strategies.id, id)).get();
  }
  getStrategiesByAccount(accountId: number): Strategy[] {
    return db.select().from(strategies).where(eq(strategies.accountId, accountId)).all();
  }
  createStrategy(data: InsertStrategy): Strategy {
    return db.insert(strategies).values({ ...data, createdAt: new Date().toISOString() }).returning().get();
  }
  updateStrategy(id: number, data: Partial<Strategy>): Strategy | undefined {
    return db.update(strategies).set(data).where(eq(strategies.id, id)).returning().get();
  }
  deleteStrategy(id: number): void {
    db.delete(strategies).where(eq(strategies.id, id)).run();
  }

  // Trades
  getTrades(limit = 100): Trade[] {
    return db.select().from(trades).orderBy(desc(trades.id)).limit(limit).all();
  }
  getTradesByStrategy(strategyId: number): Trade[] {
    return db.select().from(trades).where(eq(trades.strategyId, strategyId)).orderBy(desc(trades.id)).all();
  }
  createTrade(data: InsertTrade): Trade {
    return db.insert(trades).values({ ...data, createdAt: new Date().toISOString() }).returning().get();
  }
  updateTrade(id: number, data: Partial<Trade>): Trade | undefined {
    return db.update(trades).set(data).where(eq(trades.id, id)).returning().get();
  }

  // Positions
  getPositions(accountId?: number): Position[] {
    if (accountId) {
      return db.select().from(positions).where(eq(positions.accountId, accountId)).all();
    }
    return db.select().from(positions).all();
  }
  upsertPositions(accountId: number, positionList: Omit<Position, "id">[]): void {
    db.delete(positions).where(eq(positions.accountId, accountId)).run();
    for (const pos of positionList) {
      db.insert(positions).values(pos).run();
    }
  }

  // Bot Logs
  getLogs(limit = 200): BotLog[] {
    return db.select().from(botLogs).orderBy(desc(botLogs.id)).limit(limit).all();
  }
  createLog(data: InsertBotLog): BotLog {
    return db.insert(botLogs).values({ ...data, createdAt: new Date().toISOString() }).returning().get();
  }

  // Watchlist
  getWatchlistItems(strategyId: number): WatchlistItem[] {
    return db.select().from(watchlistItems).where(eq(watchlistItems.strategyId, strategyId)).all();
  }
  createWatchlistItem(data: InsertWatchlistItem): WatchlistItem {
    return db.insert(watchlistItems).values(data).returning().get();
  }
  deleteWatchlistItem(id: number): void {
    db.delete(watchlistItems).where(eq(watchlistItems.id, id)).run();
  }

  // Backtests
  getBacktests(): Backtest[] {
    return db.select().from(backtests).orderBy(desc(backtests.id)).all();
  }
  getBacktest(id: number): Backtest | undefined {
    return db.select().from(backtests).where(eq(backtests.id, id)).get();
  }
  getBacktestsByStrategy(strategyId: number): Backtest[] {
    return db.select().from(backtests).where(eq(backtests.strategyId, strategyId)).orderBy(desc(backtests.id)).all();
  }
  createBacktest(data: InsertBacktest): Backtest {
    return db.insert(backtests).values({ ...data, createdAt: new Date().toISOString() }).returning().get();
  }
  updateBacktest(id: number, data: Partial<Backtest>): Backtest | undefined {
    return db.update(backtests).set(data).where(eq(backtests.id, id)).returning().get();
  }
  deleteBacktest(id: number): void {
    db.delete(backtests).where(eq(backtests.id, id)).run();
  }
}

export const storage = new DatabaseStorage();
