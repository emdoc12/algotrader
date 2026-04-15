import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "@/lib/queryClient";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { ArrowUpDown } from "lucide-react";
import type { Trade } from "@shared/schema";

export default function Trades() {
  const { data: trades = [], isLoading } = useQuery<Trade[]>({
    queryKey: ["/api/trades"],
    queryFn: () => apiRequest("GET", "/api/trades?limit=200").then((r) => r.json()),
    refetchInterval: 10000,
  });

  const statusColor = (status: string) => {
    switch (status) {
      case "filled": return "bg-emerald-600 hover:bg-emerald-700";
      case "rejected": return "bg-red-600 hover:bg-red-700";
      case "cancelled": return "bg-yellow-600 hover:bg-yellow-700";
      default: return "";
    }
  };

  const actionColor = (action: string) => {
    if (action.includes("BUY")) return "text-emerald-500";
    if (action.includes("SELL")) return "text-red-400";
    return "";
  };

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Trade History</h1>
        <p className="text-sm text-muted-foreground mt-1">All orders placed by the bot</p>
      </div>

      <Card>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="p-6 space-y-3">
              {[...Array(5)].map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : trades.length === 0 ? (
            <div className="py-12 text-center">
              <ArrowUpDown className="h-10 w-10 mx-auto text-muted-foreground/50 mb-4" />
              <p className="text-muted-foreground">No trades yet.</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Action</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead className="text-right">Qty</TableHead>
                    <TableHead className="text-right">Price</TableHead>
                    <TableHead className="text-right">P&L</TableHead>
                    <TableHead>Platform</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Time</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {trades.map((t) => (
                    <TableRow key={t.id} data-testid={`trade-row-${t.id}`}>
                      <TableCell className="font-mono font-medium">{t.symbol}</TableCell>
                      <TableCell>
                        <span className={`text-sm font-medium ${actionColor(t.action)}`}>
                          {t.action.replace(/_/g, " ")}
                        </span>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-xs">
                          {t.instrumentType}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right font-mono">{t.quantity}</TableCell>
                      <TableCell className="text-right font-mono">
                        {t.price != null ? `$${t.price.toFixed(2)}` : "—"}
                      </TableCell>
                      <TableCell className="text-right font-mono">
                        {t.pnl != null ? (
                          <span className={t.pnl >= 0 ? "text-emerald-500" : "text-red-400"}>
                            {t.pnl >= 0 ? "+" : ""}${t.pnl.toFixed(2)}
                          </span>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-xs">{t.platform}</Badge>
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant={t.status === "pending" ? "secondary" : "default"}
                          className={statusColor(t.status)}
                        >
                          {t.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                        {t.executedAt
                          ? new Date(t.executedAt).toLocaleString()
                          : t.createdAt
                          ? new Date(t.createdAt).toLocaleString()
                          : "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
