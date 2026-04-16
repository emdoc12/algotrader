import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { apiRequest, queryClient } from "@/lib/queryClient";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { Play, Trash2, ChevronDown, ChevronUp, TrendingUp, TrendingDown, BarChart3, AlertCircle, Clock, CheckCircle2 } from "lucide-react";
import type { Strategy, Backtest } from "@shared/schema";

const BACKTEST_SUPPORTED = ["crypto_momentum", "crypto_mean_reversion"];

function StatusBadge({ status }: { status: string }) {
  if (status === "completed") return <Badge className="bg-emerald-600 text-white"><CheckCircle2 className="h-3 w-3 mr-1" />Completed</Badge>;
  if (status === "running") return <Badge className="bg-blue-600 text-white"><Clock className="h-3 w-3 mr-1 animate-spin" />Running</Badge>;
  if (status === "failed") return <Badge variant="destructive"><AlertCircle className="h-3 w-3 mr-1" />Failed</Badge>;
  return <Badge variant="secondary"><Clock className="h-3 w-3 mr-1" />Pending</Badge>;
}

function BacktestCard({ bt, onDelete }: { bt: Backtest; onDelete: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const trades = JSON.parse(bt.trades || "[]") as Array<{ date: string; action: string; price: number; qty: number; pnl: number; reason: string }>;
  const equityCurve = JSON.parse(bt.equityCurve || "[]") as Array<{ date: string; equity: number }>;
  const params = JSON.parse(bt.parameters || "{}");

  const pnlColor = (bt.totalPnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400";

  return (
    <Card data-testid={`card-backtest-${bt.id}`}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-base font-semibold">{bt.strategyName}</CardTitle>
            <p className="text-xs text-muted-foreground mt-0.5">
              {bt.startDate} → {bt.endDate} · {bt.strategyType.replace(/_/g, " ")}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <StatusBadge status={bt.status} />
            <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" data-testid={`button-delete-bt-${bt.id}`} onClick={onDelete}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </CardHeader>

      {bt.status === "completed" && (
        <CardContent className="space-y-4">
          {/* Stats row */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div className="bg-muted/50 rounded-md p-3">
              <p className="text-xs text-muted-foreground">Total P&L</p>
              <p className={`text-lg font-bold font-mono ${pnlColor}`}>
                {(bt.totalPnl ?? 0) >= 0 ? "+" : ""}${(bt.totalPnl ?? 0).toLocaleString()}
              </p>
            </div>
            <div className="bg-muted/50 rounded-md p-3">
              <p className="text-xs text-muted-foreground">Win Rate</p>
              <p className="text-lg font-bold font-mono">{bt.winRate?.toFixed(1)}%</p>
            </div>
            <div className="bg-muted/50 rounded-md p-3">
              <p className="text-xs text-muted-foreground">Max Drawdown</p>
              <p className="text-lg font-bold font-mono text-red-400">{bt.maxDrawdown?.toFixed(1)}%</p>
            </div>
            <div className="bg-muted/50 rounded-md p-3">
              <p className="text-xs text-muted-foreground">Sharpe Ratio</p>
              <p className="text-lg font-bold font-mono">{bt.sharpeRatio?.toFixed(2)}</p>
            </div>
          </div>

          <div className="grid grid-cols-3 gap-3 text-sm">
            <div className="text-center">
              <p className="text-muted-foreground text-xs">Trades</p>
              <p className="font-semibold">{bt.totalTrades}</p>
            </div>
            <div className="text-center">
              <p className="text-muted-foreground text-xs">Winners</p>
              <p className="font-semibold text-emerald-400">{bt.winningTrades}</p>
            </div>
            <div className="text-center">
              <p className="text-muted-foreground text-xs">Losers</p>
              <p className="font-semibold text-red-400">{bt.losingTrades}</p>
            </div>
          </div>

          {/* Equity curve chart */}
          {equityCurve.length > 1 && (
            <div>
              <p className="text-xs font-medium text-muted-foreground mb-2">Equity Curve</p>
              <ResponsiveContainer width="100%" height={160}>
                <LineChart data={equityCurve} margin={{ top: 4, right: 4, left: 0, bottom: 4 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    tickFormatter={(v) => v.slice(5)} // show MM-DD only
                    interval="preserveStartEnd"
                  />
                  <YAxis
                    tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    tickFormatter={(v) => `$${(v / 1000).toFixed(0)}k`}
                    width={48}
                  />
                  <Tooltip
                    contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", borderRadius: 6, fontSize: 12 }}
                    formatter={(v: number) => [`$${v.toLocaleString()}`, "Equity"]}
                  />
                  <Line
                    type="monotone"
                    dataKey="equity"
                    stroke="hsl(var(--primary))"
                    dot={false}
                    strokeWidth={2}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Trade log (expandable) */}
          {trades.length > 0 && (
            <div>
              <button
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                onClick={() => setExpanded(!expanded)}
                data-testid={`button-expand-bt-${bt.id}`}
              >
                {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
                {expanded ? "Hide" : "Show"} trade log ({trades.filter(t => t.action === "SELL").length} closed trades)
              </button>
              {expanded && (
                <div className="mt-2 max-h-60 overflow-y-auto rounded-md border text-xs">
                  <table className="w-full">
                    <thead className="bg-muted/50 sticky top-0">
                      <tr>
                        <th className="text-left p-2 font-medium">Date</th>
                        <th className="text-left p-2 font-medium">Action</th>
                        <th className="text-right p-2 font-medium">Price</th>
                        <th className="text-right p-2 font-medium">Qty</th>
                        <th className="text-right p-2 font-medium">P&L</th>
                        <th className="text-left p-2 font-medium">Reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {trades.map((t, i) => (
                        <tr key={i} className="border-t border-border/50">
                          <td className="p-2 text-muted-foreground">{t.date}</td>
                          <td className="p-2">
                            <Badge variant={t.action === "BUY" ? "default" : "secondary"} className="text-xs py-0">
                              {t.action}
                            </Badge>
                          </td>
                          <td className="p-2 text-right font-mono">${t.price?.toLocaleString()}</td>
                          <td className="p-2 text-right font-mono">{t.qty?.toFixed(4)}</td>
                          <td className={`p-2 text-right font-mono ${t.pnl > 0 ? "text-emerald-400" : t.pnl < 0 ? "text-red-400" : ""}`}>
                            {t.pnl !== 0 ? `${t.pnl >= 0 ? "+" : ""}$${t.pnl?.toFixed(2)}` : "—"}
                          </td>
                          <td className="p-2 text-muted-foreground">{t.reason?.replace(/_/g, " ")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </CardContent>
      )}

      {bt.status === "failed" && (
        <CardContent>
          <div className="bg-destructive/10 border border-destructive/20 rounded-md p-3 text-sm text-destructive">
            {bt.errorMessage || "Unknown error"}
          </div>
        </CardContent>
      )}

      {bt.status === "running" && (
        <CardContent>
          <div className="flex items-center gap-2 text-sm text-muted-foreground animate-pulse">
            <Clock className="h-4 w-4" />
            Fetching historical data and simulating trades...
          </div>
        </CardContent>
      )}
    </Card>
  );
}

export default function Backtests() {
  const { toast } = useToast();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [form, setForm] = useState({
    strategyId: "",
    startDate: new Date(Date.now() - 365 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10), // 1yr ago
    endDate: new Date().toISOString().slice(0, 10),
  });

  const { data: backtests = [], isLoading, refetch } = useQuery<Backtest[]>({
    queryKey: ["/api/backtests"],
    queryFn: () => apiRequest("GET", "/api/backtests").then(r => r.json()),
    refetchInterval: (data) => {
      const running = (data as Backtest[] | undefined)?.some(b => b.status === "running" || b.status === "pending");
      return running ? 3000 : false;
    },
  });

  const { data: strategies = [] } = useQuery<Strategy[]>({
    queryKey: ["/api/strategies"],
    queryFn: () => apiRequest("GET", "/api/strategies").then(r => r.json()),
  });

  const supportedStrategies = strategies.filter(s => BACKTEST_SUPPORTED.includes(s.type));

  const createMutation = useMutation({
    mutationFn: (data: typeof form) => apiRequest("POST", "/api/backtests", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/backtests"] });
      setDialogOpen(false);
      toast({ title: "Backtest queued — results will appear shortly" });
    },
    onError: () => toast({ title: "Failed to start backtest", variant: "destructive" }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => apiRequest("DELETE", `/api/backtests/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/backtests"] });
      toast({ title: "Backtest deleted" });
    },
  });

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Backtests</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Simulate strategies against historical data before going live
          </p>
        </div>
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogTrigger asChild>
            <Button data-testid="button-new-backtest" disabled={supportedStrategies.length === 0}>
              <Play className="h-4 w-4 mr-2" /> Run Backtest
            </Button>
          </DialogTrigger>
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle>Configure Backtest</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 pt-2">
              <div>
                <Label>Strategy</Label>
                <Select value={form.strategyId} onValueChange={v => setForm({ ...form, strategyId: v })}>
                  <SelectTrigger data-testid="select-bt-strategy">
                    <SelectValue placeholder="Select a strategy" />
                  </SelectTrigger>
                  <SelectContent>
                    {supportedStrategies.map(s => (
                      <SelectItem key={s.id} value={String(s.id)}>{s.name} ({s.type.replace(/_/g, " ")})</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {supportedStrategies.length === 0 && (
                  <p className="text-xs text-muted-foreground mt-1">
                    Create a Crypto Momentum or Crypto Mean Reversion strategy first.
                  </p>
                )}
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <Label>Start Date</Label>
                  <Input
                    data-testid="input-bt-start"
                    type="date"
                    value={form.startDate}
                    onChange={e => setForm({ ...form, startDate: e.target.value })}
                  />
                </div>
                <div>
                  <Label>End Date</Label>
                  <Input
                    data-testid="input-bt-end"
                    type="date"
                    value={form.endDate}
                    max={new Date().toISOString().slice(0, 10)}
                    onChange={e => setForm({ ...form, endDate: e.target.value })}
                  />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                Uses the strategy's current parameters and initial capital of $10,000.
                Historical OHLCV data is fetched from Kraken's public API.
              </p>
              <Button
                data-testid="button-run-backtest"
                className="w-full"
                disabled={!form.strategyId || !form.startDate || !form.endDate || createMutation.isPending}
                onClick={() => createMutation.mutate(form)}
              >
                {createMutation.isPending ? "Queuing..." : "Run Backtest"}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      {supportedStrategies.length === 0 && !isLoading && (
        <Card>
          <CardContent className="py-10 text-center space-y-2">
            <BarChart3 className="h-10 w-10 mx-auto text-muted-foreground/40" />
            <p className="text-muted-foreground text-sm">
              Backtesting is available for Crypto Momentum and Crypto Mean Reversion strategies.
            </p>
            <p className="text-xs text-muted-foreground">Create one of those strategies first, then come back to run a backtest.</p>
          </CardContent>
        </Card>
      )}

      {isLoading && (
        <div className="space-y-3">
          {[1, 2].map(i => <Skeleton key={i} className="h-32 w-full" />)}
        </div>
      )}

      {!isLoading && backtests.length === 0 && supportedStrategies.length > 0 && (
        <Card>
          <CardContent className="py-10 text-center">
            <BarChart3 className="h-10 w-10 mx-auto text-muted-foreground/40 mb-3" />
            <p className="text-muted-foreground text-sm">No backtests yet. Click "Run Backtest" to simulate your strategy.</p>
          </CardContent>
        </Card>
      )}

      <div className="space-y-4">
        {backtests.map(bt => (
          <BacktestCard key={bt.id} bt={bt} onDelete={() => deleteMutation.mutate(bt.id)} />
        ))}
      </div>
    </div>
  );
}
