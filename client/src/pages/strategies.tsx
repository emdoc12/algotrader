import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { apiRequest, queryClient } from "@/lib/queryClient";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Slider } from "@/components/ui/slider";
import { useToast } from "@/hooks/use-toast";
import { Plus, Trash2, Settings2, Zap, ZapOff, FlaskConical, Radio } from "lucide-react";
import { useLocation } from "wouter";
import type { Strategy, Account } from "@shared/schema";

const STRATEGY_TYPES = [
  { value: "short_put", label: "Short Put (Naked)", description: "Sell OTM puts to collect premium or acquire shares" },
  { value: "credit_spread", label: "Credit Spread", description: "Sell put/call spreads for defined-risk income" },
  { value: "covered_call", label: "Covered Call", description: "Sell calls against existing long positions" },
  { value: "iron_condor", label: "Iron Condor", description: "Sell put + call spreads on the same underlying" },
  { value: "crypto_momentum", label: "Crypto Momentum", description: "Buy breakouts, sell breakdowns on crypto" },
  { value: "crypto_mean_reversion", label: "Crypto Mean Reversion", description: "Buy dips, sell rips based on moving averages" },
  { value: "options_flow_scanner", label: "Options Flow Scanner", description: "Stream Bullflow alerts and auto-buy calls on high-score signals" },
  { value: "custom", label: "Custom Rules", description: "Define your own entry/exit rules" },
];

const DEFAULT_PARAMS: Record<string, object> = {
  short_put: { minDTE: 30, maxDTE: 60, targetDelta: 0.30, minPOP: 65, minPremium: 0.50 },
  credit_spread: { minDTE: 30, maxDTE: 60, shortDelta: 0.30, width: 5, minCredit: 0.80 },
  covered_call: { minDTE: 20, maxDTE: 45, targetDelta: 0.30, minPremium: 0.30 },
  iron_condor: { minDTE: 30, maxDTE: 55, shortDelta: 0.16, width: 5, minCredit: 1.50 },
  crypto_momentum: { symbol: "ETH", maPeriod: 20, breakoutPercent: 2, stopLossPercent: 3, takeProfitPercent: 6, initialCapital: 10000 },
  crypto_mean_reversion: { symbol: "ETH", maPeriod: 50, deviationPercent: 5, stopLossPercent: 3, takeProfitPercent: 4, initialCapital: 10000 },
  options_flow_scanner: { minPremium: 25000, minScore: 5, callsOnly: true, excludeEtfs: true, minDTE: 7, maxDTE: 60, maxContracts: 1, execution: "calls" },
  custom: {},
};

