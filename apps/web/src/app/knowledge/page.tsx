import Link from "next/link";

import { KnowledgeManager } from "../../components/knowledge-manager";

export default function KnowledgePage() {
  return (
    <main className="page-shell knowledge-shell">
      <Link className="back-link" href="/">← 返回首页</Link>
      <header className="top-header">
        <p className="eyebrow">团队知识库</p>
        <h1>知识库管理</h1>
        <p>上传、查看和启停按调查目标隔离的 Markdown 知识文档。</p>
      </header>
      <KnowledgeManager />
    </main>
  );
}
