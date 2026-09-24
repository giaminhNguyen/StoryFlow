import { useEffect, useRef, useState, type FormEvent } from "react";
import type { ApiClient } from "../api/client";
import { errorDetails, errorMessage, newKey } from "../format";

export function CreateWorkflowForm({ client, defaultVideoId, onCreated }: {
  client: ApiClient; defaultVideoId?: string | null; onCreated: (workflowId: string) => void;
}) {
  const [name, setName] = useState("");
  const [videoId, setVideoId] = useState(defaultVideoId ?? "");
  const [touchedVideo, setTouchedVideo] = useState(false);
  const [language, setLanguage] = useState("en");
  const [branch, setBranch] = useState("");
  const [voice, setVoice] = useState("narrator");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; details: string[] } | null>(null);
  const keyRef = useRef<string | null>(null);

  useEffect(() => {
    if (defaultVideoId && !touchedVideo) setVideoId((cur) => cur || defaultVideoId);
  }, [defaultVideoId, touchedVideo]);

  // Editing any field changes the payload, so it must not reuse the previous idempotency key.
  const edit = (set: (v: string) => void) => (e: { target: { value: string } }) => {
    keyRef.current = null;
    set(e.target.value);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    keyRef.current ??= newKey();
    const story: Record<string, unknown> = {};
    if (branch.trim()) story.branch = branch.trim();
    setBusy(true);
    setError(null);
    try {
      const res = await client.createWorkflow({
        name: name.trim(),
        client_key: keyRef.current,
        config: {
          source: { video_id: videoId.trim(), languages: [language.trim()] },
          story,
          tts: { voice: voice.trim() },
        },
      });
      keyRef.current = null;
      onCreated(res.workflow.id);
    } catch (err) {
      setError({ message: errorMessage(err), details: errorDetails(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)} aria-label="Create workflow" className="card form">
      <h2>Create workflow</h2>
      <label>Name
        <input value={name} onChange={edit(setName)} required disabled={busy} />
      </label>
      <label>YouTube video id
        <input value={videoId} required disabled={busy}
               onChange={(e) => { setTouchedVideo(true); edit(setVideoId)(e); }} />
      </label>
      <label>Language
        <input value={language} onChange={edit(setLanguage)} required disabled={busy} />
      </label>
      <label>Story branch (optional)
        <input value={branch} onChange={edit(setBranch)} disabled={busy} />
      </label>
      <label>Voice
        <input value={voice} onChange={edit(setVoice)} required disabled={busy} />
      </label>
      <button type="submit" disabled={busy}>{busy ? "Creating..." : "Create workflow"}</button>
      {error && (
        <div role="alert" className="banner banner-error">
          {error.message}
          {error.details.length > 0 && <ul>{error.details.map((d) => <li key={d}>{d}</li>)}</ul>}
        </div>
      )}
    </form>
  );
}
