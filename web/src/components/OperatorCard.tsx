// Personal voice tagging status and the operator sign-in. The sign-in is a plain form post: the credential goes
// straight to the server, which answers with an HttpOnly session cookie; no script reads or keeps either.
import type { OperatorSession } from "../lib/operator";
import { regionLabel } from "../lib/regions";

interface Props {
  session: OperatorSession | null;
  region: string;
  accountRef?: string; // "…9012" from the identity tool, when it has been asked
  onSignOut: () => void;
  onKill: () => void;
  compact?: boolean;
}

export function OperatorCard({ session, region, accountRef, onSignOut, onKill, compact }: Props) {
  if (!session) {
    return (
      <section className="card operator-box" aria-label="Operator voice tagging">
        <h3>Personal voice tagging <span className="pill muted-pill">Off</span></h3>
        <p className="small muted">Not enabled on this server (<code>CLOUDGUARD_VOICE_TAGGING_ENABLED</code>). Spoken and typed tagging requests stay preview-only.</p>
      </section>
    );
  }
  const expires = session.expires_at ? new Date(session.expires_at).toLocaleTimeString() : "?";
  return (
    <section className="card operator-box" aria-label="Operator voice tagging">
      <h3>Personal voice tagging
        {session.kill_switch ? <span className="pill bad">Kill switch on</span>
          : session.authenticated ? <span className="pill ok">Ready</span> : <span className="pill muted-pill">Signed out</span>}
      </h3>
      {session.kill_switch ? (
        <p className="small"><span className="pill muted-pill">Disabled</span> The server's kill switch is engaged. Every voice-tagging request is refused; remove the kill file on the server to re-enable.</p>
      ) : session.authenticated ? (
        <>
          <p className="small"><span className="pill ok">Operator signed in</span> {session.operator} · {session.mode} · until {expires}</p>
          {!compact && (
            <dl className="kv">
              <dt>Environment</dt><dd>{session.mode === "fixture" ? "Fixture (simulated account, no AWS)" : session.mode === "live" ? "Live AWS account" : session.mode}</dd>
              <dt>Account</dt><dd>{accountRef ? `account ${accountRef}` : "verified by the server with STS on every request"}</dd>
              <dt>Preferred region</dt><dd>{regionLabel(region)} ({region}); asked for each request</dd>
              <dt>Session</dt><dd>HttpOnly cookie, expires {expires}</dd>
            </dl>
          )}
          <p className="muted small">Spoken tagging requests can now apply tags after you confirm by voice. Typed chat stays preview-only.</p>
          <div className="plan-actions">
            <button className="secondary" onClick={onSignOut}>Sign out</button>
            <button className="secondary danger" onClick={onKill}>Disable voice tagging now</button>
          </div>
        </>
      ) : (
        <form method="post" action="/operator/login" className="connect-form">
          <p className="small muted">Sign in with the dedicated operator credential (not the Alexa token). The page reloads; connect afterwards.</p>
          <label className="field">
            <span className="small">Operator credential</span>
            <input type="password" name="credential" autoComplete="off" spellCheck={false} aria-label="Operator credential" />
          </label>
          <button className="secondary" type="submit">Sign in as operator</button>
        </form>
      )}
    </section>
  );
}
