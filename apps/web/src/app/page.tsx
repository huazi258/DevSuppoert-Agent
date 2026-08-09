import { IncidentCreateForm } from "../components/incident-create-form";
import { IncidentList } from "../components/incident-list";

export default function Home() {
  return (
    <main className="page-shell home-shell">
      <header className="top-header">
        <p className="eyebrow">Operations console</p>
        <h1>DevSupport Agent</h1>
        <p>Incident investigation and controlled remediation console</p>
      </header>
      <div className="home-grid">
        <IncidentCreateForm />
        <IncidentList />
      </div>
    </main>
  );
}
