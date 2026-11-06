/**
 * Offline upload queue for the Capture screen, backed by IndexedDB (idb).
 * A recording is always written here first, so nothing is lost when the phone is offline or the upload fails.
 * After a successful upload the audio blob is dropped (no recording is kept on the device).
 */
import { openDB, type DBSchema, type IDBPDatabase } from 'idb';

export interface CaptureMetadata {
  customer_org: string;
  customer_name: string;
  market: string;
  business_line: string;
  /** BCP-47 language hint, empty = auto-detect. */
  language_hint: string;
  seller_name: string;
  seller_email: string;
}

export type QueueStatus = 'queued' | 'uploading' | 'uploaded' | 'failed';

export interface QueuedRecording {
  /** Also sent as source_interaction_id, so a retried upload can never create a duplicate. */
  id: string;
  created_at: string;
  started_at: string;
  duration_seconds: number;
  mime_type: string;
  filename: string;
  size_bytes: number;
  blob: Blob | null;
  metadata: CaptureMetadata;
  status: QueueStatus;
  attempts: number;
  error: string | null;
  conversation_id: string | null;
  duplicate_of: string | null;
  uploaded_at: string | null;
}

interface VoiceCrmDb extends DBSchema {
  recordings: {
    key: string;
    value: QueuedRecording;
    indexes: { 'by-created': string };
  };
}

const DB_NAME = 'voicecrm-capture';
const QUEUE_EVENT = 'voicecrm-queue-changed';

let dbPromise: Promise<IDBPDatabase<VoiceCrmDb>> | null = null;

function database(): Promise<IDBPDatabase<VoiceCrmDb>> {
  dbPromise ??= openDB<VoiceCrmDb>(DB_NAME, 1, {
    upgrade(db) {
      const store = db.createObjectStore('recordings', { keyPath: 'id' });
      store.createIndex('by-created', 'created_at');
    },
  });
  return dbPromise;
}

export function queueEventName(): string {
  return QUEUE_EVENT;
}

function notify() {
  window.dispatchEvent(new Event(QUEUE_EVENT));
}

export async function listRecordings(): Promise<QueuedRecording[]> {
  const db = await database();
  const all = await db.getAllFromIndex('recordings', 'by-created');
  return all.reverse();
}

export async function addRecording(recording: QueuedRecording): Promise<void> {
  const db = await database();
  await db.put('recordings', recording);
  notify();
}

export async function updateRecording(id: string, patch: Partial<QueuedRecording>): Promise<QueuedRecording | undefined> {
  const db = await database();
  const current = await db.get('recordings', id);
  if (!current) return undefined;
  const next = { ...current, ...patch };
  await db.put('recordings', next);
  notify();
  return next;
}

export async function deleteRecording(id: string): Promise<void> {
  const db = await database();
  await db.delete('recordings', id);
  notify();
}

export async function clearUploaded(): Promise<void> {
  const db = await database();
  const all = await db.getAll('recordings');
  await Promise.all(all.filter((item) => item.status === 'uploaded').map((item) => db.delete('recordings', item.id)));
  notify();
}

export function newRecordingId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return `rec-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function extensionFor(mimeType: string): string {
  const type = mimeType.toLowerCase();
  if (type.includes('mp4') || type.includes('m4a') || type.includes('aac')) return 'm4a';
  if (type.includes('ogg')) return 'ogg';
  if (type.includes('wav')) return 'wav';
  if (type.includes('mpeg') || type.includes('mp3')) return 'mp3';
  return 'webm';
}
