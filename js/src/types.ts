export interface AgentRecord {
  agent_id: string;
  name: string;
  fingerprint: string;
  created_at: number;
}

export interface ConnectResult {
  state: string;
  peer_id: string;
  session_id?: string;
  code?: string;
  expires_at?: number;
  token?: string;
}

export interface SendResult {
  msg_id: string;
  status: string;
  ack_msg_id: string;
  ts: number;
}

export interface InboxMessage {
  msg_id: string;
  sender_id: string;
  target_id: string;
  ts: number;
  state: string;
  reply_to?: string | null;
  body: string;
  direction?: string;
}

export interface ContactRecord {
  peer_id: string;
  name: string;
  state: string;
  fingerprint: string;
  pinned_at: number | null;
  updated_at: number;
  good_at: string;
}
