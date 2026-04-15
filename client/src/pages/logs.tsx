import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "@/lib/queryClient";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ScrollText } from "lucide-react";
import type { BotLog } from "@shared/schema";

export default function Logs() {
  const { data: logs = [], isLoading } = useQuery<BotLog[]>({
    queryKey: ["/api/logs"],
    queryFn: () => apiRequest("GET", "/api/logs?limit=500").then((r) => r.json()),
    refetchInterval: 5000,
  });

  const levelStyle = (level: string) => {
    switch (level) {
      case "error": return "border-red-500/50 text-red-500 bg-red-500/10";
      case "warn": return "border-yellow-500/50 text-yellow-600 dark:text-yellow-400 bg-yellow-500/10";
      case "trade": return "border-emerald-500/50 text-emerald-500 bg-emerald-500/10";
      default: return "border-muted-foreground/30 bg-transparent";
    }
  };

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Bot Logs</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Real-time activity feed from all strategies
        </p>
      </div>

      <Card>
        <CardContent className="p-4">
          {isLoading ? (
            <div className="space-y-2">
              {[...Array(8)].map((_, i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </div>
          ) : logs.length === 0 ? (
            <div className="py-12 text-center">
              <ScrollText className="h-10 w-10 mx-auto text-muted-foreground/50 mb-4" />
              <p className="text-muted-foreground">No logs yet.</p>
            </div>
          ) : (
            <div className="space-y-1 font-mono text-sm max-h-[calc(100vh-220px)] overflow-y-auto">
              {logs.map((log) => (
                <div
                  key={log.id}
                  className="flex items-start gap-3 p-2 rounded hover:bg-muted/40 transition-colors"
                  data-testid={`log-entry-${log.id}`}
                >
                  <span className="text-xs text-muted-foreground/60 shrink-0 pt-0.5 w-20">
                    {log.createdAt ? new Date(log.createdAt).toLocaleTimeString() : ""}
                  </span>
                  <Badge
                    variant="outline"
                    className={`text-xs shrink-0 w-14 justify-center ${levelStyle(log.level)}`}
                  >
                    {log.level.toUpperCase()}
                  </Badge>
                  <span className="text-foreground/90 break-words">{log.message}</span>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
