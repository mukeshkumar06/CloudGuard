// The application shell: sidebar navigation and the top bar. Presentation only: every status shown here comes from
// the live connection, /healthz (mode) or the server's operator-session status; nothing is invented or stored.
import type { Connection } from "../lib/mcp";
import type { OperatorSession } from "../lib/operator";
import { REGION_OPTIONS, regionLabel } from "../lib/regions";
import type { Page } from "../pages/types";
import { Icon, type IconName } from "../ui/kit";

export const NAV: { id: Page; label: string; icon: IconName; section?: string }[] = [
  { id: "overview", label: "Overview", icon: "overview", section: "Monitor" },
  { id: "cost", label: "Cost Intelligence", icon: "cost" },
  { id: "governance", label: "Resource Governance", icon: "governance" },
  { id: "assistant", label: "Voice Assistant", icon: "voice", section: "Act" },
  { id: "tagging", label: "Tag Management", icon: "tag" },
  { id: "activity", label: "Activity & Audit", icon: "activity", section: "Account" },
  { id: "settings", label: "Settings", icon: "settings" },
  { id: "developer", label: "MCP Explorer", icon: "developer", section: "Developer" },
];

export type ConnStatus = "disconnected" | "connecting" | "connected";

/** The kill switch as the server reports it. null: voice tagging isn't enabled on the server at all. */
export function killSwitchState(session: OperatorSession | null): { label: string; tone: "ok" | "bad" | "muted" } {
  if (!session) return { label: "Voice tagging off on server", tone: "muted" };
  if (session.kill_switch) return { label: "Kill switch engaged", tone: "bad" };
  return { label: "Kill switch not engaged", tone: "ok" };
}

export function Logo() {
  return (
    <span className="logo" aria-hidden>
      <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M12 2 4 5v6.1c0 5 3.4 9.7 8 10.9 4.6-1.2 8-5.9 8-10.9V5l-8-3Zm-1.3 14.2-3.5-3.5 1.4-1.4 2.1 2.1 4.9-4.9 1.4 1.4-6.3 6.3Z" /></svg>
    </span>
  );
}

interface SidebarProps {
  page: Page;
  go: (p: Page) => void;
  status: ConnStatus;
  conn: Connection | null;
  session: OperatorSession | null;
  collapsed: boolean;
  setCollapsed: (c: boolean) => void;
  open: boolean;
  badges: Partial<Record<Page, { text: string; tone: string }>>;
}

export function Sidebar({ page, go, status, conn, session, collapsed, setCollapsed, open, badges }: SidebarProps) {
  const kill = killSwitchState(session);
  let section: string | undefined;
  return (
    <aside className={`sidebar ${open ? "open" : ""} ${collapsed ? "collapsed" : ""}`} aria-label="Sidebar">
      <div className="brand">
        <Logo />
        <div className="brand-text"><strong>CloudGuard AI</strong><span className="muted small">AWS governance console</span></div>
        <button type="button" className="icon-btn collapse-btn" aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
                aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)} data-tip={collapsed ? "Expand" : undefined}>
          <Icon name={collapsed ? "expand" : "collapse"} />
        </button>
      </div>
      {conn ? (
        <nav className="nav" aria-label="Main">
          {NAV.map((n) => {
            const head = n.section && n.section !== section ? n.section : undefined;
            if (n.section) section = n.section;
            const badge = badges[n.id];
            return (
              <div key={n.id}>
                {head && <p className="label nav-title">{head}</p>}
                <button aria-current={page === n.id ? "page" : undefined} onClick={() => go(n.id)}
                        data-tip={collapsed ? n.label : undefined}>
                  <Icon name={n.icon} /><span className="nav-label">{n.label}</span>
                  {badge && <span className={`pill ${badge.tone} nav-badge`}>{badge.text}</span>}
                </button>
              </div>
            );
          })}
        </nav>
      ) : (
        <div className="nav-placeholder small muted">Connect to an MCP server to open the console.</div>
      )}
      <div className="sidebar-foot">
        <div className={`status-row conn-pill ${status}`} role="status" data-tip={collapsed ? (status === "connected" ? "Connected" : "Not connected") : undefined}>
          {status === "connected" ? "● Connected" : status === "connecting" ? "◌ Connecting…" : "○ Not connected"}
        </div>
        {conn && <div className="status-row small muted sidebar-detail">{conn.serverName} {conn.serverVersion} · protocol {conn.protocolVersion ?? "?"}</div>}
        <div className={`status-row kill ${kill.tone}`} aria-label="Kill switch status" data-tip={collapsed ? kill.label : undefined}>
          <Icon name="power" className="row-icon" /><span className="nav-label">{kill.label}</span>
        </div>
        <p className="disclaimer small">Custom MCP client for the CloudGuard demo. Not Alexa+, not an Amazon product.</p>
      </div>
    </aside>
  );
}

interface TopbarProps {
  page: Page;
  conn: Connection;
  mode?: string;
  region: string;
  setRegion: (r: string) => void;
  session: OperatorSession | null;
  menuOpen: boolean;
  setMenuOpen: (o: boolean) => void;
  onDisconnect: () => void;
}

export function Topbar({ page, conn, mode, region, setRegion, session, menuOpen, setMenuOpen, onDisconnect }: TopbarProps) {
  const item = NAV.find((n) => n.id === page);
  const operator = !session ? null : session.kill_switch ? { text: "Voice tagging disabled", tone: "bad" }
    : session.authenticated ? { text: `Operator · ${session.operator}`, tone: "ok" } : { text: "Operator signed out", tone: "muted-pill" };
  return (
    <header className="topbar">
      <div className="topbar-left">
        <button className="icon-btn menu-toggle" aria-label="Menu" aria-expanded={menuOpen} onClick={() => setMenuOpen(!menuOpen)}><Icon name="menu" /></button>
        <nav aria-label="Breadcrumb" className="crumbs">
          <span className="muted">CloudGuard</span>{item?.section && <><span className="crumb-sep" aria-hidden>/</span><span className="muted">{item.section}</span></>}
          <span className="crumb-sep" aria-hidden>/</span><strong aria-current="page">{item?.label}</strong>
        </nav>
      </div>
      <div className="topbar-right">
        <span className="chip-status ok" title={`${conn.serverName} ${conn.serverVersion} · protocol ${conn.protocolVersion ?? "?"} · ${conn.tools.length} tools`}>
          <span className="dot ok" aria-hidden />MCP connected</span>
        <span className={`chip-status ${mode === "fixture" ? "fixture" : mode === "live" ? "live" : "bad"}`}
              title={mode === "fixture" ? "Synthetic fixture account; no AWS API is called" : mode === "live" ? "The server's real AWS account, read-only" : "/healthz could not be read"}>
          <Icon name="server" className="row-icon" />{mode === "fixture" ? "Fixture" : mode === "live" ? "Live AWS" : "Mode unknown"}
        </span>
        <label className="chip-status region-chip" title="Preferred region: offered first when CloudGuard asks which region to search. Never used without asking.">
          <Icon name="globe" className="row-icon" />
          <select value={region} onChange={(e) => setRegion(e.target.value)} aria-label="Preferred region">
            {REGION_OPTIONS.map((r) => <option key={r.code} value={r.code}>{r.label} ({r.code})</option>)}
          </select>
          <span className="sr-only">{regionLabel(region)}</span>
        </label>
        {operator && <span className={`chip-status ${operator.tone}`}><Icon name="user" className="row-icon" />{operator.text}</span>}
        <button className="secondary" onClick={onDisconnect}>Disconnect</button>
      </div>
    </header>
  );
}
