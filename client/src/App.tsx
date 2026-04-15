import { Switch, Route, Router, Link, useLocation } from "wouter";
import { useHashLocation } from "wouter/use-hash-location";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./lib/queryClient";
import { Toaster } from "@/components/ui/toaster";
import { ThemeProvider, useTheme } from "@/lib/theme";
import Dashboard from "@/pages/dashboard";
import Strategies from "@/pages/strategies";
import Accounts from "@/pages/accounts";
import Trades from "@/pages/trades";
import Positions from "@/pages/positions";
import Logs from "@/pages/logs";
import NotFound from "@/pages/not-found";
import {
  LayoutDashboard,
  Settings2,
  Wallet,
  ArrowUpDown,
  Briefcase,
  ScrollText,
  Sun,
  Moon,
  Bot,
} from "lucide-react";
import { Button } from "@/components/ui/button";

const NAV_ITEMS = [
  { path: "/", label: "Dashboard", icon: LayoutDashboard },
  { path: "/strategies", label: "Strategies", icon: Settings2 },
  { path: "/accounts", label: "Accounts", icon: Wallet },
  { path: "/trades", label: "Trades", icon: ArrowUpDown },
  { path: "/positions", label: "Positions", icon: Briefcase },
  { path: "/logs", label: "Logs", icon: ScrollText },
];

function Sidebar() {
  const [location] = useLocation();
  const { theme, toggleTheme } = useTheme();

  return (
    <aside className="w-56 shrink-0 bg-sidebar text-sidebar-foreground border-r border-sidebar-border flex flex-col h-screen sticky top-0">
      {/* Logo */}
      <div className="p-4 border-b border-sidebar-border">
        <div className="flex items-center gap-2.5">
          <div className="h-8 w-8 rounded-lg bg-primary/20 flex items-center justify-center">
            <Bot className="h-4.5 w-4.5 text-primary" />
          </div>
          <div>
            <span className="text-sm font-semibold tracking-tight">AlgoTrader</span>
            <span className="block text-xs text-sidebar-foreground/50">Tastytrade + Crypto</span>
          </div>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 p-2 space-y-0.5">
        {NAV_ITEMS.map((item) => {
          const isActive = location === item.path || (item.path !== "/" && location.startsWith(item.path));
          return (
            <Link key={item.path} href={item.path}>
              <div
                className={`flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors cursor-pointer ${
                  isActive
                    ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
                    : "text-sidebar-foreground/70 hover:text-sidebar-foreground hover:bg-sidebar-accent/50"
                }`}
                data-testid={`nav-${item.label.toLowerCase()}`}
              >
                <item.icon className="h-4 w-4 shrink-0" />
                {item.label}
              </div>
            </Link>
          );
        })}
      </nav>

      {/* Theme toggle */}
      <div className="p-3 border-t border-sidebar-border">
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-start gap-2 text-sidebar-foreground/60 hover:text-sidebar-foreground"
          onClick={toggleTheme}
          data-testid="button-theme-toggle"
        >
          {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          {theme === "dark" ? "Light Mode" : "Dark Mode"}
        </Button>
      </div>
    </aside>
  );
}

function AppContent() {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 min-w-0 overflow-auto">
        <Switch>
          <Route path="/" component={Dashboard} />
          <Route path="/strategies" component={Strategies} />
          <Route path="/accounts" component={Accounts} />
          <Route path="/trades" component={Trades} />
          <Route path="/positions" component={Positions} />
          <Route path="/logs" component={Logs} />
          <Route component={NotFound} />
        </Switch>
      </main>
    </div>
  );
}

function App() {
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <Router hook={useHashLocation}>
          <AppContent />
        </Router>
        <Toaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}

export default App;
