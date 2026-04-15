import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "@/lib/queryClient";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Activity,
  TrendingUp,
  TrendingDown,
  DollarSign,
  BarChart3,
  Zap,
  Briefcase,
  Clock,
} from "lucide-react";

interface DashboardData {
  accounts: number;
  activeStrategies: number;
  totalStrategies: number;
  todaysTrades: number;
  totalTrades: number;
  realizedPnl: number;
  unrealizedPnl: number;
  openPositions: number;
  recentTrades: Array<{
    id: number;
    symbol: string;
    action: string;
    quantity: number;
    price: number | null;
    status: string;
    platform: string;
    executedAt: string | null;
    createdAt: string;
  }>;
  recentLogs: Array<{
    id: number;
    level: string;
    message: string;
    createdAt: string;
  }>;
  strategies: Array<{
    id: number;
    name: string;
    type: string;
    isEnabled: boolean;
    platform: string;
  }>;
}

export default function Dashboard() {
  const { data, isLoading } = useQuery<DashboardData>({
    queryKey: ["/api/dashboard"],
    queryFn: () => apiRequest("GET", "/api/dashboard").then((r) => r.json()),
    refetchInterval: 10000,
  });

  if (isLoading) {
    return (
      <div className="p-6 space-y-6">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          {[...Array(4)].map((_, i) => (
            <Skeleton key={i} className="h-28 rounded-lg" />
          ))}
        </div>
        <div className="grid lg:grid-cols-2 gap-6">
          <Skeleton className="h-80 rounded-lg" />
          <Skeleton className="h-80 rounded-lg" />
        </div>
      </div>
    );
  }

  const stats = [
    {
      label: "Active Strategies",
      value: `${data?.activeStrategies ?? 0} / ${data?.totalStrategies ?? 0}`,
      icon: Zap,
      color: "text-emerald-500",
    },
    {
      label: "Today's Trades",
      value: data?.todaysTrades ?? 0,
      icon: Activity,
      color: "text-blue-500",
    },
    {
      label: "Realized P&L",
      value: `$${(data?.realizedPnl ?? 0).toFixed(2)}`,
      icon: (data?.realizedPnl ?? 0) >= 0 ? TrendingUp : TrendingDown,
      color: (data?.realizedPnl ?? 0) >= 0 ? "text-emerald-500" : "text-red-500",
    },
    {
      label: "Open Positions",
      value: data?.openPositions ?? 0,
      icon: Briefcase,
      color: "text-purple-500",
    },
  ];

  return (
    <div className="p-6 space-y-6">
      {/* Stats Grid */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {stats.map((stat) => (
          <Card key={stat.label} data-testid={`stat-${stat.label.toLowerCase().replace(/\s+/g, "-")}`}>
            <CardContent className="p-4">
              <div className="flex items-center justify-between mb-2">
                <span className="text-sm text-muted-foreground">{stat.label}</span>
                <stat.icon className={`h-4 w-4 ${stat.color}`} />
              </div>
              <div className="text-xl font-semibold">{stat.value}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid lg:grid-cols-2 gap-6">
        {/* Strategies */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base font-semibold flex items-center gap-2">
              <BarChart3 className="h-4 w-4" /> Strategies
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {data?.strategies?.length === 0 && (
              <p className="text-sm text-muted-foreground py-4 text-center">
                No strategies configured. Add one from the Strategies page.
              </p>
            )}
            {data?.strategies?.map((s) => (
              <div
                key={s.id}
                className="flex items-center justify-between p-3 rounded-md bg-muted/40"
                data-testid={`strategy-row-${s.id}`}
              >
                <div>
                  <span className="text-sm font-medium">{s.name}</span>
                  <div className="flex gap-2 mt-1">
                    <Badge variant="outline" className="text-xs">
                      {s.type.replace(/_/g, " ")}
                    </Badge>
                    <Badge variant="outline" className="text-xs">
                      {s.platform}
                    </Badge>
                  </div>
                </div>
                <Badge
                  variant={s.isEnabled ? "default" : "secondary"}
                  className={s.isEnabled ? "bg-emerald-600 hover:bg-emerald-700" : ""}
                >
                  {s.isEnabled ? "Active" : "Paused"}
                </Badge>
              </div>
            ))}
          </CardContent>
        </Card>

        {/* Recent Trades */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base font-semibold flex items-center gap-2">
              <DollarSign className="h-4 w-4" /> Recent Trades
            </CardTitle>
          </CardHeader>
          <CardContent>
            {data?.recentTrades?.length === 0 && (
              <p className="text-sm text-muted-foreground py-4 text-center">
                No trades yet. Activate a strategy to start.
              </p>
            )}
            <div className="space-y-2">
              {data?.recentTrades?.map((t) => (
                <div
                  key={t.id}
                  className="flex items-center justify-between p-3 rounded-md bg-muted/40"
                  data-testid={`trade-row-${t.id}`}
                >
                  <div>
                    <span className="text-sm font-medium font-mono">{t.symbol}</span>
                    <div className="flex gap-2 mt-1">
                      <Badge variant="outline" className="text-xs">
                        {t.action.replace(/_/g, " ")}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        x{t.quantity}
                      </span>
                    </div>
                  </div>
                  <div className="text-right">
                    <Badge
                      variant={
                        t.status === "filled"
                          ? "default"
                          : t.status === "rejected"
                          ? "destructive"
                          : "secondary"
                      }
                      className={
                        t.status === "filled" ? "bg-emerald-600 hover:bg-emerald-700" : ""
                      }
                    >
                      {t.status}
                    </Badge>
                    {t.price && (
                      <div className="text-xs text-muted-foreground mt-1 font-mono">
                        ${t.price.toFixed(2)}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Activity Log */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base font-semibold flex items-center gap-2">
            <Clock className="h-4 w-4" /> Bot Activity Log
          </CardTitle>
        </CardHeader>
        <CardContent>
          {data?.recentLogs?.length === 0 && (
            <p className="text-sm text-muted-foreground py-4 text-center">
              No activity yet.
            </p>
          )}
          <div className="space-y-1 max-h-64 overflow-y-auto">
            {data?.recentLogs?.map((log) => (
              <div
                key={log.id}
                className="flex items-start gap-3 p-2 rounded text-sm"
                data-testid={`log-row-${log.id}`}
              >
                <Badge
                  variant="outline"
                  className={`text-xs shrink-0 mt-0.5 ${
                    log.level === "error"
                      ? "border-red-500/50 text-red-500"
                      : log.level === "warn"
                      ? "border-yellow-500/50 text-yellow-500"
                      : log.level === "trade"
                      ? "border-emerald-500/50 text-emerald-500"
                      : "border-muted-foreground/30"
                  }`}
                >
                  {log.level}
                </Badge>
                <span className="text-muted-foreground">{log.message}</span>
                <span className="text-xs text-muted-foreground/60 ml-auto shrink-0">
                  {log.createdAt ? new Date(log.createdAt).toLocaleTimeString() : ""}
                </span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
