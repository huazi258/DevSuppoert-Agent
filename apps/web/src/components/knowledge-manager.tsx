"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import {
  ApiError,
  listInvestigationTargets,
  listKnowledgeDocuments,
  updateKnowledgeDocumentStatus,
  uploadKnowledgeDocument,
} from "../lib/api";
import {
  formatDate,
  type InvestigationTargetOption,
  type KnowledgeDocument,
  type KnowledgeDocumentType,
  type KnowledgeScope,
} from "../lib/types";

const documentTypeLabels: Record<KnowledgeDocumentType, string> = {
  architecture: "架构文档",
  runbook: "操作手册",
  postmortem: "历史事故",
  config_note: "配置说明",
};

function messageFor(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.status === 0) return error.detail;
  return fallback;
}

export function KnowledgeManager() {
  const [targets, setTargets] = useState<InvestigationTargetOption[]>([]);
  const [targetId, setTargetId] = useState("");
  const [scope, setScope] = useState<KnowledgeScope>("shared");
  const [serviceId, setServiceId] = useState("");
  const [environment, setEnvironment] = useState("common");
  const [documentType, setDocumentType] = useState<KnowledgeDocumentType>("architecture");
  const [version, setVersion] = useState("v1");
  const [file, setFile] = useState<File | null>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loadingTargets, setLoadingTargets] = useState(true);
  const [loadingDocuments, setLoadingDocuments] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [changingDocumentId, setChangingDocumentId] = useState<string | null>(null);

  const selectedTarget = useMemo(
    () => targets.find((target) => target.id === targetId) ?? null,
    [targetId, targets],
  );

  const loadDocuments = useCallback(async (nextTargetId: string) => {
    if (!nextTargetId) {
      setDocuments([]);
      return;
    }
    setLoadingDocuments(true);
    try {
      setDocuments(await listKnowledgeDocuments(nextTargetId));
    } catch (loadError: unknown) {
      setError(messageFor(loadError, "知识文档列表加载失败，请稍后重试。"));
    } finally {
      setLoadingDocuments(false);
    }
  }, []);

  useEffect(() => {
    async function loadTargets() {
      setLoadingTargets(true);
      try {
        setTargets(await listInvestigationTargets());
      } catch (loadError: unknown) {
        setError(messageFor(loadError, "调查目标加载失败，请稍后重试。"));
      } finally {
        setLoadingTargets(false);
      }
    }
    void loadTargets();
  }, []);

  function onTargetChange(nextTargetId: string) {
    setTargetId(nextTargetId);
    setServiceId("");
    setEnvironment("common");
    setError(null);
    void loadDocuments(nextTargetId);
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedTarget) {
      setError("请选择调查目标。");
      return;
    }
    if (scope === "service" && !serviceId) {
      setError("指定服务范围时必须选择服务。");
      return;
    }
    if (!file) {
      setError("请选择要上传的 Markdown 文件。");
      return;
    }
    if (!file.name.toLowerCase().endsWith(".md")) {
      setError("仅支持上传 .md Markdown 文件。");
      return;
    }
    if (!version.trim()) {
      setError("请填写文档版本。");
      return;
    }

    setUploading(true);
    setError(null);
    try {
      await uploadKnowledgeDocument(
        {
          target_id: selectedTarget.id,
          scope,
          service_id: scope === "service" ? serviceId : undefined,
          environment,
          document_type: documentType,
          version: version.trim(),
        },
        file,
      );
      setFile(null);
      const fileInput = document.getElementById("knowledge-markdown-file") as HTMLInputElement | null;
      if (fileInput) fileInput.value = "";
      await loadDocuments(selectedTarget.id);
    } catch (uploadError: unknown) {
      setError(messageFor(uploadError, "知识文档上传失败，请检查文件和范围后重试。"));
    } finally {
      setUploading(false);
    }
  }

  async function toggleStatus(knowledgeDocument: KnowledgeDocument) {
    setChangingDocumentId(knowledgeDocument.id);
    setError(null);
    try {
      const updated = await updateKnowledgeDocumentStatus(
        knowledgeDocument.id,
        knowledgeDocument.status === "enabled" ? "disabled" : "enabled",
      );
      setDocuments((current) => current.map((item) => (item.id === updated.id ? updated : item)));
    } catch (statusError: unknown) {
      setError(messageFor(statusError, "文档状态更新失败，请稍后重试。"));
    } finally {
      setChangingDocumentId(null);
    }
  }

  const serviceUnavailable = selectedTarget !== null && selectedTarget.services.length === 0;

  return (
    <div className="knowledge-layout">
      <section className="panel" aria-labelledby="knowledge-upload-heading">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Markdown 文档</p>
            <h2 id="knowledge-upload-heading">上传并摄取知识文档</h2>
          </div>
        </div>
        <p className="subtle-status">仅支持 Markdown；上传内容会按所选调查目标、服务和环境隔离。</p>
        <form className="incident-form" onSubmit={onSubmit}>
          <label className="full-width">
            调查目标
            <select disabled={loadingTargets || uploading} onChange={(event) => onTargetChange(event.target.value)} value={targetId}>
              <option value="">{loadingTargets ? "正在加载调查目标…" : "请选择调查目标"}</option>
              {targets.map((target) => <option key={target.id} value={target.id}>{target.display_name}（{target.environment}）</option>)}
            </select>
          </label>
          <label>
            范围
            <select
              disabled={!selectedTarget || uploading}
              onChange={(event) => {
                const nextScope = event.target.value as KnowledgeScope;
                setScope(nextScope);
                if (nextScope === "shared") setServiceId("");
              }}
              value={scope}
            >
              <option value="shared">系统共享</option>
              <option value="service">指定服务</option>
            </select>
          </label>
          <label>
            服务
            <select disabled={!selectedTarget || scope !== "service" || serviceUnavailable || uploading} onChange={(event) => setServiceId(event.target.value)} value={serviceId}>
              <option value="">{!selectedTarget ? "请先选择调查目标" : "请选择服务"}</option>
              {selectedTarget?.services.map((service) => <option key={service.id} value={service.id}>{service.display_name}（{service.name}）</option>)}
            </select>
          </label>
          <label>
            环境
            <select disabled={!selectedTarget || uploading} onChange={(event) => setEnvironment(event.target.value)} value={environment}>
              <option value="common">通用</option>
              {selectedTarget ? <option value={selectedTarget.environment}>{selectedTarget.environment}</option> : null}
            </select>
          </label>
          <label>
            文档类型
            <select disabled={!selectedTarget || uploading} onChange={(event) => setDocumentType(event.target.value as KnowledgeDocumentType)} value={documentType}>
              {Object.entries(documentTypeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label>
            版本
            <input disabled={!selectedTarget || uploading} onChange={(event) => setVersion(event.target.value)} required value={version} />
          </label>
          <label className="full-width">
            Markdown 文件
            <input accept=".md,text/markdown" disabled={!selectedTarget || uploading} id="knowledge-markdown-file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} required type="file" />
          </label>
          {serviceUnavailable && scope === "service" ? <p className="error-banner full-width">该调查目标没有可用服务。</p> : null}
          {error ? <p className="error-banner full-width" role="alert">{error}</p> : null}
          <button className="button primary-button full-width" disabled={!selectedTarget || uploading || (scope === "service" && (!serviceId || serviceUnavailable))} type="submit">
            {uploading ? "正在解析并建立索引…" : "上传 Markdown 文档"}
          </button>
        </form>
      </section>

      <section className="panel" aria-labelledby="knowledge-list-heading">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">当前范围</p>
            <h2 id="knowledge-list-heading">知识文档</h2>
          </div>
          <button className="button secondary-button" disabled={!targetId || loadingDocuments} onClick={() => void loadDocuments(targetId)} type="button">刷新</button>
        </div>
        {!targetId ? <p className="empty-state">选择调查目标后查看其知识文档。</p> : null}
        {loadingDocuments ? <p className="empty-state">正在加载知识文档…</p> : null}
        {targetId && !loadingDocuments && documents.length === 0 ? <p className="empty-state">该调查目标还没有知识文档。</p> : null}
        {documents.length > 0 ? (
          <div className="table-scroll">
            <table className="knowledge-table">
              <thead><tr><th>标题</th><th>范围</th><th>环境</th><th>类型</th><th>版本</th><th>更新时间</th><th>状态</th></tr></thead>
              <tbody>
                {documents.map((knowledgeDocument) => (
                  <tr key={knowledgeDocument.id}>
                    <td>{knowledgeDocument.title}</td>
                    <td>{knowledgeDocument.scope === "shared" ? "系统共享" : knowledgeDocument.service_display_name ?? "指定服务"}</td>
                    <td>{knowledgeDocument.environment === "common" ? "通用" : knowledgeDocument.environment}</td>
                    <td>{documentTypeLabels[knowledgeDocument.document_type]}</td>
                    <td>{knowledgeDocument.version}</td>
                    <td>{formatDate(knowledgeDocument.updated_at)}</td>
                    <td><button className="button secondary-button compact-button" disabled={changingDocumentId === knowledgeDocument.id} onClick={() => void toggleStatus(knowledgeDocument)} type="button">{knowledgeDocument.status === "enabled" ? "已启用（停用）" : "已停用（启用）"}</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
