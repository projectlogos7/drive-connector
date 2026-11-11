export type StageKey = "discover" | "index" | "content" | "embed";
export type StageState = "pending" | "active" | "done" | "skipped";

export interface Stage {
  key: StageKey;
  label: string;
  state: StageState;
  done: number | null;
  total: number | null;
}

export interface CurrentFile {
  name: string | null;
  size: number | null;
  path: string | null;
  kind: string | null;
  stage: StageKey;
  action: string;
  at: string;
}

export interface Progress {
  source_id: string;
  name: string;
  type: "gdrive" | "local";
  status: SourceStatus;
  busy: boolean;
  percent: number;
  label: string;
  step: number | null;
  steps: number;
  stages: Stage[];
  current: CurrentFile | null;
  started_at: string | null;
  files: number | null;
  folders: number | null;
  bytes: number | null;
}

export type SourceStatus = "connecting" | "initial_scan" | "live" | "degraded" | "paused" | "error";

export interface Source {
  id: string;
  name: string;
  type: "gdrive" | "local";
  root_kind: "my_drive" | "folder" | "shared_drive" | "local_folder";
  root: string;
  device: string | null;
  status: SourceStatus;
  status_reason: string | null;
  last_sync_at: string | null;
  files: number;
  folders: number;
  total_size: string;
}

export interface SourceHealth {
  source_id: string;
  last_successful_sync: string | null;
  push_channel?: { active: boolean; expires_at: string | null };
  device?: { name: string | null; online: boolean; last_seen_at: string | null };
  queue: { pending: number; running: number; dead_letter: number };
  last_error: string | null;
}

export interface Change {
  id?: number;
  event_id?: number;
  type: string;
  kind: string | null;
  name: string | null;
  path: string | null;
  from?: string | null;
  from_path?: string | null;
  source: string | null;
  device?: string | null;
  size?: number | null;
  detected_at: string;
}

export interface Notification {
  id: number;
  kind: "changes" | "alert";
  title: string;
  body: string;
  created_at: string;
}

export interface Connection {
  id: string;
  google_email: string | null;
  scopes: string[];
  created_at: string;
  revoked_at: string | null;
}

export interface Device {
  id: string;
  name: string;
  hostname: string | null;
  os: string | null;
  agent_version: string | null;
  last_seen_at: string | null;
  revoked_at: string | null;
  online: boolean;
}

export interface Root {
  kind: "my_drive" | "folder" | "shared_drive";
  id: string;
  name: string;
  source_id?: string;
}
