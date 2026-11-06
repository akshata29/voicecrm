import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import { getErrorMessage, ingestApi, toApiError } from '@/api/client';
import { qk } from '@/api/hooks';
import { deleteRecording, listRecordings, queueEventName, updateRecording, type QueuedRecording } from './queue';

function buildForm(recording: QueuedRecording, blob: Blob): FormData {
  const { metadata } = recording;
  const form = new FormData();
  form.append('file', blob, recording.filename);
  form.append('channel', 'mobile');
  form.append('market', metadata.market);
  form.append('business_line', metadata.business_line);
  form.append('seller_name', metadata.seller_name);
  form.append('seller_email', metadata.seller_email);
  if (metadata.customer_name) form.append('customer_name', metadata.customer_name);
  if (metadata.customer_org) form.append('customer_org', metadata.customer_org);
  if (metadata.language_hint) form.append('language_hint', metadata.language_hint);
  form.append('title', metadata.customer_org ? `${metadata.customer_org} - in-person visit` : 'In-person visit');
  form.append('started_at', recording.started_at);
  form.append('source_interaction_id', recording.id);
  return form;
}

/**
 * Live view of the IndexedDB queue plus the uploader. While `online`, queued recordings upload automatically
 * (on save, on reconnect, and on a slow retry timer for failures that may be transient).
 */
export function useRecordingQueue(online: boolean) {
  const client = useQueryClient();
  const [items, setItems] = useState<QueuedRecording[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [storageError, setStorageError] = useState<string | null>(null);
  const flushing = useRef(false);

  const refresh = useCallback(async () => {
    try {
      setItems(await listRecordings());
      setStorageError(null);
    } catch (error) {
      setStorageError(getErrorMessage(error));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onChange = () => void refresh();
    window.addEventListener(queueEventName(), onChange);
    return () => window.removeEventListener(queueEventName(), onChange);
  }, [refresh]);

  const uploadOne = useCallback(
    async (recording: QueuedRecording): Promise<'uploaded' | 'failed' | 'network'> => {
      if (!recording.blob) return 'failed';
      await updateRecording(recording.id, { status: 'uploading', attempts: recording.attempts + 1, error: null });
      try {
        const response = await ingestApi.upload(buildForm(recording, recording.blob));
        await updateRecording(recording.id, {
          status: 'uploaded',
          blob: null,
          conversation_id: response.duplicate_of ?? response.conversation_id,
          duplicate_of: response.duplicate_of,
          uploaded_at: new Date().toISOString(),
          error: null,
        });
        void client.invalidateQueries({ queryKey: qk.conversations });
        void client.invalidateQueries({ queryKey: qk.metrics });
        return 'uploaded';
      } catch (error) {
        const apiError = toApiError(error);
        // No HTTP status means the network (not the server) failed: keep it queued and try again on reconnect.
        const network = apiError.status === null;
        await updateRecording(recording.id, {
          status: network ? 'queued' : 'failed',
          error: network ? 'Waiting for a connection' : apiError.message,
        });
        return network ? 'network' : 'failed';
      }
    },
    [client],
  );

  const flush = useCallback(async () => {
    if (!online || flushing.current) return;
    flushing.current = true;
    try {
      // Re-read after each pass: recordings saved while an upload was running are picked up too.
      for (;;) {
        const pending = (await listRecordings()).filter((item) => item.status === 'queued' && item.blob).reverse();
        if (pending.length === 0) return;
        for (const item of pending) {
          if ((await uploadOne(item)) === 'network') return;
        }
      }
    } finally {
      flushing.current = false;
    }
  }, [online, uploadOne]);

  const queuedCount = items.filter((item) => item.status === 'queued').length;

  // Auto-upload: when new items arrive while online, and whenever the connection returns.
  useEffect(() => {
    if (online && queuedCount > 0) void flush();
  }, [online, queuedCount, flush]);

  // If the server was unreachable while the browser still reports "online", keep trying.
  useEffect(() => {
    if (!online || queuedCount === 0) return;
    const id = window.setInterval(() => void flush(), 20_000);
    return () => window.clearInterval(id);
  }, [online, queuedCount, flush]);

  // Slow retry for transient server-side failures.
  const failedCount = items.filter((item) => item.status === 'failed' && item.attempts < 4).length;
  useEffect(() => {
    if (!online || failedCount === 0) return;
    const id = window.setInterval(() => {
      void (async () => {
        const failed = (await listRecordings()).filter((item) => item.status === 'failed' && item.attempts < 4);
        for (const item of failed) await updateRecording(item.id, { status: 'queued' });
      })();
    }, 30_000);
    return () => window.clearInterval(id);
  }, [online, failedCount]);

  const retry = useCallback(async (id: string) => {
    await updateRecording(id, { status: 'queued', error: null });
  }, []);

  const remove = useCallback(async (id: string) => {
    await deleteRecording(id);
  }, []);

  return { items, loaded, storageError, retry, remove, flush };
}