export default function Strategies() {
  const { toast } = useToast();
  const [, setLocation] = useLocation();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [newStrategy, setNewStrategy] = useState({
    name: "",
    type: "short_put",
    platform: "tastytrade",
    accountId: 0,
    tradingMode: "paper" as "paper" | "live",
    maxPositionSize: 1,
    maxDailyTrades: 5,
    maxBuyingPowerUsage: 50,
    scanInterval: 300,
  });

  const { data: strategies = [], isLoading } = useQuery<Strategy[]>({
    queryKey: ["/api/strategies"],
    queryFn: () => apiRequest("GET", "/api/strategies").then((r) => r.json()),
  });

  const { data: accounts = [] } = useQuery<Account[]>({
    queryKey: ["/api/accounts"],
    queryFn: () => apiRequest("GET", "/api/accounts").then((r) => r.json()),
  });

  const createMutation = useMutation({
    mutationFn: (data: typeof newStrategy) =>
      apiRequest("POST", "/api/strategies", {
        ...data,
        parameters: JSON.stringify(DEFAULT_PARAMS[data.type] || {}),
        isEnabled: false,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/strategies"] });
      setDialogOpen(false);
      toast({ title: "Strategy created" });
    },
  });

  const toggleMutation = useMutation({
    mutationFn: (id: number) => apiRequest("POST", `/api/strategies/${id}/toggle`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/strategies"] });
      queryClient.invalidateQueries({ queryKey: ["/api/dashboard"] });
    },
  });

  const modeMutation = useMutation({
    mutationFn: ({ id, mode }: { id: number; mode: string }) =>
      apiRequest("PATCH", `/api/strategies/${id}`, { tradingMode: mode }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/strategies"] });
      toast({ title: "Trading mode updated" });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => apiRequest("DELETE", `/api/strategies/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/strategies"] });
      toast({ title: "Strategy deleted" });
    },
  });

  const getAccountName = (id: number) => accounts.find((a) => a.id === id)?.name ?? "Unknown";

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Strategies</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Configure and manage your automated trading strategies
          </p>
        </div>
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogTrigger asChild>
            <Button data-testid="button-add-strategy">
              <Plus className="h-4 w-4 mr-2" /> New Strategy
            </Button>
          </DialogTrigger>
          <DialogContent className="max-w-lg">
            <DialogHeader>
              <DialogTitle>Create Strategy</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 pt-2">
              <div>
                <Label>Strategy Name</Label>
                <Input
                  data-testid="input-strategy-name"
                  placeholder="e.g. SNDK Short Puts"
                  value={newStrategy.name}
                  onChange={(e) => setNewStrategy({ ...newStrategy, name: e.target.value })}
                />
              </div>
              <div>
                <Label>Type</Label>
                <Select
                  value={newStrategy.type}
                  onValueChange={(v) => setNewStrategy({ ...newStrategy, type: v })}
                >
                  <SelectTrigger data-testid="select-strategy-type">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {STRATEGY_TYPES.map((t) => (
                      <SelectItem key={t.value} value={t.value}>
                        {t.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground mt-1">
                  {STRATEGY_TYPES.find((t) => t.value === newStrategy.type)?.description}
                </p>
              </div>
              <div>
                <Label>Platform</Label>
                <Select
                  value={newStrategy.platform}
                  onValueChange={(v) => setNewStrategy({ ...newStrategy, platform: v })}
                >
                  <SelectTrigger data-testid="select-platform">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="tastytrade">Tastytrade</SelectItem>
                    <SelectItem value="tasty_crypto">Tasty Crypto</SelectItem>
                    <SelectItem value="kraken">Kraken</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label>Account{newStrategy.platform === "kraken" || newStrategy.type === "options_flow_scanner" ? " (auto-configured from env vars)" : ""}</Label>
                <Select
                  value={String(newStrategy.accountId || "")}
                  onValueChange={(v) => setNewStrategy({ ...newStrategy, accountId: Number(v) })}
                >
                  <SelectTrigger data-testid="select-account">
                    <SelectValue placeholder="Select account" />
                  </SelectTrigger>
                  <SelectContent>
                    {accounts.map((a) => (
                      <SelectItem key={a.id} value={String(a.id)}>
                        {a.name} ({a.accountNumber})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label>Trading Mode</Label>
                <Select
                  value={newStrategy.tradingMode}
                  onValueChange={(v) => setNewStrategy({ ...newStrategy, tradingMode: v as "paper" | "live" })}
                >
                  <SelectTrigger data-testid="select-trading-mode">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="paper">Paper Trading (simulated)</SelectItem>
                    <SelectItem value="live">Live Trading (real orders)</SelectItem>
                  </SelectContent>
                </Select>
                {newStrategy.tradingMode === "live" && (
                  <p className="text-xs text-amber-400 mt-1">⚠ Live mode places real orders. Make sure DRY_RUN=false in your container.</p>
                )}
              </div>
              <div>
                <Label>Max Buying Power Usage: {newStrategy.maxBuyingPowerUsage}%</Label>
                <Slider
                  data-testid="slider-buying-power"
                  value={[newStrategy.maxBuyingPowerUsage]}
                  onValueChange={([v]) => setNewStrategy({ ...newStrategy, maxBuyingPowerUsage: v })}
                  min={5}
                  max={100}
                  step={5}
                  className="mt-2"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label>Max Position Size</Label>
                  <Input
                    data-testid="input-max-position"
                    type="number"
                    value={newStrategy.maxPositionSize}
                    onChange={(e) =>
                      setNewStrategy({ ...newStrategy, maxPositionSize: Number(e.target.value) })
                    }
                  />
                </div>
                <div>
                  <Label>Max Daily Trades</Label>
                  <Input
                    data-testid="input-max-daily"
                    type="number"
                    value={newStrategy.maxDailyTrades}
                    onChange={(e) =>
                      setNewStrategy({ ...newStrategy, maxDailyTrades: Number(e.target.value) })
                    }
                  />
                </div>
              </div>
              <Button
                data-testid="button-create-strategy"
                className="w-full"
                onClick={() => createMutation.mutate(newStrategy)}
                disabled={!newStrategy.name || (["tastytrade","tasty_crypto"].includes(newStrategy.platform) && !newStrategy.accountId) || createMutation.isPending}
              >
                {createMutation.isPending ? "Creating..." : "Create Strategy"}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      {isLoading && (
        <div className="grid gap-4 md:grid-cols-2">
          {[1, 2].map((i) => (
            <Card key={i} className="animate-pulse h-40" />
          ))}
        </div>
      )}

      {!isLoading && strategies.length === 0 && (
        <Card>
          <CardContent className="py-12 text-center">
            <Settings2 className="h-10 w-10 mx-auto text-muted-foreground/50 mb-4" />
            <p className="text-muted-foreground">No strategies yet. Create one to get started.</p>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        {strategies.map((s) => {
          const params = s.parameters ? JSON.parse(s.parameters) : {};
          const typeInfo = STRATEGY_TYPES.find((t) => t.value === s.type);
          return (
            <Card key={s.id} data-testid={`card-strategy-${s.id}`}>
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between">
                  <CardTitle className="text-base font-semibold">{s.name}</CardTitle>
                  <div className="flex items-center gap-2">
                    <Switch
                      data-testid={`switch-strategy-${s.id}`}
                      checked={s.isEnabled}
                      onCheckedChange={() => toggleMutation.mutate(s.id)}
                    />
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8 text-destructive"
                      data-testid={`button-delete-strategy-${s.id}`}
                      onClick={() => deleteMutation.mutate(s.id)}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="flex gap-2 flex-wrap items-center">
                  <Badge variant="outline">{typeInfo?.label ?? s.type}</Badge>
                  <Badge variant="outline">{s.platform}</Badge>
                  <Badge variant={s.isEnabled ? "default" : "secondary"} className={s.isEnabled ? "bg-emerald-600" : ""}>
                    {s.isEnabled ? (
                      <><Zap className="h-3 w-3 mr-1" /> Active</>
                    ) : (
                      <><ZapOff className="h-3 w-3 mr-1" /> Paused</>
                    )}
                  </Badge>
                  {/* Paper / Live mode toggle */}
                  <button
                    data-testid={`button-mode-${s.id}`}
                    onClick={() => modeMutation.mutate({ id: s.id, mode: s.tradingMode === "live" ? "paper" : "live" })}
                    className={`flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border font-medium transition-colors ${
                      s.tradingMode === "live"
                        ? "border-amber-500/60 text-amber-400 bg-amber-500/10 hover:bg-amber-500/20"
                        : "border-border text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {s.tradingMode === "live" ? (
                      <><Radio className="h-3 w-3" /> Live</>
                    ) : (
                      <><FlaskConical className="h-3 w-3" /> Paper</>
                    )}
                  </button>
                </div>
                <div className="text-sm text-muted-foreground">
                  Account: {getAccountName(s.accountId)}
                </div>
                <div className="grid grid-cols-3 gap-3 text-sm">
                  <div>
                    <span className="text-muted-foreground block text-xs">Max Position</span>
                    <span className="font-mono">{s.maxPositionSize}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block text-xs">Daily Limit</span>
                    <span className="font-mono">{s.maxDailyTrades}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block text-xs">BP Usage</span>
                    <span className="font-mono">{s.maxBuyingPowerUsage}%</span>
                  </div>
                </div>
                {Object.keys(params).length > 0 && (
                  <div className="bg-muted/50 rounded-md p-3 space-y-1">
                    <span className="text-xs font-medium text-muted-foreground">Parameters</span>
                    <div className="grid grid-cols-2 gap-1 text-xs">
                      {Object.entries(params).map(([key, val]) => (
                        <div key={key} className="flex justify-between">
                          <span className="text-muted-foreground">{key}:</span>
                          <span className="font-mono">{String(val)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {["crypto_momentum", "crypto_mean_reversion"].includes(s.type) && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="w-full text-xs"
                    data-testid={`button-backtest-${s.id}`}
                    onClick={() => setLocation("/backtests")}
                  >
                    <FlaskConical className="h-3.5 w-3.5 mr-1.5" /> Run Backtest
                  </Button>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
