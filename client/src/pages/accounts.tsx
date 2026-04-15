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
import { useToast } from "@/hooks/use-toast";
import { Plus, Trash2, Shield, Wifi, WifiOff } from "lucide-react";
import type { Account } from "@shared/schema";

export default function Accounts() {
  const { toast } = useToast();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [newAccount, setNewAccount] = useState({
    name: "",
    platform: "tastytrade",
    username: "",
    accountNumber: "",
    isSandbox: false,
  });

  const { data: accounts = [], isLoading } = useQuery<Account[]>({
    queryKey: ["/api/accounts"],
    queryFn: () => apiRequest("GET", "/api/accounts").then((r) => r.json()),
  });

  const createMutation = useMutation({
    mutationFn: (data: typeof newAccount) => apiRequest("POST", "/api/accounts", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/accounts"] });
      setDialogOpen(false);
      setNewAccount({ name: "", platform: "tastytrade", username: "", accountNumber: "", isSandbox: false });
      toast({ title: "Account added" });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => apiRequest("DELETE", `/api/accounts/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/accounts"] });
      toast({ title: "Account removed" });
    },
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, isActive }: { id: number; isActive: boolean }) =>
      apiRequest("PATCH", `/api/accounts/${id}`, { isActive }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["/api/accounts"] }),
  });

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Accounts</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Manage your Tastytrade and Tasty Crypto connections
          </p>
        </div>
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogTrigger asChild>
            <Button data-testid="button-add-account">
              <Plus className="h-4 w-4 mr-2" /> Add Account
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Add Trading Account</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 pt-2">
              <div>
                <Label>Display Name</Label>
                <Input
                  data-testid="input-account-name"
                  placeholder="e.g. My Margin Account"
                  value={newAccount.name}
                  onChange={(e) => setNewAccount({ ...newAccount, name: e.target.value })}
                />
              </div>
              <div>
                <Label>Platform</Label>
                <Select
                  value={newAccount.platform}
                  onValueChange={(v) => setNewAccount({ ...newAccount, platform: v })}
                >
                  <SelectTrigger data-testid="select-account-platform">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="tastytrade">Tastytrade</SelectItem>
                    <SelectItem value="tasty_crypto">Tasty Crypto</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label>Username / Email</Label>
                <Input
                  data-testid="input-account-username"
                  placeholder="your-email@example.com"
                  value={newAccount.username}
                  onChange={(e) => setNewAccount({ ...newAccount, username: e.target.value })}
                />
              </div>
              <div>
                <Label>Account Number</Label>
                <Input
                  data-testid="input-account-number"
                  placeholder="5WX01234"
                  value={newAccount.accountNumber}
                  onChange={(e) => setNewAccount({ ...newAccount, accountNumber: e.target.value })}
                />
              </div>
              <div className="flex items-center justify-between">
                <div>
                  <Label>Sandbox Mode</Label>
                  <p className="text-xs text-muted-foreground">Use certification/test environment</p>
                </div>
                <Switch
                  data-testid="switch-sandbox"
                  checked={newAccount.isSandbox}
                  onCheckedChange={(v) => setNewAccount({ ...newAccount, isSandbox: v })}
                />
              </div>
              <div className="bg-muted/50 rounded-md p-3">
                <div className="flex items-start gap-2">
                  <Shield className="h-4 w-4 text-muted-foreground mt-0.5 shrink-0" />
                  <p className="text-xs text-muted-foreground">
                    Your credentials are stored locally in the SQLite database.
                    Authentication tokens are managed via the Tastytrade SDK session.
                    For production use, connect via OAuth for secure token management.
                  </p>
                </div>
              </div>
              <Button
                data-testid="button-create-account"
                className="w-full"
                onClick={() => createMutation.mutate(newAccount)}
                disabled={!newAccount.name || !newAccount.username || !newAccount.accountNumber || createMutation.isPending}
              >
                {createMutation.isPending ? "Adding..." : "Add Account"}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      {isLoading && (
        <div className="grid gap-4 md:grid-cols-2">
          {[1, 2].map((i) => (
            <Card key={i} className="animate-pulse h-32" />
          ))}
        </div>
      )}

      {!isLoading && accounts.length === 0 && (
        <Card>
          <CardContent className="py-12 text-center">
            <Wifi className="h-10 w-10 mx-auto text-muted-foreground/50 mb-4" />
            <p className="text-muted-foreground">No accounts connected. Add one to start trading.</p>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        {accounts.map((a) => (
          <Card key={a.id} data-testid={`card-account-${a.id}`}>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base font-semibold">{a.name}</CardTitle>
                <div className="flex items-center gap-2">
                  <Switch
                    data-testid={`switch-account-${a.id}`}
                    checked={a.isActive}
                    onCheckedChange={(v) => toggleMutation.mutate({ id: a.id, isActive: v })}
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-destructive"
                    data-testid={`button-delete-account-${a.id}`}
                    onClick={() => deleteMutation.mutate(a.id)}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex gap-2 flex-wrap">
                <Badge variant="outline">{a.platform}</Badge>
                {a.isSandbox && (
                  <Badge variant="outline" className="border-yellow-500/50 text-yellow-600 dark:text-yellow-400">
                    Sandbox
                  </Badge>
                )}
                <Badge
                  variant={a.isActive ? "default" : "secondary"}
                  className={a.isActive ? "bg-emerald-600" : ""}
                >
                  {a.isActive ? (
                    <><Wifi className="h-3 w-3 mr-1" /> Active</>
                  ) : (
                    <><WifiOff className="h-3 w-3 mr-1" /> Disabled</>
                  )}
                </Badge>
              </div>
              <div className="text-sm space-y-1">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Username</span>
                  <span className="font-mono text-sm">{a.username}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Account #</span>
                  <span className="font-mono text-sm">{a.accountNumber}</span>
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
