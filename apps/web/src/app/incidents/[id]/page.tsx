"use client";

import { useParams } from "next/navigation";

import { IncidentConsole } from "../../../components/incident-console";

export default function IncidentPage() {
  const { id } = useParams<{ id: string }>();
  return <IncidentConsole incidentId={id} />;
}
