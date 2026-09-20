import { IncidentCreateForm } from "../components/incident-create-form";
import { IncidentList } from "../components/incident-list";

export default function Home() {
  return (
    <main className="page-shell home-shell">
      <header className="top-header">
        <p className="eyebrow">只读故障调查</p>
        <h1>DevSupport Agent</h1>
        <p>基于已配置运行数据和团队知识，收集可追溯证据，协助人工调查微服务故障。</p>
      </header>
      <div className="home-grid">
        <IncidentCreateForm />
        <IncidentList />
      </div>
    </main>
  );
}
