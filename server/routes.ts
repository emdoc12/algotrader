import type { Express } from "express";
import type { Server } from "http";
import { storage } from "./storage";
import { insertAccountSchema, insertStrategySchema, insertTradeSchema, insertBotLogSchema, insertWatchlistItemSchema } from "@shared/schema";

export function registerRoutes(server: Server, app: Express) {
  // ============ ACCOUNTS ============
  app.get("/api/accounts", (_req, res) => {
    const accounts = storage.getAccounts();
    res.json(accounts);
  });

  app.get("/api/accounts/:id", (req, res) => {
    const account = storage.getAccount(Number(req.params.id));
    if (!account) return res.status(404).json({ error: "Account not found" });
    res.json(account);
  });

  app.post("/api/accounts", (req, res) => {
    const parsed = insertAccountSchema.safeParse(req.body);
    if (!parsed.success) return res.status(400).json({ error: parsed.error.flatten() });
    const account = storage.createAccount(parsed.data);
    res.status(201).json(account);
  });

  app.patch("/api/accounts/:id", (req, res) => {
    const account = storage.updateAccount(Number(req.params.id), req.body);
    if (!account) return res.status(404).json({ error: "Account not found" });
    res.json(account);
  });

  app.delete("/api/accounts/:id", (req, res) => {
    storage.deleteAccount(Number(req.params.id));
    res.status(204).send();
  });

  // ============ STRATEGIES ============
  app.get("/api/strategies", (_req, res) => {
    const strategies = storage.getStrategies();
    res.json(strategies);
  });

  app.get("/api/strategies/:id", (req, res) => {
    const strategy = storage.getStrategy(Number(req.params.id));
    if (!strategy) return res.status(404).json({ error: "Strategy not found" });
    res.json(strategy);
  });

  app.post("/api/strategies", (req, res) => {
    const parsed = insertStrategySchema.safeParse(req.body);
    if (!parsed.success) return res.status(400).json({ error: parsed.error.flatten() });
    const strategy = storage.createStrategy(parsed.data);
    res.status(201).json(strategy);
  });

  app.patch("/api/strategies/:id", (req, res) => {
    const strategy = storage.updateStrategy(Number(req.params.id), req.body);
    if (!strategy) return res.status(404).json({ error: "Strategy not found" });
    res.json(strategy);
  });

  app.delete("/api/strategies/:id", (req, res) => {
    storage.deleteStrategy(Number(req.params.id));
    res.status(204).send();
  });

  // Toggle strategy enabled/disabled
  app.post("/api/strategies/:id/toggle", (req, res) => {
    const strategy = storage.getStrategy(Number(req.params.id));
    if (!strategy) return res.status(404).json({ error: "Strategy not found" });
    const updated = storage.updateStrategy(strategy.id, { isEnabled: !strategy.isEnabled });
    
    storage.createLog({
      level: "info",
      strategyId: strategy.id,
      message: `Strategy "${strategy.name}" ${updated?.isEnabled ? "enabled" : "disabled"}`,
    });
    
    res.json(updated);
  });

  // ============ TRADES ============
  app.get("/api/trades", (req, res) => {
    const limit = req.query.limit ? Number(req.query.limit) : 100;
    const trades = storage.getTrades(limit);
    res.json(trades);
  });

  app.get("/api/trades/strategy/:strategyId", (req, res) => {
    const trades = storage.getTradesByStrategy(Number(req.params.strategyId));
    res.json(trades);
  });

  app.post("/api/trades", (req, res) => {
    const parsed = insertTradeSchema.safeParse(req.body);
    if (!parsed.success) return res.status(400).json({ error: parsed.error.flatten() });
    const trade = storage.createTrade(parsed.data);
    res.status(201).json(trade);
  });

  // ============ POSITIONS ============
  app.get("/api/positions", (req, res) => {
    const accountId = req.query.accountId ? Number(req.query.accountId) : undefined;
    const positions = storage.getPositions(accountId);
    res.json(positions);
  });

  // Upsert positions from the Python engine (replaces all positions for an account)
  app.post("/api/positions", (req, res) => {
    const { accountId, positions: positionList } = req.body;
    if (!accountId || !Array.isArray(positionList)) {
      return res.status(400).json({ error: "accountId and positions[] required" });
    }
    const now = new Date().toISOString();
    const normalized = positionList.map((p: any) => ({
      accountId: Number(accountId),
      symbol: p.symbol,
      instrumentType: p.instrumentType || p.instrument_type || "equity",
      quantity: Number(p.quantity),
      averagePrice: Number(p.averagePrice ?? p.average_price ?? 0),
      currentPrice: p.currentPrice ?? p.current_price ?? null,
      marketValue: p.marketValue ?? p.market_value ?? null,
      unrealizedPnl: p.unrealizedPnl ?? p.unrealized_pnl ?? null,
      optionDetails: p.optionDetails ?? p.option_details ?? null,
      updatedAt: now,
    }));
    storage.upsertPositions(Number(accountId), normalized);
    res.status(200).json({ updated: normalized.length });
  });

  // ============ BOT LOGS ============
  app.get("/api/logs", (req, res) => {
    const limit = req.query.limit ? Number(req.query.limit) : 200;
    const logs = storage.getLogs(limit);
    res.json(logs);
  });

  app.post("/api/logs", (req, res) => {
    const parsed = insertBotLogSchema.safeParse(req.body);
    if (!parsed.success) return res.status(400).json({ error: parsed.error.flatten() });
    const log = storage.createLog(parsed.data);
    res.status(201).json(log);
  });

  // ============ WATCHLIST ============
  app.get("/api/watchlist/:strategyId", (req, res) => {
    const items = storage.getWatchlistItems(Number(req.params.strategyId));
    res.json(items);
  });

  app.post("/api/watchlist", (req, res) => {
    const parsed = insertWatchlistItemSchema.safeParse(req.body);
    if (!parsed.success) return res.status(400).json({ error: parsed.error.flatten() });
    const item = storage.createWatchlistItem(parsed.data);
    res.status(201).json(item);
  });

  app.delete("/api/watchlist/:id", (req, res) => {
    storage.deleteWatchlistItem(Number(req.params.id));
    res.status(204).send();
  });

  // ============ BACKTESTS ============
  app.get("/api/backtests", (_req, res) => {
    res.json(storage.getBacktests());
  });

  app.get("/api/backtests/:id", (req, res) => {
    const bt = storage.getBacktest(Number(req.params.id));
    if (!bt) return res.status(404).json({ error: "Backtest not found" });
    res.json(bt);
  });

  app.get("/api/backtests/strategy/:strategyId", (req, res) => {
    res.json(storage.getBacktestsByStrategy(Number(req.params.strategyId)));
  });

  // Create a new backtest job (status=pending, Python engine picks it up)
  app.post("/api/backtests", (req, res) => {
    const { strategyId, startDate, endDate } = req.body;
    if (!strategyId || !startDate || !endDate) {
      return res.status(400).json({ error: "strategyId, startDate, endDate required" });
    }
    const strategy = storage.getStrategy(Number(strategyId));
    if (!strategy) return res.status(404).json({ error: "Strategy not found" });
    const bt = storage.createBacktest({
      strategyId: strategy.id,
      strategyName: strategy.name,
      strategyType: strategy.type,
      platform: strategy.platform,
      parameters: strategy.parameters,
      startDate,
      endDate,
      status: "pending",
      totalTrades: 0,
      winningTrades: 0,
      losingTrades: 0,
      totalPnl: 0,
      maxDrawdown: 0,
      winRate: 0,
      sharpeRatio: 0,
      trades: "[]",
      equityCurve: "[]",
    });
    res.status(201).json(bt);
  });

  // Python engine calls this to update backtest progress/results
  app.patch("/api/backtests/:id", (req, res) => {
    const bt = storage.updateBacktest(Number(req.params.id), req.body);
    if (!bt) return res.status(404).json({ error: "Backtest not found" });
    res.json(bt);
  });

  app.delete("/api/backtests/:id", (req, res) => {
    storage.deleteBacktest(Number(req.params.id));
    res.status(204).send();
  });

  // ============ DASHBOARD SUMMARY ============
  app.get("/api/dashboard", (_req, res) => {
    const allAccounts = storage.getAccounts();
    const allStrategies = storage.getStrategies();
    const recentTrades = storage.getTrades(20);
    const allPositions = storage.getPositions();
    const recentLogs = storage.getLogs(50);

    const activeStrategies = allStrategies.filter(s => s.isEnabled).length;
    const todaysTrades = recentTrades.filter(t => {
      if (!t.createdAt) return false;
      const tradeDate = new Date(t.createdAt).toDateString();
      return tradeDate === new Date().toDateString();
    });

    const totalPnl = recentTrades.reduce((sum, t) => sum + (t.pnl || 0), 0);
    const unrealizedPnl = allPositions.reduce((sum, p) => sum + (p.unrealizedPnl || 0), 0);

    res.json({
      accounts: allAccounts.length,
      activeStrategies,
      totalStrategies: allStrategies.length,
      todaysTrades: todaysTrades.length,
      totalTrades: recentTrades.length,
      realizedPnl: totalPnl,
      unrealizedPnl,
      openPositions: allPositions.length,
      recentTrades: recentTrades.slice(0, 10),
      recentLogs: recentLogs.slice(0, 20),
      strategies: allStrategies,
    });
  });
}
