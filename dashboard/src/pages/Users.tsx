import { useEffect, useState } from "react";
import { Badge, Card, Empty, PageHeader, SectionTitle, TableWrap } from "@/components/ui";
import { ageText } from "@/lib/format";

interface Register {
  users: { email: string; name: string; role: string; created_at: string; disabled: boolean }[];
  signins: { t: string; email: string; ok: boolean; reason: string }[];
}

export default function Users() {
  const [reg, setReg] = useState<Register | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch("/api/data?file=users", { credentials: "same-origin", cache: "no-store" })
      .then(async (r) => {
        if (r.status === 403) throw new Error("Only admins can see this page.");
        if (!r.ok) throw new Error("Could not load the account register.");
        setReg((await r.json()) as Register);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <div className="space-y-6">
      <PageHeader title="Users and access" lead="Everyone signs in as themselves. Accounts are created and disabled from a trusted machine, not from this page." />
      {error && <Empty>{error}</Empty>}
      {reg && (
        <>
          <Card>
            <SectionTitle>Accounts</SectionTitle>
            {reg.users.length ? (
              <TableWrap>
                <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Created</th></tr></thead>
                <tbody>{reg.users.map((u) => (
                  <tr key={u.email}><td>{u.name}</td><td>{u.email}</td><td><Badge tone={u.role === "admin" ? "accent" : "neutral"}>{u.role}</Badge></td>
                    <td><Badge tone={u.disabled ? "fail" : "pass"}>{u.disabled ? "disabled" : "active"}</Badge></td><td>{ageText(u.created_at)}</td></tr>
                ))}</tbody>
              </TableWrap>
            ) : <p className="text-sm text-muted">No accounts yet.</p>}
            <p className="mt-4 text-xs text-faint">Admins see this page. Viewers see everything else.</p>
          </Card>
          <Card>
            <SectionTitle>Recent sign-ins</SectionTitle>
            {reg.signins.length ? (
              <ul className="divide-y divide-line text-sm">{reg.signins.map((s, i) => (
                <li key={i} className="flex flex-wrap items-center gap-2 py-2">
                  <Badge tone={s.ok ? "pass" : "fail"}>{s.ok ? "signed in" : s.reason === "unknown" ? "unknown email" : "failed"}</Badge>
                  <span>{s.email || "(none)"}</span><span className="ml-auto text-xs text-faint">{ageText(s.t)}</span>
                </li>
              ))}</ul>
            ) : <p className="text-sm text-muted">Nothing recorded yet.</p>}
            <p className="mt-4 text-xs text-faint">Five wrong passwords lock an account for 15 minutes. Emails and times are logged; passwords and IP addresses are not.</p>
          </Card>
        </>
      )}
    </div>
  );
}
