import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "@/lib/queryClient";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { Briefcase } from "lucide-react";
import type { Position } from "@shared/schema";

export default function Positions() {
  const { data: positions = [], isLoading } = useQuery<Position[]>({
    queryKey: ["/api/positions"],
    queryFn: () => apiRequest("GET", "/api/positions").then((r) => r.json()),
    refetchInterval: 10000,
  });

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Open Positions</h1>
        <p className="text-sm text-muted-foreground mt-1">Current portfolio holdings across all accounts</p>
      </div>

      <Card>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="p-6 space-y-3">
              {[...Array(3)].map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : positions.length === 0 ? (
            <div className="py-12 text-center">
              <Briefcase className="h-10 w-10 mx-auto text-muted-foreground/50 mb-4" />
              <p className="text-muted-foreground">No open positions.</p>
              <p className="text-xs text-muted-foreground mt-1">
                Positions will appear here once the bot syncs with your Tastytrade account.
              </p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead className="text-right">Qty</TableHead>
                    <TableHead className="text-right">Avg Price</TableHead>
                    <TableHead className="text-right">Current</TableHead>
                    <TableHead className="text-right">Market Value</TableHead>
                    <TableHead className="text-right">Unrealized P&L</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {positions.map((p) => (
                    <TableRow key={p.id} data-testid={`position-row-${p.id}`}>
                      <TableCell className="font-mono font-medium">{p.symbol}</TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-xs">{p.instrumentType}</Badge>
                      </TableCell>
                      <TableCell className="text-right font-mono">{p.quantity}</TableCell>
                      <TableCell className="text-right font-mono">
                        ${p.averagePrice.toFixed(2)}
                      </TableCell>
                      <TableCell className="text-right font-mono">
                        {p.currentPrice != null ? `$${p.currentPrice.toFixed(2)}` : "—"}
                      </TableCell>
                      <TableCell className="text-right font-mono">
                        {p.marketValue != null ? `$${p.marketValue.toFixed(2)}` : "—"}
                      </TableCell>
                      <TableCell className="text-right font-mono">
                        {p.unrealizedPnl != null ? (
                          <span className={p.unrealizedPnl >= 0 ? "text-emerald-500" : "text-red-400"}>
                            {p.unrealizedPnl >= 0 ? "+" : ""}${p.unrealizedPnl.toFixed(2)}
                          </span>
                        ) : (
                          "—"
                        )}
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
